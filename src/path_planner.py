#!/usr/bin/env python
"""
Uses map to create waypoints to desired position
"""
import math
import sys
import threading

import carla
from agents.navigation.global_route_planner import GlobalRoutePlanner

import carla_common.transforms as trans
import ros_compatibility as roscomp
from ros_compatibility.exceptions import *
from ros_compatibility.node import CompatibleNode
from ros_compatibility.qos import QoSProfile, DurabilityPolicy
from transforms3d.euler import euler2mat, quat2euler, euler2quat

from carla_msgs.msg import CarlaWorldInfo
from carla_waypoint_types.srv import GetWaypoint, GetActorWaypoint
from geometry_msgs.msg import PoseStamped
from nav_msgs.msg import Path, OccupancyGrid

import rospy

from hybrid_astar import hybrid_astar_planning
import tf
import numpy as np
from log_waypoints import * 

from tf.transformations import quaternion_from_euler, euler_from_quaternion

import time
import os
import rospkg


THRESHOLD = 0.3

def on_collision(event):
    print("COLLISION")

class AstarPathPlanner(CompatibleNode):

    """
    Generates a path using AStar for vehicle to follow based on SLAM map
    """
    WAYPOINT_DISTANCE = 2.0

    def __init__(self):
        """
        Constructor
        """
        super(AstarPathPlanner, self).__init__('astar_waypoint_publisher')
        self.connect_to_carla()
        self.map = self.world.get_map()
        self.slam_map = None #Added SLAM MAP
        self.ego_vehicle = None
        self.ego_vehicle_location = None
        self.on_tick = None
        self.role_name = self.get_param("role_name", 'ego_vehicle')
        self.waypoint_publisher = self.new_publisher(
            Path,
            '/carla/{}/waypoints'.format(self.role_name),
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))

        # initialize ros services
        self.get_waypoint_service = self.new_service(
            GetWaypoint,
            '/carla_waypoint_publisher/{}/get_waypoint'.format(self.role_name),
            self.get_waypoint)
        self.get_actor_waypoint_service = self.new_service(
            GetActorWaypoint,
            '/carla_waypoint_publisher/{}/get_actor_waypoint'.format(self.role_name),
            self.get_actor_waypoint)

        # set initial goal
        # self.goal = self.world.get_map().get_spawn_points()[0]\
        position_param = rospy.get_param('initial_goal/position', [0.0, 0.0, 0.0])
        orientation_param = rospy.get_param('initial_goal/orientation', [0.0, 0.0, 0.0, 1.0])
        position = position_param.split(',')
        orientation = orientation_param.split(',')

        print(position)
        print(orientation)
        print(position[0])
        print(orientation[0])

        self.goal_position = position[:3]
        

        goal = PoseStamped()
        goal.header.frame_id = "map"
        goal.header.stamp = rospy.Time.now()

        goal.pose.position.x = float(position[0])
        goal.pose.position.y = float(position[1])
        goal.pose.position.z = float(position[2])

        goal.pose.orientation.x = float(orientation[0])
        goal.pose.orientation.y = float(orientation[1])
        goal.pose.orientation.z = float(orientation[2])
        goal.pose.orientation.w = float(orientation[3])

        self.goal_orientation = goal.pose.orientation

        self.goal = trans.ros_pose_to_carla_transform(goal.pose)

        self.current_route = None
        self.goal_subscriber = self.new_subscription(
            PoseStamped,
            "/carla/{}/goal".format(self.role_name),
            self.on_goal,
            qos_profile=10)
        
        #Subscrive to SLAM map
        self.map_subscriber = self.new_subscription(
            OccupancyGrid,
            "/map",
            self.on_map_update,
            qos_profile=10
        )

        # use callback to wait for ego vehicle
        self.last_map_update_time = time.time()
        self.loginfo("Waiting for ego vehicle...")
        self.on_tick = self.world.on_tick(self.find_ego_vehicle_actor)

        #Get vehicle pose:
        self.tf_listener = tf.TransformListener()
        self.vehicle_frame = "{}".format(self.role_name) + "_rear"  # e.g., "ego_vehicle"
        self.global_frame = "map"  # Assuming "map" is the global reference frame


        self.tf_listener_center = tf.TransformListener()
        self.vehicle_frame_center = "{}".format(self.role_name)  # e.g., "ego_vehicle"

        # (cur_trans, cur_quat) = self.tf_listener_center.lookupTransform(self.global_frame, self.vehicle_frame_center, rospy.Time(0))
        datetime_now = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.attached = False
        # self.log_filename = f"waypoints_log.txt"
        # self.log_filename_gt = f"waypoints_log_groundtruth.txt"

        # with open(self.log_filename, 'a') as f:
        #     f.write("=======================")

        # with open(self.log_filename_gt, 'a') as f:
        #     f.write("=======================")

        self.timer = rospy.Timer(rospy.Duration(0.2 ), self.timer_callback)
        
    def quaternion_to_yaw_deg(self, q):
        """
        q is an object with attributes x, y, z, w (CARLA-style quaternion)
        Returns yaw angle in degrees
        """
        siny_cosp = 2 * (q.w * q.z + q.x * q.y)
        cosy_cosp = 1 - 2 * (q.y**2 + q.z**2)
        yaw_rad = math.atan2(siny_cosp, cosy_cosp)
        return math.degrees(yaw_rad)

    def reached_goal(self, loc, rot):
        goal = np.array([float(x) for x in self.goal_position])
        current = np.array([loc.x, -loc.y, loc.z])

        dist = np.linalg.norm(goal - current)
        print("\nDISTANCE to GOAL: ", dist)
        
        yaw_goal = self.quaternion_to_yaw_deg(self.goal_orientation)
        # angle_dist = abs(rot.yaw - yaw_goal) % 360

        # print("ANGLE to GOAL: ", rot.yaw, yaw_goal, angle_dist)
        
        return dist < 1.0

    def timer_callback(self, event):
        # if ego vehicle is defined and we haven't attached a collision detector
        if self.ego_vehicle and not self.attached:
            blueprint_library = self.world.get_blueprint_library()
            collision_sensor_bp = blueprint_library.find('sensor.other.collision')
            collision_sensor = self.world.spawn_actor(collision_sensor_bp, carla.Transform(), attach_to=self.ego_vehicle)
            collision_sensor.listen(lambda event: on_collision(event))
            self.attached = True

        (cur_trans, cur_quat) = self.tf_listener_center.lookupTransform(self.global_frame, self.vehicle_frame_center, rospy.Time(0))
        carla_loc = carla.Location(cur_trans[0], -cur_trans[1], cur_trans[2])

        roll, pitch, yaw = quat2euler([cur_quat[3],
                                    cur_quat[0],
                                    cur_quat[1],
                                    cur_quat[2]])
        carla_rot = trans.RPY_to_carla_rotation(roll, pitch, yaw)

        velocity = self.ego_vehicle.get_velocity()
        speed = (velocity.x**2 + velocity.y**2)**0.5
        moving = speed > 0.01
        print("speed" , speed)
        print("moving" , moving)
        if self.reached_goal(carla_loc, carla_rot) and not moving:
            print("FINISHED")
            return 

        gt_location = self.ego_vehicle.get_location()
        gt_rot = self.ego_vehicle.get_transform().rotation 

        # log_line = f"{carla_loc.x:.3f}, {carla_loc.y:.3f}, {carla_loc.z:.3f}, {carla_rot.pitch:.2f}, {carla_rot.yaw:.2f}, {carla_rot.roll:.2f}\n"
        # log_line_gt= f"{gt_location.x:.3f}, {gt_location.y:.3f}, {gt_location.z:.3f}, {gt_rot.pitch:.2f}, {gt_rot.yaw:.2f}, {gt_rot.roll:.2f}\n"

        loc_quat = trans.carla_rotation_to_ros_quaternion(carla_rot)
        gt_quat = trans.carla_rotation_to_ros_quaternion(gt_rot)     
        current_time = time.time()  
        log_line = f"{current_time} {carla_loc.x:.3f} {carla_loc.y:.3f} {carla_loc.z:.3f} {loc_quat.x:.4f} {loc_quat.y:.4f} {loc_quat.z:.4f} {loc_quat.w:.4f}\n"
        log_line_gt= f"{current_time} {gt_location.x:.3f} {gt_location.y:.3f} {gt_location.z:.3f} {gt_quat.x:.4f} {gt_quat.y:.4f} {gt_quat.z:.4f} {gt_quat.w:.4f}\n"
        
        
        print("LOCALIZED POSE INFO: " + log_line)
        print("GROUNDTRUTH POSE INFO: " + log_line_gt)

        # with open(self.log_filename, 'a') as f:
        #     f.write(log_line)

        # with open(self.log_filename_gtis_within_goal, 'a') as f:
        #     f.write(log_line_gt)

        goal_loc = self.goal.location
        goal_yaw = self.goal.rotation.yaw
        print("goal_loc", goal_loc)

        if is_within_goal(carla_loc, carla_rot.yaw, goal_loc, goal_yaw):
            print("Reached goal. Shutting down timer!")
            self.timer.shutdown()
            return 

    def destroy(self):
        """
        Destructor
        """
        self.ego_vehicle = None
        if self.on_tick:
            self.world.remove_on_tick(self.on_tick)

    def get_waypoint(self, req, response=None):        

        carla_position = carla.Location()

        carla_position.x = req.location.x
        carla_position.y = -req.location.y
        carla_position.z = req.location.z

        carla_waypoint = self.map.get_waypoint(carla_position)

        response = roscomp.get_service_response(GetWaypoint)
        response.waypoint.pose = trans.carla_transform_to_ros_pose(carla_waypoint.transform)
        response.waypoint.is_junction = carla_waypoint.is_junction
        response.waypoint.road_id = carla_waypoint.road_id
        response.waypoint.section_id = carla_waypoint.section_id
        response.waypoint.lane_id = carla_waypoint.lane_id
        return response

    def get_actor_waypoint(self, req, response=None):
        """
        Convenience method to get the waypoint for an actor
        """
        # self.loginfo("get_actor_waypoint(): Get waypoint of actor {}".format(req.id))
        actor = self.world.get_actors().find(req.id)

        response = roscomp.get_service_response(GetActorWaypoint)
        if actor:
            carla_waypoint = self.map.get_waypoint(actor.get_location())
            response.waypoint.pose = trans.carla_transform_to_ros_pose(carla_waypoint.transform)
            response.waypoint.is_junction = carla_waypoint.is_junction
            response.waypoint.road_id = carla_waypoint.road_id
            response.waypoint.section_id = carla_waypoint.section_id
            response.waypoint.lane_id = carla_waypoint.lane_id
        else:
            self.logwarn("get_actor_waypoint(): Actor {} not valid.".format(req.id))
        return response

    def on_goal(self, goal):
        """
        Callback for /move_base_simple/goal

        Receiving a goal (e.g. from RVIZ '2D Nav Goal') triggers a new route calculation.

        :return:
        """
        self.loginfo("Received goal, trigger rerouting...")
        carla_goal = trans.ros_pose_to_carla_transform(goal.pose)
        self.goal = carla_goal
        self.reroute()

    def on_map_update(self, occ_map):
        """
        Callback for /map

        When Map is updated, reroute to avoid new obstacles.

        :return:
        """
        self.loginfo("Received new map, trigger rerouting...")
        current_time = time.time()

        update_time = self.get_param("astar_update", 1.5)

        if current_time - self.last_map_update_time >= update_time:
            self.last_map_update_time = current_time
            self.slam_map = occ_map
            self.reroute()

    def reroute(self):
        """
        Triggers a rerouting
        """
        
        if self.ego_vehicle is None or self.goal is None:
            # no ego vehicle, remove route if published
            self.current_route = None
            self.publish_waypoints()
        else:
            self.current_route = self.calculate_route(self.goal)
        self.publish_waypoints()

    def find_ego_vehicle_actor(self, _):
        """
        Look for an carla actor with name 'ego_vehicle'
        """
        hero = None
        for actor in self.world.get_actors():
            if actor.attributes.get('role_name') == self.role_name:
                hero = actor
                break

        ego_vehicle_changed = False
        if hero is None and self.ego_vehicle is not None:
            ego_vehicle_changed = True

        if not ego_vehicle_changed and hero is not None and self.ego_vehicle is None:
            ego_vehicle_changed = True

        if not ego_vehicle_changed and hero is not None and \
                self.ego_vehicle is not None and hero.id != self.ego_vehicle.id:
            ego_vehicle_changed = True

        if ego_vehicle_changed:
            self.loginfo("Ego vehicle changed.")
            self.ego_vehicle = hero
            self.reroute()
        elif self.ego_vehicle:
            current_location = self.ego_vehicle.get_location()
            if self.ego_vehicle_location:
                dx = self.ego_vehicle_location.x - current_location.x
                dy = self.ego_vehicle_location.y - current_location.y
                distance = math.sqrt(dx * dx + dy * dy)
                if distance > self.WAYPOINT_DISTANCE:
                    self.loginfo("Ego vehicle was repositioned.")
                    self.reroute()
            self.ego_vehicle_location = current_location

    def calculate_route(self, goal):
        """
        Calculate a route from the current location to 'goal'
        """
        self.loginfo("Calculating route to x={}, y={}, z={}".format(
            goal.location.x,
            goal.location.y,
            goal.location.z))

        # grp = GlobalRoutePlanner(self.world.get_map(), sampling_resolution=1)
        # route = grp.trace_route(self.ego_vehicle.get_location(),
        #                         carla.Location(goal.location.x,
        #                                        goal.location.y,
        #                                        goal.location.z))
        
        #TODO: This should actually get pose from ROS, not carla...

        (cur_trans, cur_quat) = self.tf_listener.lookupTransform(self.global_frame, self.vehicle_frame, rospy.Time(0))
        cur_rot = euler_from_quaternion(cur_quat)

        goal_rot = trans.carla_rotation_to_RPY(goal.rotation)
        
        map_width = self.slam_map.info.width
        map_height = self.slam_map.info.height
        map_res = self.slam_map.info.resolution
        origin_x = self.slam_map.info.origin.position.x
        origin_y = self.slam_map.info.origin.position.y

        occ = np.array(self.slam_map.data).reshape((map_height, map_width))
        
        occupied_indices = np.column_stack(np.where(occ == 100))

        occupied_points_world = np.column_stack([
            occupied_indices[:, 1] * map_res + origin_x,  # x-coordinates
            occupied_indices[:, 0] * map_res + origin_y   # y-coordinates
        ])
        

        obs_dist = 15
        mask = (np.abs(occupied_points_world[:, 0] - cur_trans[0]) < obs_dist) & (np.abs(occupied_points_world[:, 1] - cur_trans[1]) < obs_dist)
        occupied_points_world = occupied_points_world[mask]

        # print(occupied_points_world.shape)
        # print(occupied_points_world[0:10])

        if occupied_points_world.size == 0:
            occupied_points_world = np.array([[0.0, 0.0], [0.01, 0.01]])

        route, _ = hybrid_astar_planning(cur_trans[0], cur_trans[1], cur_rot[2],
                                      goal.location.x, -goal.location.y, goal_rot[2], 
                                      occupied_points_world[:, 0], occupied_points_world[:, 1], 0.5, np.deg2rad(9.0)) #TODO: input map into astar

        if route is not None:
            self.loginfo("Succesfully Found Route!")
        else:
            self.loginfo("ERROR: No Route Found!")

        return route

    def publish_waypoints(self):
        """
        Publish the ROS message containing the waypoints
        """
        msg = Path()
        msg.header.frame_id = "map"
        msg.header.stamp = roscomp.ros_timestamp(self.get_time(), from_sec=True)
        if self.current_route is not None and len(self.current_route.x) > 3:
            for i in range(len(self.current_route.x)):
                if i == 0 and i == 1:
                    continue
                pose = PoseStamped()
                # pose.pose = trans.carla_transform_to_ros_pose(wp[0].transform)

                #Shifts waypoints to center of car frame instead of rear middle wheel:
                shift_dist = 1.437 #TODO: Make this a param in ros
                dx = shift_dist * np.cos(self.current_route.yaw[i])
                dy = shift_dist * np.sin(self.current_route.yaw[i])
                
                # Apply the shift
                shifted_x = self.current_route.x[i] + dx
                shifted_y = self.current_route.y[i] + dy

                #Publishes poses to ROS calculated by AStar
                if i == len(self.current_route.x) - 1:
                    pose.pose.position.x = self.current_route.x[i]
                    pose.pose.position.y = self.current_route.y[i]
                else:
                    pose.pose.position.x = shifted_x
                    pose.pose.position.y = shifted_y
                # pose.pose.position.x = self.current_route.x[i]
                # pose.pose.position.y = self.current_route.y[i]
                pose.pose.position.z = 0.0  # Assuming flat path (no height)

                # Convert yaw to quaternion
                quat = quaternion_from_euler(0.0, 0.0, self.current_route.yaw[i])
                
                # Set orientation (quaternion) from yaw
                pose.pose.orientation.x = quat[0]
                pose.pose.orientation.y = quat[1]
                pose.pose.orientation.z = quat[2]
                pose.pose.orientation.w = quat[3]
                msg.poses.append(pose)

        self.waypoint_publisher.publish(msg)
        self.loginfo("Published {} waypoints.".format(len(msg.poses)))

    def connect_to_carla(self):

        self.loginfo("Waiting for CARLA world (topic: /carla/world_info)...")
        try:
            self.wait_for_message(
                "/carla/world_info",
                CarlaWorldInfo,
                qos_profile=QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL),
                timeout=50.0)
        except ROSException as e:
            self.logerr("Error while waiting for world info: {}".format(e))
            raise e

        host = self.get_param("host", "127.0.0.1")
        port = self.get_param("port", 2000)
        timeout = self.get_param("timeout", 10)
        self.loginfo("CARLA world available. Trying to connect to {host}:{port}".format(
            host=host, port=port))

        carla_client = carla.Client(host=host, port=port)
        carla_client.set_timeout(timeout)

        try:
            self.world = carla_client.get_world()
        except RuntimeError as e:
            self.logerr("Error while connecting to Carla: {}".format(e))
            raise e

        self.loginfo("Connected to Carla.")


def main(args=None):
    """
    main function
    """
    roscomp.init('carla_waypoint_publisher', args)

    waypoint_converter = None
    try:
        waypoint_converter = AstarPathPlanner()
        waypoint_converter.spin()
    except (RuntimeError, ROSException):
        pass
    except KeyboardInterrupt:
        roscomp.loginfo("User requested shut down.")
    finally:
        roscomp.loginfo("Shutting down.")
        if waypoint_converter:
            waypoint_converter.destroy()
        roscomp.shutdown()


if __name__ == "__main__":
    main()

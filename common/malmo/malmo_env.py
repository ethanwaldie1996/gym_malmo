import logging
import time
import os

import numpy as np
import json
import xml.etree.ElementTree as ET
import gym
from gym import spaces, error

import MalmoPython
import common.malmo.malmo_server as minecraft_py

SINGLE_DIRECTION_DISCRETE_MOVEMENTS = ["jumpeast", "jumpnorth", "jumpsouth", "jumpwest",
                                       "movenorth", "moveeast", "movesouth", "movewest",
                                       "jumpuse", "use", "attack", "jump"]

MULTIPLE_DIRECTION_DISCRETE_MOVEMENTS = ["move", "turn", "look", "strafe",
                                         "jumpmove", "jumpstrafe"]


class MalmoEnvironment(gym.Env):
    """
    Based on the GYM environment created by:

    https://github.com/tambetm/gym-minecraft/blob/master/gym_minecraft/envs/minecraft_env.py

    """
    metadata = {'render.modes': ['human']}

    def __init__(self, parse_world_state=False,
                 ):

        self.logger = logging.getLogger(__name__)

        super(MalmoEnvironment, self).__init__()

        self.agent_host = MalmoPython.AgentHost()
        self.parse_world_state = parse_world_state
        self.tick_speed = 5
        self.client_pool = None
        self.mc_process = None
        self.screen = None
        self.previous_observations = None
        self.num_actions = 0
        self.agent_position = None
        self._num_resets = 0
        self._reset_counter = 20
        self.replay_buffer_size = 5
        self.replay_buffer = []

    def _load_mission(self, **kwargs) -> MalmoPython.MissionSpec:
        """
        Override this function to generate a mission spec. The return should be
        a string containing the XML for this mission.

        :return:
        """
        raise NotImplementedError("You must Implement a Mission Spec in order to start a mission!")

    def _world_state_parser(self, world_state: MalmoPython.WorldState) -> np.ndarray:
        """
        This function is used to control the encoding of the world state that is returned to the agent.

        :param world_state:
        :return:
        """

        raise NotImplementedError("If not collecting data from images you must implement a world state parser.")

    def _build_observation_space(self):
        """
        This method will be called any time a state parser is detected, it allows for the observation space to be
        configured to match a specific specification.

        :return:
        """

        raise NotImplementedError("If you must implement an observation space constructor!")

    def _init_replay_buffer(self):


        shape = list(self.observation_space.shape)

        # correct for the replay buffer size in the shape.
        shape[0] = int(shape[0]/self.replay_buffer_size)

        initial_observation = np.zeros(shape=(tuple(shape)))

        self.replay_buffer = [initial_observation]*self.replay_buffer_size

    @staticmethod
    def load_mission_xml_from_file(path: str) -> MalmoPython.MissionSpec:
        with open(path, 'r') as f:
            mission_spec = f.read()
        mission_spec = MalmoPython.MissionSpec(mission_spec, True)
        return mission_spec

    def init(self, client_pool=None,
             start_minecraft=None,
             continuous_discrete=True,
             add_noop_command=None,
             max_retries=90,
             retry_sleep=10,
             step_sleep=0.001,
             skip_steps=0,
             tick_speed=5,
             replay_buffer_size=5,
             logger=None,
             videoResolution=None,
             videoWithDepth=None,
             observeRecentCommands=None,
             observeHotBar=None,
             observeFullInventory=None,
             observeGrid=None,
             observeDistance=None,
             observeChat=None,
             allowContinuousMovement=None,
             allowDiscreteMovement=None,
             allowAbsoluteMovement=None,
             recordDestination=None,
             recordObservations=None,
             recordRewards=None,
             recordCommands=None,
             recordMP4=None,
             gameMode=None,
             forceWorldReset=None):

        if logger:
            self.logger = logger

        self.max_retries = max_retries
        self.retry_sleep = retry_sleep
        self.step_sleep = step_sleep
        self.skip_steps = skip_steps
        self.tick_speed = tick_speed
        self.forceWorldReset = forceWorldReset
        self.continuous_discrete = continuous_discrete
        self.add_noop_command = add_noop_command

        self.replay_buffer_size = replay_buffer_size

        self.mission_spec = self._load_mission()
        self.logger.info("Loaded mission: " + self.mission_spec.getSummary())

        if videoResolution:
            if videoWithDepth:
                self.mission_spec.requestVideoWithDepth(*videoResolution)
            else:
                self.mission_spec.requestVideo(*videoResolution)

        if observeRecentCommands:
            self.mission_spec.observeRecentCommands()
        if observeHotBar:
            self.mission_spec.observeHotBar()
        if observeFullInventory:
            self.mission_spec.observeFullInventory()
        if observeGrid:
            self.mission_spec.observeGrid(*(observeGrid + ["grid"]))
        if observeDistance:
            self.mission_spec.observeDistance(*(observeDistance + ["dist"]))
        if observeChat:
            self.mission_spec.observeChat()

        if allowContinuousMovement or allowDiscreteMovement or allowAbsoluteMovement:
            # if there are any parameters, remove current command handlers first
            self.mission_spec.removeAllCommandHandlers()

            if allowContinuousMovement is True:
                self.mission_spec.allowAllContinuousMovementCommands()
            elif isinstance(allowContinuousMovement, list):
                for cmd in allowContinuousMovement:
                    self.mission_spec.allowContinuousMovementCommand(cmd)

            if allowDiscreteMovement is True:
                self.mission_spec.allowAllDiscreteMovementCommands()
            elif isinstance(allowDiscreteMovement, list):
                for cmd in allowDiscreteMovement:
                    self.mission_spec.allowDiscreteMovementCommand(cmd)

            if allowAbsoluteMovement is True:
                self.mission_spec.allowAllAbsoluteMovementCommands()
            elif isinstance(allowAbsoluteMovement, list):
                for cmd in allowAbsoluteMovement:
                    self.mission_spec.allowAbsoluteMovementCommand(cmd)

        if start_minecraft:
            # start Minecraft process assigning port dynamically
            self.mc_process, port = minecraft_py.start()
            self.logger.info("Started Minecraft on port %d, overriding client_pool.", port)
            client_pool = [('127.0.0.1', port)]

        if client_pool:
            if not isinstance(client_pool, list):
                raise ValueError("client_pool must be list of tuples of (IP-address, port)")
            self.client_pool = MalmoPython.ClientPool()
            for client in client_pool:
                self.client_pool.add(MalmoPython.ClientInfo(*client))

        # TODO: produce observation space dynamically based on requested features

        self.video_height = self.mission_spec.getVideoHeight(0)
        self.video_width = self.mission_spec.getVideoWidth(0)
        self.video_depth = self.mission_spec.getVideoChannels(0)
        if self.parse_world_state:
            self._build_observation_space()
        else:

            self.observation_space = spaces.Box(low=0, high=255,
                                                shape=(self.replay_buffer_size*self.video_height,
                                                       self.video_width,
                                                       self.video_depth))
        self.last_image = np.zeros(shape=(self.video_height, self.video_width, self.video_depth))


        self._create_action_space()

        # mission recording
        self.mission_record_spec = MalmoPython.MissionRecordSpec()  # record nothing
        if recordDestination:
            self.mission_record_spec.setDestination(recordDestination)
        if recordRewards:
            self.mission_record_spec.recordRewards()
        if recordCommands:
            self.mission_record_spec.recordCommands()
        if recordMP4:
            self.mission_record_spec.recordMP4(*recordMP4)

        if gameMode:
            if gameMode == "spectator":
                self.mission_spec.setModeToSpectator()
            elif gameMode == "creative":
                self.mission_spec.setModeToCreative()
            elif gameMode == "survival":
                self.logger.warning = ("Cannot force survival mode, assuming it is the default.")
            else:
                assert False, "Unknown game mode: " + gameMode

    def _create_action_space(self):
        # collect different actions based on allowed commands
        continuous_actions = []
        discrete_actions = []
        multidiscrete_actions = []
        multidiscrete_action_ranges = []
        if self.add_noop_command:
            # add NOOP command
            discrete_actions.append("move 0\nturn 0")
        chs = self.mission_spec.getListOfCommandHandlers(0)
        for ch in chs:
            cmds = self.mission_spec.getAllowedCommands(0, ch)
            for cmd in cmds:
                self.logger.debug(ch + ":" + cmd)
                if ch == "ContinuousMovement":
                    if cmd in ["move", "strafe", "pitch", "turn"]:
                        if self.continuous_discrete:
                            discrete_actions.append(cmd + " 1")
                            discrete_actions.append(cmd + " -1")
                        else:
                            continuous_actions.append(cmd)
                    elif cmd in ["crouch", "jump", "attack", "use"]:
                        if self.continuous_discrete:
                            discrete_actions.append(cmd + " 1")
                            discrete_actions.append(cmd + " 0")
                        else:
                            multidiscrete_actions.append(cmd)
                            multidiscrete_action_ranges.append([0, 1])
                    else:
                        raise ValueError("Unknown continuous action " + cmd)
                elif ch == "DiscreteMovement":
                    if cmd in SINGLE_DIRECTION_DISCRETE_MOVEMENTS:
                        discrete_actions.append(cmd + " 1")
                    elif cmd in MULTIPLE_DIRECTION_DISCRETE_MOVEMENTS:
                        discrete_actions.append(cmd + " 1")
                        discrete_actions.append(cmd + " -1")
                    else:
                        raise ValueError(False, "Unknown discrete action " + cmd)
                elif ch == "AbsoluteMovement":
                    # TODO: support for AbsoluteMovement
                    self.logger.warning("Absolute movement not supported, ignoring.")
                elif ch == "Inventory":
                    # TODO: support for Inventory
                    self.logger.warning("Inventory management not supported, ignoring.")
                else:
                    self.logger.warning("Unknown commandhandler " + ch)

        # turn action lists into action spaces
        self.action_names = []
        self.action_spaces = []
        if len(discrete_actions) > 0:
            self.action_spaces.append(spaces.Discrete(len(discrete_actions)))
            self.action_names.append(discrete_actions)
        if len(continuous_actions) > 0:
            self.action_spaces.append(spaces.Box(-1, 1, (len(continuous_actions),)))
            self.action_names.append(continuous_actions)
        if len(multidiscrete_actions) > 0:
            self.action_spaces.append(spaces.MultiDiscrete(multidiscrete_action_ranges))
            self.action_names.append(multidiscrete_actions)

        # if there is only one action space, don't wrap it in Tuple
        if len(self.action_spaces) == 1:
            self.action_space = self.action_spaces[0]
        else:
            self.action_space = spaces.Tuple(self.action_spaces)
        self.logger.debug(self.action_space)

    def _take_action(self, actions, world_state):
        # if there is only one action space, it wasn't wrapped in Tuple
        if len(self.action_spaces) == 1:
            actions = [actions]

        # send appropriate command for different actions
        for spc, cmds, acts in zip(self.action_spaces, self.action_names, actions):
            if isinstance(spc, spaces.Discrete):
                self.logger.debug(cmds[acts])
                self._send_command(cmds[acts], world_state)
            elif isinstance(spc, spaces.Box):
                for cmd, val in zip(cmds, acts):
                    self.logger.debug(cmd + " " + str(val))
                    self._send_command(cmd + " " + str(val), world_state)
            elif isinstance(spc, spaces.MultiDiscrete):
                for cmd, val in zip(cmds, acts):
                    self.logger.debug(cmd + " " + str(val))
                    self._send_command(cmd + " " + str(val), world_state)
            else:
                self.logger.warning("Unknown action space for %s, ignoring." % cmds)

    def _send_command(self, command: str, world_state):
        """
        Sends a command to the agent host.

        :param command:
        :param world_state:
        :return:
        """
        self.agent_host.sendCommand(command)

    def _get_world_state(self, ignore_rewards=False):
        # wait till we have got at least one observation or mission has ended
        while True:
            time.sleep(self.step_sleep)  # wait for 1ms to not consume entire CPU
            world_state = self.agent_host.peekWorldState()
            if ((len(world_state.rewards) > 0 or ignore_rewards)
                and world_state.number_of_observations_since_last_state > self.skip_steps) \
                    or not world_state.is_mission_running:
                break

        return self.agent_host.getWorldState()

    def _peek_valid_world_state(self):
        # wait till we have got at least one observation or mission has ended
        while True:
            time.sleep(self.step_sleep)  # wait for 1ms to not consume entire CPU
            world_state = self.agent_host.peekWorldState()
            if (len(world_state.rewards) > 0 and world_state.number_of_observations_since_last_state > self.skip_steps) \
                    or not world_state.is_mission_running:
                break

        return world_state

    def _get_video_frame(self, world_state):
        # process the video frame
        if world_state.number_of_video_frames_since_last_state > 0:
            assert len(world_state.video_frames) == 1
            frame = world_state.video_frames[-1]
            image = np.frombuffer(frame.pixels, dtype=np.uint8)
            image = image.reshape((frame.height, frame.width, frame.channels))
            # self.logger.debug(image)
            self.last_image = image
        else:
            # can happen only when mission ends before we get frame
            # then just use the last frame, it doesn't matter much anyway
            image = self.last_image

        return image

    def _get_observation(self, world_state):
        """
        Fetches observations from the world state, and if there are no new observations, it returns the last
        observation.

        :param world_state:
        :return:
        """

        if world_state.number_of_observations_since_last_state > 0 and world_state.is_mission_running:
            missed = world_state.number_of_observations_since_last_state - len(
                world_state.observations) - self.skip_steps
            if missed > 0:
                self.logger.debug("Agent missed %d observation(s).", missed)
            assert len(world_state.observations) == 1
            observations = json.loads(world_state.observations[-1].text)
            self.previous_observations = observations
            return observations
        else:
            return self.previous_observations

    def _update_replay_buffer_and_get_observation(self, observation: np.ndarray):
        """
        Updates the replay buffer (analogous to a queue).

        :param observation:
        :return:
        """

        self.replay_buffer.insert(0, observation)
        self.replay_buffer.pop(-1)

        return np.vstack(self.replay_buffer)

    def step(self, action):
        # take the action only if mission is still running
        world_state = self.agent_host.peekWorldState()
        if world_state.is_mission_running:
            # take action
            self._take_action(action, world_state)

            self.num_actions += 1
            # wait for the new state
            world_state = self._get_world_state()

        # log errors and control messages
        for error in world_state.errors:
            self.logger.warning(error.text)
        for msg in world_state.mission_control_messages:
            self.logger.debug(msg.text)
            root = ET.fromstring(msg.text)
            if root.tag == '{http://ProjectMalmo.microsoft.com}MissionEnded':
                for el in root.findall('{http://ProjectMalmo.microsoft.com}HumanReadableStatus'):
                    self.logger.info("Mission ended: %s", el.text)

        # detect terminal state
        done = not world_state.is_mission_running

        # other auxiliary data
        info = {}
        info['has_mission_begun'] = world_state.has_mission_begun
        info['is_mission_running'] = world_state.is_mission_running
        info['number_of_video_frames_since_last_state'] = world_state.number_of_video_frames_since_last_state
        info['number_of_rewards_since_last_state'] = world_state.number_of_rewards_since_last_state
        info['number_of_observations_since_last_state'] = world_state.number_of_observations_since_last_state
        info['mission_control_messages'] = [msg.text for msg in world_state.mission_control_messages]
        info['observation'] = self._get_observation(world_state)

        if self.parse_world_state:
            obs, reward = self._world_state_parser(world_state)
        else:
            # take the last frame from world state
            reward = sum([r.getValue() for r in world_state.rewards])
            obs_frame = self._get_video_frame(world_state)
            obs = self._update_replay_buffer_and_get_observation(obs_frame)
        if done:
            self.logger.info("Number of actions in iteration {}".format(self.num_actions))
        return obs, reward, done, info

    def render(self, mode='human', close=False):
        pass

    def reset(self, force_reset=False):

        self.num_actions = 0

        self.mission_spec = self._load_mission()

        # force new world each time
        if self.forceWorldReset or force_reset:
            self.mission_spec.forceWorldReset()
        elif self._num_resets % self._reset_counter == 0:
            logging.info("Forcing WORLD RESET reset_counter {} ".format(self._num_resets))
            self.mission_spec.forceWorldReset()

        self._num_resets += 1

        # this seemed to increase probability of success in first try
        time.sleep(0.1)
        # Attempt to start a mission
        for retry in range(self.max_retries + 1):
            try:
                if self.client_pool:
                    self.agent_host.startMission(self.mission_spec, self.client_pool, self.mission_record_spec, 0,
                                                 "experiment_id")
                else:
                    self.agent_host.startMission(self.mission_spec, self.mission_record_spec)
                break
            except RuntimeError as e:
                if retry == self.max_retries:
                    self.logger.error("Error starting mission: " + str(e))
                    raise
                else:
                    self.logger.warning("Error starting mission: " + str(e))
                    self.logger.info("Sleeping for %d seconds...", self.retry_sleep)
                    time.sleep(self.retry_sleep)

        # Loop until mission starts:
        self.logger.info("Waiting for the mission to start")
        world_state = self.agent_host.getWorldState()
        while not world_state.has_mission_begun:
            time.sleep(0.1)
            world_state = self.agent_host.getWorldState()
            for error in world_state.errors:
                self.logger.warning(error.text)

        self._init_replay_buffer()

        self.logger.info("Mission running")
        self.logger.info("Collecting First Observation")
        world_state = self._get_world_state(ignore_rewards=True)

        if self.parse_world_state:
            obs, _ = self._world_state_parser(world_state)
            return obs
        else:
            obs_frame = self._get_video_frame(world_state)
            obs = self._update_replay_buffer_and_get_observation(obs_frame)
            return obs

    def close(self):
        if hasattr(self, 'mc_process') and self.mc_process:
            minecraft_py.stop(self.mc_process)

    def seed(self, seed=None):
        self.mission_spec.setWorldSeed(str(seed))
        return [seed]

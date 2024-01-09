# Copyright (C) 2022 Istituto Italiano di Tecnologia (IIT). All rights reserved.
# This software may be modified and distributed under the terms of the
# Released under the terms of the BSD 3-Clause License

import sys
import time
import math
import h5py
import numpy as np
from PyQt5.QtCore import pyqtSignal, QThread, QMutex, QMutexLocker
from robot_log_visualizer.utils.utils import PeriodicThreadState

# for real-time logging
import yarp
import json


class TextLoggingMsg:
    def __init__(self, level, text):
        self.level = level
        self.text = text

    def color(self):
        if self.level == "ERROR":
            return "#d62728"
        elif self.level == "WARNING":
            return "#ff7f0e"
        elif self.level == "DEBUG":
            return "#1f77b4"
        elif self.level == "INFO":
            return "#2ca02c"
        else:
            return "black"


class SignalProvider(QThread):
    update_index_signal = pyqtSignal()

    def __init__(self, period: float):
        QThread.__init__(self)

        # set device state
        self._state = PeriodicThreadState.pause
        self.state_lock = QMutex()

        self._index = 0
        self.index_lock = QMutex()

        self.period = period

        self.data = {}
        self.timestamps = np.array([])
        self.text_logging_data = {}

        self.initial_time = math.inf
        self.end_time = -math.inf

        self.joints_name = []
        self.robot_name = ""

        self.root_name = "robot_logger_device"

        self._current_time = 0

        self.realtimeBufferReached = False

        # for networking with the real-time logger
        self.networkInit = False

    def __populate_text_logging_data(self, file_object):
        data = {}
        for key, value in file_object.items():
            if not isinstance(value, h5py._hl.group.Group):
                continue
            if key == "#refs#":
                continue
            if "data" in value.keys():
                data[key] = {}
                level_ref = value["data"]["level"]
                text_ref = value["data"]["text"]

                data[key]["timestamps"] = np.squeeze(np.array(value["timestamps"]))

                # New way to store the struct array in robometry https://github.com/robotology/robometry/pull/175
                if text_ref.shape[0] == len(data[key]["timestamps"]):
                    # If len(value[text[0]].shape) == 2 then the text contains a string, otherwise it is empty
                    # We need to manually check the shape to handle the case in which the text is empty
                    data[key]["data"] = [
                        TextLoggingMsg(
                            text="".join(chr(c[0]) for c in value[text[0]]),
                            level="".join(chr(c[0]) for c in value[level[0]]),
                        )
                        if len(value[text[0]].shape) == 2
                        else TextLoggingMsg(
                            text="",
                            level="".join(chr(c[0]) for c in value[level[0]]),
                        )
                        for text, level in zip(text_ref, level_ref)
                    ]

                # Old approach (before https://github.com/robotology/robometry/pull/175)
                else:
                    data[key]["data"] = [
                        TextLoggingMsg(
                            text="".join(chr(c[0]) for c in value[text]),
                            level="".join(chr(c[0]) for c in value[level]),
                        )
                        for text, level in zip(text_ref[0], level_ref[0])
                    ]

            else:
                data[key] = self.__populate_text_logging_data(file_object=value)

        return data

    def __populate_numerical_data(self, file_object):
        data = {}
        for key, value in file_object.items():
            if not isinstance(value, h5py._hl.group.Group):
                continue
            if key == "#refs#":
                print("Skipping for refs")
                continue
            if key == "log":
                print("Skipping for log")
                continue
            if "data" in value.keys():
                data[key] = {}
                data[key]["data"] = np.squeeze(np.array(value["data"]))
                data[key]["timestamps"] = np.squeeze(np.array(value["timestamps"]))

                # if the initial or end time has been updated we can also update the entire timestamps dataset
                if data[key]["timestamps"][0] < self.initial_time:
                    self.timestamps = data[key]["timestamps"]
                    self.initial_time = self.timestamps[0]

                if data[key]["timestamps"][-1] > self.end_time:
                    self.timestamps = data[key]["timestamps"]
                    self.end_time = self.timestamps[-1]

                # In yarp telemetry v0.4.0 the elements_names was saved.
                if "elements_names" in value.keys():
                    elements_names_ref = value["elements_names"]
                    data[key]["elements_names"] = [
                        "".join(chr(c[0]) for c in value[ref])
                        for ref in elements_names_ref[0]
                    ]
                
            else:
                data[key] = self.__populate_numerical_data(file_object=value)

        return data

    def __populateRealtimeLoggerData(self, rawData, input):
        data = {}
        for key, value in input.items():
            if key not in rawData.keys():
                rawData[key] = value
            elif key == "description_list" or key == "yarp_robot_name":
                continue
 
            if value is None:
                continue

            if "data" in value.keys() and "timestamps" in value.keys():
                data[key] = {}
                rawData[key]["data"] = np.append(rawData[key]["data"], np.array(value["data"])).reshape(-1, len(value["data"]))
                rawData[key]["timestamps"] = np.append(rawData[key]["timestamps"], np.array(value["timestamps"]))

                if rawData[key]["timestamps"][0] < self.initial_time:
                    self.timestamps = rawData[key]["timestamps"]
                    self.initial_time = self.timestamps[0]

                if rawData[key]["timestamps"][-1] > self.end_time:
                    self.timestamps = rawData[key]["timestamps"]
                    self.end_time = self.timestamps[-1]

                if self.end_time - self.initial_time >= 20:
                    self.realtimeBufferReached = True
                    tempInitialTime = self.initial_time
                    tempEndTime = self.end_time
                    while tempEndTime - tempInitialTime >= 20:
                        rawData[key]["data"] = np.delete(rawData[key]["data"], 0, axis=0)
                        rawData[key]["timestamps"] = np.delete(rawData[key]["timestamps"], 0)
                        tempInitialTime = rawData[key]["timestamps"][0]
                        tempEndTime = rawData[key]["timestamps"][-1]

                if "elements_names" in value.keys():
                    rawData[key]["elements_names"] = value["elements_names"]


            else:
                data[key] = self.__populateRealtimeLoggerData(rawData=rawData[key],input=value)

        return data
        

    def establish_connection(self):
        self.root_name = "robot_realtime"
        self.initalFrame = True
        if not self.networkInit:
            yarp.Network.init()
            self.loggingInput = yarp.BufferedPortBottle()
            self.loggingInput.open("/visualizerInput")
            yarp.Network.connect("/testLoggerOutput", "/visualizerInput")
            
            self.networkInit = True
        success = self.loggingInput.read()
        if not success:
            print("Failed to read realtime YARP port, closing")
            sys.exit(1)
        else:
            rawInput = str(success.toString())

            # json.loads is done twice, the 1st time is to remove escape characters
            # the 2nd time actually converts the string to the dictionary
            input = json.loads(json.loads(rawInput))
            self.__populateRealtimeLoggerData(self.data, input)
            if self.realtimeBufferReached:
                self.initial_time = self.timestamps[0]
                self.end_time = self.timestamps[-1]
                self.timestamps = np.delete(self.timestamps, 0)
                self.realtimeBufferReached = False
            self.joints_name = self.data["robot_realtime"]["description_list"]

    def open_mat_file(self, file_name: str):
        with h5py.File(file_name, "r") as file:

            root_variable = file.get(self.root_name)
            self.data = self.__populate_numerical_data(file)

            if "log" in root_variable.keys():
                self.text_logging_data["log"] = self.__populate_text_logging_data(
                    root_variable["log"]
                )

            for name in file.keys():
                if "description_list" in file[name].keys():
                    self.root_name = name
                    break

            joint_ref = root_variable["description_list"]
            self.joints_name = [
                "".join(chr(c[0]) for c in file[ref]) for ref in joint_ref[0]
            ]
            if "yarp_robot_name" in root_variable.keys():
                robot_name_ref = root_variable["yarp_robot_name"]
                try:
                    self.robot_name = "".join(chr(c[0]) for c in robot_name_ref)
                except:
                    pass
            self.index = 0

    def __len__(self):
        return self.timestamps.shape[0]

    @property
    def state(self):
        locker = QMutexLocker(self.state_lock)
        value = self._state
        return value

    @state.setter
    def state(self, new_state: PeriodicThreadState):
        locker = QMutexLocker(self.state_lock)
        self._state = new_state

    @property
    def index(self):
        locker = QMutexLocker(self.index_lock)
        value = self._index
        return value

    @index.setter
    def index(self, index):
        locker = QMutexLocker(self.index_lock)
        self._index = index

    def register_update_index(self, slot):
        self.update_index_signal.connect(slot)

    def set_dataset_percentage(self, percentage):
        self.update_index(int(percentage * len(self)))

    def update_index(self, index):
        locker = QMutexLocker(self.index_lock)
        self._index = max(min(index, len(self.timestamps) - 1), 0)
        self._current_time = self.timestamps[self._index] - self.initial_time

    @property
    def current_time(self):
        locker = QMutexLocker(self.index_lock)
        value = self._current_time
        return value

    def get_joints_position(self):
        return self.data[self.root_name]["joints_state"]["positions"]["data"]

    def get_joints_position_at_index(self, index):
        joints_position_timestamps = self.data[self.root_name]["joints_state"][
            "positions"
        ]["timestamps"]
        # given the index find the closest timestamp
        closest_index = np.argmin(
            np.abs(joints_position_timestamps - self.timestamps[index])
        )
        return self.get_joints_position()[closest_index, :]

    def run(self):
        while True:
            start = time.time()
            if self.state == PeriodicThreadState.running:
                self.index_lock.lock()
                tmp_index = self._index
                self._current_time += self.period
                self._current_time = min(
                    self._current_time, self.timestamps[-1] - self.initial_time
                )

                # find the index associated to the current time in self.timestamps
                # this is valid since self.timestamps is sorted and self._current_time is increasing
                while (
                    self._current_time > self.timestamps[tmp_index] - self.initial_time
                ):
                    tmp_index += 1
                    if tmp_index > len(self.timestamps):
                        break

                self._index = tmp_index

                self.index_lock.unlock()

                self.update_index_signal.emit()

            sleep_time = self.period - (time.time() - start)
            if sleep_time > 0:
                time.sleep(sleep_time)

            if self.state == PeriodicThreadState.closed:
                return

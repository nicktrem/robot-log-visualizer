# Copyright (C) 2022 Istituto Italiano di Tecnologia (IIT). All rights reserved.
# This software may be modified and distributed under the terms of the
# Released under the terms of the BSD 3-Clause License

import time
import h5py
import numpy as np
from PyQt5.QtCore import pyqtSignal, QThread, QMutex, QMutexLocker
from robot_log_visualizer.signal_provider.signal_provider import (
    SignalProvider,
    ProviderType,
    TextLoggingMsg,
)
from robot_log_visualizer.utils.utils import PeriodicThreadState, RobotStatePath


class MatfileSignalProvider(SignalProvider):
    def __init__(self, period: float, signal_root_name: str):
        super().__init__(period, signal_root_name, ProviderType.OFFLINE)

    def __len__(self):
        return len(self.timestamps)

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
                        (
                            TextLoggingMsg(
                                text="".join(chr(c[0]) for c in value[text[0]]),
                                level="".join(chr(c[0]) for c in value[level[0]]),
                            )
                            if len(value[text[0]].shape) == 2
                            else TextLoggingMsg(
                                text="",
                                level="".join(chr(c[0]) for c in value[level[0]]),
                            )
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
                continue
            if key == "log":
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

    def open(self, source: str) -> bool:
        with h5py.File(source, "r") as file:
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

        return True

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

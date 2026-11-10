"""Which mic he listens through: the dashboard's choice, and whether the robot is told to stream
its mic. No audio devices: a stand-in for Ears."""

import asyncio
import unittest
from unittest.mock import patch

from brain import config, main, settings


class FakeEars:
    def __init__(self, missing=None):
        self.source = "robot"

    def set_source(self, source):
        self.source = source

    def apply_config(self):
        pass


class MicChoiceTests(unittest.TestCase):
    def setUp(self):
        self.saved = (config.MIC_SOURCE, main.ears, main.robot_socket)
        self.sent = []

        async def send(msg):
            self.sent.append(msg)
        self.patch = patch.object(main, "send_to_robot", send)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        config.MIC_SOURCE, main.ears, main.robot_socket = self.saved

    def choose(self, mic):
        s = settings.defaults(); s["rocky"].update(mic=mic)
        main.apply_settings(s)

    def test_settings_map_to_config(self):
        self.choose("robot")
        self.assertEqual(config.MIC_SOURCE, "robot")
        self.choose("auto")
        self.assertEqual(config.MIC_SOURCE, "auto")
        with self.assertRaises(ValueError):
            settings.clean_value("rocky", "mic", "computer")   # the fork's choices: no computer mic

    def test_pick_source(self):
        main.ears = FakeEars()
        for mic, robot, want in (("auto", True, "robot"), ("auto", False, "robot"),
                                 ("robot", False, "robot"), ("robot", True, "robot")):
            self.choose(mic)
            main.robot_socket = object() if robot else None
            main.pick_mic_source()
            self.assertEqual(main.ears.source, want, (mic, robot))

    def test_robot_streams_only_when_listened_to(self):
        main.ears = FakeEars(); main.robot_socket = object()
        self.choose("robot"); main.pick_mic_source(); asyncio.run(main.update_robot_mic())
        self.choose("auto"); main.pick_mic_source(); asyncio.run(main.update_robot_mic())
        self.assertEqual(self.sent, [{"type": "mic", "on": True}, {"type": "mic", "on": True}])
        main.ears.source = "browser"   # someone else's mic is live: the robot's goes quiet
        asyncio.run(main.update_robot_mic())
        self.assertEqual(self.sent[-1], {"type": "mic", "on": False})


if __name__ == "__main__":
    unittest.main()

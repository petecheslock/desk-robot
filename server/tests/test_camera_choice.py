"""Whose camera he sees through: the robot's frames are turned by the robot's own settings (the
V2 head's lens is on its side), whatever else the browser may later push. No real camera."""

import unittest

from brain import config, main


def _tiny_jpeg() -> bytes:
    """A 64-wide, 48-tall JPEG (asymmetric, so a turn shows up in the shape)."""
    import cv2
    import numpy as np
    img = np.zeros((48, 64, 3), np.uint8)
    img[:24] = 255        # top half white: up and down are told apart
    return cv2.imencode(".jpg", img)[1].tobytes()


class CameraChoiceTests(unittest.TestCase):
    def test_robot_frames_turn_by_the_robots_own_setting(self):
        # Everything as a fresh checkout: the robot's frames turn by the robot's setting (270° on a
        # V2 head), not by nothing: a 64-wide x 48-tall frame comes back tall.
        saved = (config.CAMERA_ROTATE, config.CAMERA_MIRROR)
        try:
            config.CAMERA_ROTATE, config.CAMERA_MIRROR = 270, False
            main.eyes.jpeg, main.eyes.frame_at = b"", 0.0
            main.eyes.push_frame(_tiny_jpeg())
            import cv2
            import numpy as np
            got = cv2.imdecode(np.frombuffer(main.eyes.latest(max_age=5.0), np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(got.shape[:2], (64, 48))   # was 48 tall x 64 wide: the robot's turn happened
        finally:
            config.CAMERA_ROTATE, config.CAMERA_MIRROR = saved

    def test_browser_frames_go_through_untouched(self):
        # The dashboard's webcam is upright already (Phase 4 of the fork pushes these): no turn, no mirror.
        saved = (config.CAMERA_ROTATE, config.CAMERA_MIRROR)
        try:
            config.CAMERA_ROTATE, config.CAMERA_MIRROR = 270, True
            main.eyes.jpeg, main.eyes.frame_at = b"", 0.0
            main.eyes.push_frame(_tiny_jpeg(), "browser")
            import cv2
            import numpy as np
            got = cv2.imdecode(np.frombuffer(main.eyes.latest(max_age=5.0), np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual(got.shape[:2], (48, 64))   # upright, as the webcam saw it
        finally:
            config.CAMERA_ROTATE, config.CAMERA_MIRROR = saved


if __name__ == "__main__":
    unittest.main()

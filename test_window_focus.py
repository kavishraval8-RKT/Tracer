import unittest

from sidecar_app import _rect_is_minimized, foreground_is_allowed


class ForegroundFocusTests(unittest.TestCase):
    def test_foreground_is_allowed_only_for_kicad_or_tracer(self) -> None:
        self.assertTrue(foreground_is_allowed(10, {10}, {20}))
        self.assertTrue(foreground_is_allowed(20, {10}, {20}))
        self.assertFalse(foreground_is_allowed(30, {10}, {20}))
        self.assertFalse(foreground_is_allowed(20, set(), {20}))

    def test_off_screen_rect_counts_as_minimized(self) -> None:
        self.assertTrue(_rect_is_minimized(-32000, -32000))
        self.assertFalse(_rect_is_minimized(100, 200))
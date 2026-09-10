#!/usr/bin/env python3

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from standalone_pet import (
    AFFECTION_DEFAULT,
    AFFECTION_MAX,
    DRAG_ALERT_TEXT,
    DRAG_POSE_INDEX,
    LANDING_TEXT,
    STATE_INFO_TEXT,
    alert_markup,
    centered_popup_x,
    clamp_affection,
    danger_speed_multiplier,
    decrease_to_floor,
    drag_pose_for_delta,
    elapsed_periods,
    heart_effect_origin,
    hover_dismount_ready,
    low_affection_tint,
    key_hits_lock,
    shortest_cycle_path,
    should_resume_held_drag,
    timed_affection_loss,
    window_state_hides_auxiliary,
)


class DragPoseTests(unittest.TestCase):
    def test_left_drag_lags_to_right_apex(self) -> None:
        self.assertEqual(drag_pose_for_delta(-8, 0), "right_apex")

    def test_right_drag_lags_to_left_apex(self) -> None:
        self.assertEqual(drag_pose_for_delta(8, 0), "left_apex")

    def test_vertical_drag_uses_bottom(self) -> None:
        self.assertEqual(drag_pose_for_delta(0, -8), "bottom")
        self.assertEqual(drag_pose_for_delta(0, 8), "bottom")

    def test_diagonal_drag_prioritizes_horizontal(self) -> None:
        self.assertEqual(drag_pose_for_delta(-3, 20), "right_apex")
        self.assertEqual(drag_pose_for_delta(3, -20), "left_apex")

    def test_subpixel_jitter_keeps_current_pose(self) -> None:
        self.assertIsNone(drag_pose_for_delta(0.5, -0.5))

    def test_resume_indices_have_apex_holds(self) -> None:
        self.assertEqual(DRAG_POSE_INDEX, {"bottom": 0, "right_apex": 6, "left_apex": 13})

    def test_apex_to_apex_uses_intermediate_gif_frames(self) -> None:
        self.assertEqual(shortest_cycle_path(6, 13), [7, 8, 9, 10, 11, 12, 13])
        self.assertEqual(shortest_cycle_path(13, 6), [12, 11, 10, 9, 8, 7, 6])

    def test_vertical_target_uses_shortest_physical_path(self) -> None:
        self.assertEqual(shortest_cycle_path(6, 0), [5, 4, 3, 2, 1, 0])
        self.assertEqual(shortest_cycle_path(13, 0), [14, 15, 16, 0])

    def test_status_text_is_concise(self) -> None:
        self.assertEqual(STATE_INFO_TEXT["working"], "工作中")
        self.assertEqual(STATE_INFO_TEXT["waiting"], "思考中")
        self.assertEqual(STATE_INFO_TEXT["idle"], "空闲中")
        self.assertEqual(DRAG_ALERT_TEXT, "WOC ！！！")
        self.assertEqual(LANDING_TEXT, "咋滴啊？")

    def test_drag_and_landing_alerts_share_the_same_emphasis(self) -> None:
        drag_markup = alert_markup(DRAG_ALERT_TEXT)
        landing_markup = alert_markup(LANDING_TEXT)
        self.assertEqual(
            drag_markup.replace(DRAG_ALERT_TEXT, ""),
            landing_markup.replace(LANDING_TEXT, ""),
        )
        self.assertIn('foreground="#FFE600"', landing_markup)
        self.assertIn('size="xx-large"', landing_markup)
        self.assertIn('weight="bold"', landing_markup)

    def test_held_drag_resumes_only_when_still_and_settled(self) -> None:
        self.assertFalse(should_resume_held_drag(109, False))
        self.assertFalse(should_resume_held_drag(500, True))
        self.assertTrue(should_resume_held_drag(110, False))


class AffectionTests(unittest.TestCase):
    def test_default_and_cap(self) -> None:
        self.assertEqual(AFFECTION_DEFAULT, 100)
        self.assertEqual(AFFECTION_MAX, 1000)
        self.assertEqual(clamp_affection(1001), 1000)
        self.assertEqual(clamp_affection(-1), 0)

    def test_drag_loses_five_points_each_complete_second(self) -> None:
        loss, carry = timed_affection_loss(0.6, 0.3, 5)
        self.assertEqual(loss, 0)
        self.assertAlmostEqual(carry, 0.9)
        loss, carry = timed_affection_loss(0.2, carry, 5)
        self.assertEqual(loss, 5)
        self.assertAlmostEqual(carry, 0.1)
        loss, carry = timed_affection_loss(2.2, carry, 5)
        self.assertEqual(loss, 10)
        self.assertAlmostEqual(carry, 0.3)

    def test_standing_loses_one_point_each_complete_second(self) -> None:
        loss, carry = timed_affection_loss(1.0, 0.0, 1)
        self.assertEqual(loss, 1)
        self.assertAlmostEqual(carry, 0.0)

    def test_drag_and_standing_decay_stop_at_five(self) -> None:
        self.assertEqual(decrease_to_floor(100, 5), 95)
        self.assertEqual(decrease_to_floor(7, 5), 5)
        self.assertEqual(decrease_to_floor(5, 5), 5)
        self.assertEqual(decrease_to_floor(3, 5), 3)

    def test_idle_decay_counts_every_five_second_period(self) -> None:
        self.assertEqual(elapsed_periods(4.99, 5.0, 5.0), 0)
        self.assertEqual(elapsed_periods(5.0, 5.0, 5.0), 1)
        self.assertEqual(elapsed_periods(15.0, 5.0, 5.0), 3)

    def test_hover_needs_one_uninterrupted_second(self) -> None:
        self.assertFalse(hover_dismount_ready(0.99, 0.0, True, False, "swing"))
        self.assertTrue(hover_dismount_ready(1.0, 0.0, True, False, "swing"))
        self.assertFalse(hover_dismount_ready(3.0, 0.0, True, True, "swing"))
        self.assertFalse(hover_dismount_ready(3.0, None, True, False, "swing"))

    def test_danger_speed_increases_every_half_second(self) -> None:
        self.assertEqual(danger_speed_multiplier(6, 99.0), 1.0)
        self.assertAlmostEqual(danger_speed_multiplier(5, 0.25), 1.075)
        self.assertAlmostEqual(danger_speed_multiplier(5, 0.50), 1.15)
        self.assertAlmostEqual(danger_speed_multiplier(4, 0.75), 1.225)
        self.assertAlmostEqual(danger_speed_multiplier(4, 1.00), 1.30)
        self.assertAlmostEqual(danger_speed_multiplier(1, 2.00), 1.60)
        self.assertEqual(danger_speed_multiplier(0, 99.0), 1.0)

    def test_danger_speed_has_no_half_second_jump(self) -> None:
        before = danger_speed_multiplier(5, 0.5 - 0.000001)
        after = danger_speed_multiplier(5, 0.5 + 0.000001)
        self.assertLess(abs(after - before), 0.000001)

    def test_low_affection_has_ten_progressive_red_levels(self) -> None:
        self.assertEqual(low_affection_tint(11), 0.0)
        self.assertAlmostEqual(low_affection_tint(10), 0.1)
        self.assertAlmostEqual(low_affection_tint(9), 0.2)
        self.assertAlmostEqual(low_affection_tint(1), 1.0)
        self.assertAlmostEqual(low_affection_tint(0), 1.0)

    def test_key_unlocks_only_on_center_lock(self) -> None:
        self.assertTrue(key_hits_lock(960, 540, 1920, 1080))
        self.assertTrue(key_hits_lock(1010, 540, 1920, 1080))
        self.assertFalse(key_hits_lock(1200, 540, 1920, 1080))
        self.assertFalse(key_hits_lock(1800, 950, 1920, 1080))

    def test_popup_centers_on_visible_pet_not_full_canvas(self) -> None:
        self.assertEqual(centered_popup_x(100, 217, 120), 257)
        self.assertEqual(centered_popup_x(100, 160, 120), 200)

    def test_heart_starts_just_right_of_head_and_overlaps_pet(self) -> None:
        # Window origin + fixed head center + 20px offset - 28px heart center.
        self.assertEqual(heart_effect_origin(100, 200, 217, 208, 1.0), (309, 237))

    def test_show_desktop_states_hide_auxiliary_windows(self) -> None:
        self.assertTrue(window_state_hides_auxiliary(1))  # WITHDRAWN
        self.assertTrue(window_state_hides_auxiliary(2))  # ICONIFIED
        self.assertTrue(window_state_hides_auxiliary(3))
        self.assertFalse(window_state_hides_auxiliary(0))


if __name__ == "__main__":
    unittest.main()

"""
apps/attendance/tests.py
═══════════════════════════════════════════════════════════════════════════════
Unit tests for attendance hour / lateness / undertime calculation.

Run with:
    python manage.py test attendance

These test the pure functions in attendance/utils.py directly — no
database fixtures needed, so they run in milliseconds.
═══════════════════════════════════════════════════════════════════════════════
"""

from datetime import time
from decimal import Decimal

from django.test import TestCase

from .utils import (
    time_diff,
    calculate_total_hours,
    calculate_late_minutes,
    calculate_undertime_minutes,
)


class TimeDiffTests(TestCase):
    """
    Tests for the core time_diff() helper. This is the single function
    that makes or breaks every hours calculation in the system — if it's
    wrong, every payslip downstream is wrong.
    """

    def test_simple_same_day_diff(self):
        diff = time_diff(time(8, 0, 0), time(12, 0, 0))
        self.assertEqual(diff.total_seconds(), 4 * 3600)

    def test_zero_diff_same_time(self):
        diff = time_diff(time(9, 0, 0), time(9, 0, 0))
        self.assertEqual(diff.total_seconds(), 0)

    def test_overnight_shift_with_flag(self):
        """23:00 → 01:30 should be 2.5 hours when allow_overnight=True."""
        diff = time_diff(time(23, 0, 0), time(1, 30, 0), allow_overnight=True)
        self.assertAlmostEqual(diff.total_seconds(), 2.5 * 3600)

    def test_overnight_shift_without_flag_returns_zero(self):
        """
        Without allow_overnight, an apparent 'negative' diff must return
        zero — never a negative duration that could corrupt total_hours.
        """
        diff = time_diff(time(23, 0, 0), time(1, 30, 0), allow_overnight=False)
        self.assertEqual(diff.total_seconds(), 0)

    def test_accepts_string_times(self):
        diff = time_diff('08:00:00', '17:00:00')
        self.assertEqual(diff.total_seconds(), 9 * 3600)

    def test_equal_times_overnight_flag_still_zero(self):
        """Equal start/end must be 0, not misread as a full 24h wraparound."""
        diff = time_diff(time(0, 0, 0), time(0, 0, 0), allow_overnight=True)
        self.assertEqual(diff.total_seconds(), 0)

    def test_long_overnight_shift(self):
        """20:00 -> 06:00 (10-hour graveyard shift) computes correctly."""
        diff = time_diff(time(20, 0, 0), time(6, 0, 0), allow_overnight=True)
        self.assertEqual(diff.total_seconds(), 10 * 3600)


class CalculateTotalHoursTests(TestCase):
    """Tests for calculate_total_hours() — combines AM and PM halves."""

    def test_standard_8_hour_day(self):
        hours = calculate_total_hours(
            time(8, 0, 0), time(12, 0, 0),
            time(13, 0, 0), time(17, 0, 0),
        )
        self.assertEqual(hours, Decimal('8.0000'))

    def test_only_first_half_worked(self):
        hours = calculate_total_hours(
            time(8, 0, 0), time(12, 0, 0),
            None, None,
        )
        self.assertEqual(hours, Decimal('4.0000'))

    def test_only_second_half_worked(self):
        hours = calculate_total_hours(
            None, None,
            time(13, 0, 0), time(17, 0, 0),
        )
        self.assertEqual(hours, Decimal('4.0000'))

    def test_no_time_logged_returns_zero(self):
        hours = calculate_total_hours(None, None, None, None)
        self.assertEqual(hours, Decimal('0.0000'))

    def test_overnight_second_half_shift(self):
        """
        A PM shift that crosses midnight (e.g. 22:00 → 02:00) must count
        as 4 hours, not 0 and not a negative value. This is the exact
        bug pattern that silently zeroed out night-shift pay before the fix.
        """
        hours = calculate_total_hours(
            None, None,
            time(22, 0, 0), time(2, 0, 0),
        )
        self.assertEqual(hours, Decimal('4.0000'))

    def test_both_halves_with_one_overnight(self):
        hours = calculate_total_hours(
            time(8, 0, 0), time(12, 0, 0),   # 4h
            time(22, 0, 0), time(1, 0, 0),   # 3h overnight
        )
        self.assertEqual(hours, Decimal('7.0000'))

    def test_partial_minutes_rounding(self):
        hours = calculate_total_hours(
            time(8, 0, 0), time(12, 15, 0),
            None, None,
        )
        # 4 hours 15 minutes = 4.25 hours
        self.assertEqual(hours, Decimal('4.2500'))

    def test_accepts_string_time_inputs(self):
        hours = calculate_total_hours(
            '08:00:00', '12:00:00',
            '13:00:00', '17:00:00',
        )
        self.assertEqual(hours, Decimal('8.0000'))

    def test_accepts_browser_style_hh_mm_strings(self):
        """
        REGRESSION TEST: a previous version of this code padded short
        'HH:MM' strings with trailing zeros instead of inserting ':00',
        which crashed on exactly this input. 'HH:MM' (no seconds) is
        what <input type="time"> and most manual-entry forms actually
        submit, so this must never break again.
        """
        hours = calculate_total_hours('08:00', '12:00', '13:00', '17:00')
        self.assertEqual(hours, Decimal('8.0000'))

    def test_hh_mm_overnight_shift_does_not_crash(self):
        hours = calculate_total_hours(None, None, '22:00', '02:00')
        self.assertEqual(hours, Decimal('4.0000'))


class LateMinutesTests(TestCase):

    def test_on_time_no_late_minutes(self):
        self.assertEqual(calculate_late_minutes(time(8, 0, 0)), 0)

    def test_early_arrival_no_late_minutes(self):
        self.assertEqual(calculate_late_minutes(time(7, 45, 0)), 0)

    def test_late_arrival_minutes_counted(self):
        self.assertEqual(calculate_late_minutes(time(8, 15, 0)), 15)

    def test_no_time_in_returns_zero(self):
        self.assertEqual(calculate_late_minutes(None), 0)

    def test_custom_workday_start(self):
        self.assertEqual(
            calculate_late_minutes(time(9, 10, 0), workday_start='09:00:00'),
            10,
        )

    def test_accepts_browser_style_hh_mm_string(self):
        """REGRESSION TEST -- see test_accepts_browser_style_hh_mm_strings above."""
        self.assertEqual(calculate_late_minutes('08:15'), 15)


class UndertimeMinutesTests(TestCase):

    def test_on_time_no_undertime(self):
        self.assertEqual(calculate_undertime_minutes(time(17, 0, 0)), 0)

    def test_left_early_undertime_counted(self):
        self.assertEqual(calculate_undertime_minutes(time(16, 30, 0)), 30)

    def test_left_late_no_undertime(self):
        self.assertEqual(calculate_undertime_minutes(time(18, 0, 0)), 0)

    def test_no_time_out_returns_zero(self):
        self.assertEqual(calculate_undertime_minutes(None), 0)

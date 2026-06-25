"""
apps/attendance/utils.py  (NEW FILE)
═══════════════════════════════════════════════════════════════════════════════
Pure calculation helpers for attendance hours, lateness, and undertime.

Extracted from views.py so they:
  1. Can be unit-tested without needing Django request/view context
  2. Are reused consistently everywhere — OTP logging, admin manual edits,
     and payroll's hours_worked lookup all call the SAME functions, so
     there's only one place where "how do we compute hours" is defined.

apps/attendance/views.py should import and use these instead of redefining
_time_diff / _calculate_total_hours inline (see integration_notes.py).
═══════════════════════════════════════════════════════════════════════════════
"""

from datetime import datetime, date, timedelta
from decimal import Decimal


def time_diff(t1, t2, allow_overnight=False):
    """
    Returns a timedelta between two time objects or 'HH:MM:SS' strings.

    If allow_overnight=True and t2 is earlier than t1 (e.g. 23:00 → 01:30),
    treats it as a shift that crossed midnight and adds one day to t2.
    Without the flag, an apparent "negative" duration returns timedelta(0)
    rather than a negative value — never let total_hours go negative.
    """
    if isinstance(t1, str):
        t1 = datetime.strptime(t1[:8].ljust(8, '0'), '%H:%M:%S').time()
    if isinstance(t2, str):
        t2 = datetime.strptime(t2[:8].ljust(8, '0'), '%H:%M:%S').time()

    base = date.today()
    d1 = datetime.combine(base, t1)
    d2 = datetime.combine(base, t2)
    diff = d2 - d1

    if diff.total_seconds() < 0:
        if allow_overnight:
            d2 = datetime.combine(base + timedelta(days=1), t2)
            diff = d2 - d1
        else:
            return timedelta(0)
    return diff


def calculate_total_hours(time_in_am, time_out_am, time_in_pm, time_out_pm):
    """
    Computes total hours worked from the four shift-half time fields.
    AM half and PM half are each calculated independently and are
    overnight-safe (a half that crosses midnight is handled correctly).

    Returns a Decimal rounded to 4 places (matches the model field
    precision so values aren't silently truncated on save).
    """
    total = timedelta()
    if time_in_am and time_out_am:
        total += time_diff(time_in_am, time_out_am, allow_overnight=True)
    if time_in_pm and time_out_pm:
        total += time_diff(time_in_pm, time_out_pm, allow_overnight=True)

    hours = Decimal(str(round(total.total_seconds() / 3600, 4)))
    return hours.quantize(Decimal('0.0001'))


def calculate_late_minutes(time_in_am, workday_start='08:00:00'):
    """
    Minutes late relative to the configured workday start time.
    Only checks the AM time-in (the start of the workday).
    Returns 0 if not late or if time_in_am is missing.
    """
    if not time_in_am:
        return 0
    if isinstance(time_in_am, str):
        t_in = datetime.strptime(time_in_am[:8].ljust(8, '0'), '%H:%M:%S').time()
    else:
        t_in = time_in_am
    start = datetime.strptime(workday_start, '%H:%M:%S').time()

    if t_in > start:
        base  = date.today()
        delta = datetime.combine(base, t_in) - datetime.combine(base, start)
        return int(delta.total_seconds() / 60)
    return 0


def calculate_undertime_minutes(time_out_pm, workday_end='17:00:00'):
    """
    Minutes of undertime relative to the configured workday end time.
    Only checks the PM time-out (the end of the workday).
    Returns 0 if not undertime or if time_out_pm is missing.
    """
    if not time_out_pm:
        return 0
    if isinstance(time_out_pm, str):
        t_out = datetime.strptime(time_out_pm[:8].ljust(8, '0'), '%H:%M:%S').time()
    else:
        t_out = time_out_pm
    end = datetime.strptime(workday_end, '%H:%M:%S').time()

    if t_out < end:
        base  = date.today()
        delta = datetime.combine(base, end) - datetime.combine(base, t_out)
        return int(delta.total_seconds() / 60)
    return 0

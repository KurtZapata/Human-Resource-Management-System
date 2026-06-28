"""
apps/employees/leave_balance.py  (NEW FILE)
═══════════════════════════════════════════════════════════════════════════════
Leave balance tracking. Separate from payroll/engine.py deliberately —
engine.py is a PURE read-only calculation function (called possibly many
times per payroll run with no side effects). Decrementing a leave balance
is a side-effecting WRITE that happens once, at the moment HR files the
leave (in payroll's adjustments_view), independent of when payroll itself
runs.

Call adjust_leave_balance() from adjustments_view() — see the wiring notes
in the delivery message for exactly where.
═══════════════════════════════════════════════════════════════════════════════
"""

from decimal import Decimal


def adjust_leave_balance(employee, leave_type_id, days_delta):
    """
    Adjusts an employee's LeaveBalance.remaining_days for one leave type.

    days_delta is NEGATIVE to consume leave credits (filing a new leave),
    POSITIVE to restore them (deleting or shrinking an existing leave
    adjustment). Never lets remaining_days go below zero.

    If the employee has no LeaveBalance row yet for this leave type, one
    is created starting at the type's max_days quota before applying the
    delta -- so the very first leave ever filed still has a sane starting
    point to subtract from.

    Returns the updated LeaveBalance instance, or None if leave_type_id
    doesn't resolve to a real LeaveType.
    """
    from accounts.models import LeaveType
    from .models import LeaveBalance

    try:
        leave_type = LeaveType.objects.get(pk=leave_type_id)
    except LeaveType.DoesNotExist:
        return None

    balance, _created = LeaveBalance.objects.get_or_create(
        employee=employee,
        leave_type=leave_type,
        defaults={'remaining_days': Decimal(str(leave_type.max_days))},
    )

    new_balance = balance.remaining_days + Decimal(str(days_delta))
    balance.remaining_days = max(Decimal('0'), new_balance)
    balance.save(update_fields=['remaining_days'])
    return balance


def hours_to_days(hours, hours_per_day=8):
    """Converts a leave Adjustment's hours into a day-count for balance tracking."""
    return (Decimal(str(hours)) / Decimal(str(hours_per_day)))


def reconcile_leave_adjustment_balance(employee, old_leave_type_id, old_hours,
                                        new_leave_type_id, new_hours):
    """
    Called when an existing leave Adjustment is edited. Restores whatever
    the OLD leave_type/hours combination had consumed, then deducts the
    NEW combination — correct whether the leave type changed, the hours
    changed, both changed, or it was converted to/from a non-leave type
    (pass None for whichever side doesn't apply).

    This is intentionally a single function (rather than two separate
    "restore" then "deduct" calls at the view layer) so the sequencing
    can be tested directly and never drifts apart from what the view
    actually does.
    """
    if old_leave_type_id:
        old_days = hours_to_days(old_hours) if old_hours else Decimal('1')
        adjust_leave_balance(employee, old_leave_type_id, days_delta=+old_days)

    if new_leave_type_id:
        new_days = hours_to_days(new_hours) if new_hours else Decimal('1')
        adjust_leave_balance(employee, new_leave_type_id, days_delta=-new_days)


def get_leave_summary(employee):
    """
    Returns a list of dicts -- one per LeaveType -- showing how many days
    are used vs remaining for this employee. Used on the employee profile
    page and anywhere a "vacation: 3/15 used" style summary is needed.

    Includes EVERY LeaveType, even ones the employee has never used yet
    (shown at full quota, 0 used), so the admin always sees the complete
    picture rather than only types that happen to have a LeaveBalance row.
    """
    from accounts.models import LeaveType
    from .models import LeaveBalance

    summary = []
    balances_by_type = {
        b.leave_type_id: b
        for b in LeaveBalance.objects.filter(employee=employee).select_related('leave_type')
    }

    for lt in LeaveType.objects.all():
        bal = balances_by_type.get(lt.id)
        remaining = bal.remaining_days if bal else Decimal(str(lt.max_days))
        used = Decimal(str(lt.max_days)) - remaining
        summary.append({
            'leave_type':      lt,
            'leave_type_id':   lt.id,
            'name':            lt.name,
            'is_paid':         lt.is_paid,
            'max_days':        lt.max_days,
            'remaining_days':  remaining,
            'used_days':       max(Decimal('0'), used),
        })
    return summary


def get_lateness_summary(employee, period=None):
    """
    Returns a tally of lateness for an employee -- either for a specific
    PayrollPeriod, or all-time if period is None. Sourced entirely from
    existing Attendance rows; no new model needed.

    Returns: {'late_count': int, 'total_late_minutes': int}
    """
    from django.db.models import Sum
    from attendance.models import Attendance

    qs = Attendance.objects.filter(employee=employee, status='late')
    if period is not None:
        qs = qs.filter(date__gte=period.start_date, date__lte=period.end_date)

    return {
        'late_count':         qs.count(),
        'total_late_minutes': qs.aggregate(t=Sum('late_minutes'))['t'] or 0,
    }

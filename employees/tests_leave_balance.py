"""
apps/employees/tests_leave_balance.py
Run with:
    python manage.py test employees.tests_leave_balance
"""
from datetime import date, time
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from accounts.models import LeaveType
from attendance.models import Attendance
from .models import Employee, LeaveBalance
from .leave_balance import adjust_leave_balance, hours_to_days, get_leave_summary, get_lateness_summary


class LeaveBalanceTestBase(TestCase):

    def setUp(self):
        self.emp = Employee.objects.create(
            employee_code='2026-0001', first_name='Juan', last_name='Cruz',
            email='juan@example.com', employment_type='regular', status='active',
        )
        self.vacation = LeaveType.objects.create(name='Vacation Leave', is_paid=True, max_days=15)
        self.sick     = LeaveType.objects.create(name='Sick Leave', is_paid=True, max_days=10)


class AdjustLeaveBalanceTests(LeaveBalanceTestBase):

    def test_first_leave_filed_creates_balance_at_full_quota_then_decrements(self):
        # No LeaveBalance row exists yet -- filing 2 days vacation leave
        adjust_leave_balance(self.emp, self.vacation.id, days_delta=Decimal('-2'))
        bal = LeaveBalance.objects.get(employee=self.emp, leave_type=self.vacation)
        self.assertEqual(bal.remaining_days, Decimal('13'))  # 15 - 2

    def test_multiple_leaves_decrement_cumulatively(self):
        adjust_leave_balance(self.emp, self.vacation.id, days_delta=Decimal('-2'))
        adjust_leave_balance(self.emp, self.vacation.id, days_delta=Decimal('-1'))
        bal = LeaveBalance.objects.get(employee=self.emp, leave_type=self.vacation)
        self.assertEqual(bal.remaining_days, Decimal('12'))  # 15 - 2 - 1

    def test_restoring_a_deleted_leave_adds_days_back(self):
        adjust_leave_balance(self.emp, self.vacation.id, days_delta=Decimal('-3'))
        adjust_leave_balance(self.emp, self.vacation.id, days_delta=Decimal('+3'))  # deleted later
        bal = LeaveBalance.objects.get(employee=self.emp, leave_type=self.vacation)
        self.assertEqual(bal.remaining_days, Decimal('15'))  # back to full

    def test_balance_never_goes_negative(self):
        adjust_leave_balance(self.emp, self.vacation.id, days_delta=Decimal('-20'))  # more than quota
        bal = LeaveBalance.objects.get(employee=self.emp, leave_type=self.vacation)
        self.assertEqual(bal.remaining_days, Decimal('0'))

    def test_different_leave_types_track_independently(self):
        adjust_leave_balance(self.emp, self.vacation.id, days_delta=Decimal('-5'))
        adjust_leave_balance(self.emp, self.sick.id, days_delta=Decimal('-2'))

        vac_bal  = LeaveBalance.objects.get(employee=self.emp, leave_type=self.vacation)
        sick_bal = LeaveBalance.objects.get(employee=self.emp, leave_type=self.sick)
        self.assertEqual(vac_bal.remaining_days, Decimal('10'))   # 15 - 5
        self.assertEqual(sick_bal.remaining_days, Decimal('8'))   # 10 - 2

    def test_unknown_leave_type_id_returns_none_without_crashing(self):
        result = adjust_leave_balance(self.emp, 999999, days_delta=Decimal('-1'))
        self.assertIsNone(result)


class HoursToDaysTests(TestCase):

    def test_eight_hours_equals_one_day(self):
        self.assertEqual(hours_to_days(8), Decimal('1'))

    def test_four_hours_equals_half_day(self):
        self.assertEqual(hours_to_days(4), Decimal('0.5'))

    def test_sixteen_hours_equals_two_days(self):
        self.assertEqual(hours_to_days(16), Decimal('2'))


class GetLeaveSummaryTests(LeaveBalanceTestBase):

    def test_unused_leave_types_show_full_quota(self):
        summary = get_leave_summary(self.emp)
        vac_entry = next(s for s in summary if s['name'] == 'Vacation Leave')
        self.assertEqual(vac_entry['remaining_days'], Decimal('15'))
        self.assertEqual(vac_entry['used_days'], Decimal('0'))

    def test_used_leave_shows_correct_remaining_and_used(self):
        adjust_leave_balance(self.emp, self.vacation.id, days_delta=Decimal('-4'))
        summary = get_leave_summary(self.emp)
        vac_entry = next(s for s in summary if s['name'] == 'Vacation Leave')
        self.assertEqual(vac_entry['remaining_days'], Decimal('11'))
        self.assertEqual(vac_entry['used_days'], Decimal('4'))

    def test_summary_includes_every_leave_type_even_unused_ones(self):
        LeaveType.objects.create(name='Bereavement Leave', is_paid=True, max_days=3)
        summary = get_leave_summary(self.emp)
        names = [s['name'] for s in summary]
        self.assertIn('Vacation Leave', names)
        self.assertIn('Sick Leave', names)
        self.assertIn('Bereavement Leave', names)
        self.assertEqual(len(summary), 3)


class GetLatenessSummaryTests(LeaveBalanceTestBase):

    def test_counts_late_records_and_minutes(self):
        Attendance.objects.create(
            employee=self.emp, date=date(2026, 6, 1), status='late', late_minutes=15
        )
        Attendance.objects.create(
            employee=self.emp, date=date(2026, 6, 2), status='late', late_minutes=10
        )
        Attendance.objects.create(
            employee=self.emp, date=date(2026, 6, 3), status='present', late_minutes=0
        )
        result = get_lateness_summary(self.emp)
        self.assertEqual(result['late_count'], 2)
        self.assertEqual(result['total_late_minutes'], 25)

    def test_no_late_records_returns_zeroes(self):
        result = get_lateness_summary(self.emp)
        self.assertEqual(result['late_count'], 0)
        self.assertEqual(result['total_late_minutes'], 0)

    def test_period_filter_excludes_lates_outside_the_period(self):
        from payroll.models import PayrollPeriod
        period = PayrollPeriod.objects.create(
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 15), status='open'
        )
        Attendance.objects.create(
            employee=self.emp, date=date(2026, 6, 5), status='late', late_minutes=20
        )
        Attendance.objects.create(
            employee=self.emp, date=date(2026, 7, 5), status='late', late_minutes=99
        )  # outside the period
        result = get_lateness_summary(self.emp, period=period)
        self.assertEqual(result['late_count'], 1)
        self.assertEqual(result['total_late_minutes'], 20)


class ReconcileLeaveAdjustmentBalanceTests(LeaveBalanceTestBase):
    """
    Tests the exact restore-old-then-deduct-new sequence used when HR
    edits an existing leave adjustment from the Adjustments page.
    """

    def test_increasing_hours_on_same_leave_type_only_deducts_the_difference(self):
        from .leave_balance import reconcile_leave_adjustment_balance
        # Originally filed 1 day (8h)
        adjust_leave_balance(self.emp, self.vacation.id, days_delta=Decimal('-1'))
        bal = LeaveBalance.objects.get(employee=self.emp, leave_type=self.vacation)
        self.assertEqual(bal.remaining_days, Decimal('14'))

        # HR edits it up to 3 days (24h)
        reconcile_leave_adjustment_balance(
            self.emp,
            old_leave_type_id=self.vacation.id, old_hours=Decimal('8'),
            new_leave_type_id=self.vacation.id, new_hours=Decimal('24'),
        )
        bal.refresh_from_db()
        # Restore 1 day (back to 15), then deduct 3 days -> 12
        self.assertEqual(bal.remaining_days, Decimal('12'))

    def test_decreasing_hours_on_same_leave_type_restores_the_difference(self):
        from .leave_balance import reconcile_leave_adjustment_balance
        adjust_leave_balance(self.emp, self.vacation.id, days_delta=Decimal('-3'))  # 3 days filed

        reconcile_leave_adjustment_balance(
            self.emp,
            old_leave_type_id=self.vacation.id, old_hours=Decimal('24'),
            new_leave_type_id=self.vacation.id, new_hours=Decimal('8'),  # reduced to 1 day
        )
        bal = LeaveBalance.objects.get(employee=self.emp, leave_type=self.vacation)
        self.assertEqual(bal.remaining_days, Decimal('14'))  # 15 - 1

    def test_changing_leave_type_moves_the_consumption_correctly(self):
        from .leave_balance import reconcile_leave_adjustment_balance
        adjust_leave_balance(self.emp, self.vacation.id, days_delta=Decimal('-2'))  # filed under vacation

        # HR realizes it should have been sick leave instead
        reconcile_leave_adjustment_balance(
            self.emp,
            old_leave_type_id=self.vacation.id, old_hours=Decimal('16'),
            new_leave_type_id=self.sick.id, new_hours=Decimal('16'),
        )
        vac_bal  = LeaveBalance.objects.get(employee=self.emp, leave_type=self.vacation)
        sick_bal = LeaveBalance.objects.get(employee=self.emp, leave_type=self.sick)
        self.assertEqual(vac_bal.remaining_days, Decimal('15'))  # fully restored
        self.assertEqual(sick_bal.remaining_days, Decimal('8'))  # 10 - 2

    def test_converting_leave_to_overtime_only_restores_no_new_deduction(self):
        """If HR changes the adjustment type away from 'leave' entirely,
        new_leave_type_id will be None -- only the restore should fire."""
        from .leave_balance import reconcile_leave_adjustment_balance
        adjust_leave_balance(self.emp, self.vacation.id, days_delta=Decimal('-2'))

        reconcile_leave_adjustment_balance(
            self.emp,
            old_leave_type_id=self.vacation.id, old_hours=Decimal('16'),
            new_leave_type_id=None, new_hours=None,
        )
        bal = LeaveBalance.objects.get(employee=self.emp, leave_type=self.vacation)
        self.assertEqual(bal.remaining_days, Decimal('15'))  # fully restored, nothing deducted

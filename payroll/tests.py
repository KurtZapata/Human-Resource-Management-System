"""
apps/payroll/tests.py
═══════════════════════════════════════════════════════════════════════════════
Tests for the payroll calculation engine (apps/payroll/engine.py).

Run with:
    python manage.py test payroll

These build real model fixtures (Employee, SalaryGrade, Attendance,
PayrollPeriod, PayrollComponent, Adjustment, Calendar) using Django's test
database, and assert that compute_employee_payroll() returns the exact
expected peso amounts for each scenario. If any of these go red, the
payroll formula has regressed — do not deploy until they pass again.
═══════════════════════════════════════════════════════════════════════════════
"""

from datetime import date, time
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase

from employees.models import Employee, Department, Position, SalaryGrade
from attendance.models import Attendance
from attendance.utils import calculate_total_hours, calculate_late_minutes
from calendar_app.models import Calendar
from accounts.models import LeaveType

from .models import PayrollPeriod, PayrollComponent, Adjustment, EmployeePayrollComponent
from .engine import compute_employee_payroll, eval_formula


# ═══════════════════════════════════════════════════════════════════════════════
#  Shared fixtures
# ═══════════════════════════════════════════════════════════════════════════════

class PayrollEngineTestBase(TestCase):
    """Common setup: one employee on P100/hr, P125/hr OT, one open period."""

    def setUp(self):
        self.user = User.objects.create_user('hradmin', password='test12345')

        self.dept     = Department.objects.create(name='Operations')
        self.position = Position.objects.create(name='Staff', department=self.dept, base_salary=0)
        self.grade    = SalaryGrade.objects.create(
            name='Grade 1',
            hourly_rate=Decimal('100.00'),
            overtime_rate=Decimal('125.00'),
        )
        self.employee = Employee.objects.create(
            employee_code='2026-0001',
            first_name='Juan', last_name='Dela Cruz',
            email='juan@example.com',
            employment_type='regular', status='active',
            department=self.dept, position=self.position,
            salary_grade=self.grade,
        )
        self.period = PayrollPeriod.objects.create(
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 15),
            status='open',
        )

    def _log_day(self, day, time_in_am=time(8, 0), time_out_am=time(12, 0),
                 time_in_pm=time(13, 0), time_out_pm=time(17, 0), status='present'):
        """Creates one Attendance record with correctly auto-computed hours."""
        hours = calculate_total_hours(time_in_am, time_out_am, time_in_pm, time_out_pm)
        late  = calculate_late_minutes(time_in_am) if time_in_am else 0
        return Attendance.objects.create(
            employee=self.employee, date=day,
            time_in_am=time_in_am, time_out_am=time_out_am,
            time_in_pm=time_in_pm, time_out_pm=time_out_pm,
            total_hours=hours, late_minutes=late, status=status,
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  1. Basic pay = hourly_rate x hours_worked
# ═══════════════════════════════════════════════════════════════════════════════

class BasicPayCalculationTests(PayrollEngineTestBase):
    """Verifies the core formula: basic_pay = hourly_rate x hours_worked."""

    def test_basic_pay_for_one_standard_8_hour_day(self):
        self._log_day(date(2026, 6, 1))  # 8 hours
        result = compute_employee_payroll(self.employee, self.period, components=[])
        self.assertEqual(result['hours_worked'], Decimal('8.0000'))
        self.assertEqual(result['basic_pay'], Decimal('800.00'))   # 100 x 8
        self.assertEqual(result['net_pay'], Decimal('800.00'))

    def test_basic_pay_accumulates_across_multiple_days(self):
        self._log_day(date(2026, 6, 1))
        self._log_day(date(2026, 6, 2))
        self._log_day(date(2026, 6, 3))
        result = compute_employee_payroll(self.employee, self.period, components=[])
        self.assertEqual(result['hours_worked'], Decimal('24.0000'))
        self.assertEqual(result['basic_pay'], Decimal('2400.00'))  # 100 x 24

    def test_half_day_pay_is_proportional(self):
        self._log_day(date(2026, 6, 1), time_in_pm=None, time_out_pm=None)  # 4h only
        result = compute_employee_payroll(self.employee, self.period, components=[])
        self.assertEqual(result['hours_worked'], Decimal('4.0000'))
        self.assertEqual(result['basic_pay'], Decimal('400.00'))

    def test_no_attendance_means_zero_pay(self):
        result = compute_employee_payroll(self.employee, self.period, components=[])
        self.assertEqual(result['basic_pay'], Decimal('0.00'))
        self.assertEqual(result['net_pay'], Decimal('0.00'))

    def test_missing_salary_grade_raises_error(self):
        self.employee.salary_grade = None
        self.employee.save()
        with self.assertRaises(ValueError):
            compute_employee_payroll(self.employee, self.period, components=[])


# ═══════════════════════════════════════════════════════════════════════════════
#  2. Modular components -- fixed / percentage / formula / operators
# ═══════════════════════════════════════════════════════════════════════════════

class ComponentCalculationTests(PayrollEngineTestBase):

    def test_fixed_deduction_subtracted_correctly(self):
        self._log_day(date(2026, 6, 1))  # basic = 800.00
        pagibig = PayrollComponent.objects.create(
            name='Pag-IBIG', type='deduction', operator='-',
            calculation_type='fixed', default_value=Decimal('100.00'),
            is_active=True, sort_order=1,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[pagibig])
        self.assertEqual(result['net_pay'], Decimal('700.00'))
        self.assertEqual(result['total_deductions'], Decimal('100.00'))

    def test_percentage_deduction_uses_monthly_equiv_not_basic_pay(self):
        """
        SSS-style deductions must base on monthly_equiv (hourly_rate x 8 x 22),
        NOT the variable hours-based basic_pay -- otherwise a short pay period
        would wrongly shrink the statutory deduction.
        """
        self._log_day(date(2026, 6, 1))  # only 8h worked this period
        sss = PayrollComponent.objects.create(
            name='SSS', type='deduction', operator='-',
            calculation_type='percentage', default_value=Decimal('4.5'),
            pct_base='monthly_equiv', is_active=True, sort_order=1,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[sss])
        # monthly_equiv = 100 x 8 x 22 = 17,600.00 ; 4.5% = 792.00
        sss_line = next(b for b in result['breakdown'] if b['name'] == 'SSS')
        self.assertEqual(round(sss_line['amount'], 2), 792.00)

    def test_formula_component_late_deduction(self):
        self._log_day(date(2026, 6, 1), time_in_am=time(8, 30, 0))  # 30 min late
        late_ded = PayrollComponent.objects.create(
            name='Late Deduction', type='deduction', operator='-',
            calculation_type='formula',
            formula='hourly_rate / 60 * late_minutes',
            default_value=Decimal('0'), is_active=True, sort_order=1,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[late_ded])
        # (100/60) x 30 = 50.00
        late_line = next(b for b in result['breakdown'] if b['name'] == 'Late Deduction')
        self.assertEqual(round(late_line['amount'], 2), 50.00)

    def test_multiply_operator(self):
        self._log_day(date(2026, 6, 1))  # basic = 800.00
        bonus = PayrollComponent.objects.create(
            name='Multiplier Test', type='earning', operator='*',
            calculation_type='fixed', default_value=Decimal('1.1'),
            is_active=True, sort_order=1,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[bonus])
        self.assertEqual(result['net_pay'], Decimal('880.00'))  # 800 x 1.1

    def test_employee_override_takes_precedence_over_default(self):
        self._log_day(date(2026, 6, 1))  # basic = 800.00
        allowance = PayrollComponent.objects.create(
            name='Allowance', type='earning', operator='+',
            calculation_type='fixed', default_value=Decimal('500.00'),
            is_active=True, sort_order=1,
        )
        EmployeePayrollComponent.objects.create(
            employee=self.employee, component=allowance,
            value=Decimal('750.00'), is_active=True,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[allowance])
        self.assertEqual(result['net_pay'], Decimal('1550.00'))  # 800 + 750 (override, not 500)

    def test_inactive_override_is_ignored(self):
        self._log_day(date(2026, 6, 1))
        allowance = PayrollComponent.objects.create(
            name='Allowance', type='earning', operator='+',
            calculation_type='fixed', default_value=Decimal('500.00'),
            is_active=True, sort_order=1,
        )
        EmployeePayrollComponent.objects.create(
            employee=self.employee, component=allowance,
            value=Decimal('750.00'), is_active=False,  # disabled override
        )
        result = compute_employee_payroll(self.employee, self.period, components=[allowance])
        self.assertEqual(result['net_pay'], Decimal('1300.00'))  # 800 + 500 (default used)


# ═══════════════════════════════════════════════════════════════════════════════
#  3. Overtime -- hours x ot_rate, recomputed live every time
# ═══════════════════════════════════════════════════════════════════════════════

class OvertimeCalculationTests(PayrollEngineTestBase):

    def test_overtime_added_correctly(self):
        self._log_day(date(2026, 6, 1))  # basic = 800.00
        Adjustment.objects.create(
            employee=self.employee, payroll_period=self.period,
            type='overtime', hours=Decimal('3'), rate=Decimal('0'), amount=Decimal('0'),
            description='Overtime on 2026-06-01', created_by=self.user,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[])
        # OT = 3h x 125 = 375.00 ; net = 800 + 375 = 1175.00
        self.assertEqual(result['net_pay'], Decimal('1175.00'))

    def test_overtime_recomputed_live_ignores_stale_stored_amount(self):
        """
        If hours/rate change after an adjustment was saved, the engine must
        recompute from hours x the CURRENT ot_rate -- never trust whatever
        amount happens to already be stored on the row.
        """
        self._log_day(date(2026, 6, 1))
        Adjustment.objects.create(
            employee=self.employee, payroll_period=self.period,
            type='overtime', hours=Decimal('2'),
            rate=Decimal('999'), amount=Decimal('9999.00'),  # deliberately wrong/stale
            description='Overtime on 2026-06-01', created_by=self.user,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[])
        self.assertEqual(result['net_pay'], Decimal('1050.00'))  # 800 + (2 x 125), NOT 9999

    def test_multiple_overtime_entries_sum_correctly(self):
        self._log_day(date(2026, 6, 1))
        Adjustment.objects.create(
            employee=self.employee, payroll_period=self.period, type='overtime',
            hours=Decimal('2'), rate=Decimal('0'), amount=Decimal('0'),
            description='Overtime on 2026-06-01', created_by=self.user,
        )
        Adjustment.objects.create(
            employee=self.employee, payroll_period=self.period, type='overtime',
            hours=Decimal('1.5'), rate=Decimal('0'), amount=Decimal('0'),
            description='Overtime on 2026-06-02', created_by=self.user,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[])
        # (2 + 1.5) x 125 = 437.50 ; net = 800 + 437.50 = 1237.50
        self.assertEqual(result['net_pay'], Decimal('1237.50'))


# ═══════════════════════════════════════════════════════════════════════════════
#  4. Leave -- manual amount, sign decides the effect
# ═══════════════════════════════════════════════════════════════════════════════

class LeaveCalculationTests(PayrollEngineTestBase):

    def setUp(self):
        super().setUp()
        self.leave_type = LeaveType.objects.create(name='Sick Leave', is_paid=True, max_days=5)

    def test_positive_leave_amount_adds_to_pay(self):
        self._log_day(date(2026, 6, 1))  # basic = 800.00
        Adjustment.objects.create(
            employee=self.employee, payroll_period=self.period,
            type='leave', hours=Decimal('8'),
            amount=Decimal('800.00'),  # HR manually grants a full paid leave day
            leave_type_id=self.leave_type.id,
            description='Paid sick leave', created_by=self.user,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[])
        self.assertEqual(result['net_pay'], Decimal('1600.00'))  # 800 + 800

    def test_negative_leave_amount_deducts_from_pay(self):
        self._log_day(date(2026, 6, 1))  # basic = 800.00
        Adjustment.objects.create(
            employee=self.employee, payroll_period=self.period,
            type='leave', hours=Decimal('8'),
            amount=Decimal('-800.00'),  # HR manually deducts unpaid leave
            leave_type_id=self.leave_type.id,
            description='Unpaid leave', created_by=self.user,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[])
        self.assertEqual(result['net_pay'], Decimal('0.00'))
        self.assertEqual(result['total_deductions'], Decimal('800.00'))


# ═══════════════════════════════════════════════════════════════════════════════
#  5. Calendar -- holiday / rest day premiums
# ═══════════════════════════════════════════════════════════════════════════════

class CalendarPremiumTests(PayrollEngineTestBase):

    def test_regular_holiday_worked_doubles_pay_for_that_day(self):
        self._log_day(date(2026, 6, 1))    # ordinary day, 8h
        self._log_day(date(2026, 6, 12))   # 8h worked ON a holiday
        Calendar.objects.create(
            date=date(2026, 6, 12), type='regular_holiday',
            is_paid=True, rate_multiplier=Decimal('2.00'),
            description='Independence Day', created_by=self.user,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[])
        # June 1: 8h x 100 = 800 normal
        # June 12: 8h x 100 = 800 normal + premium (800x2 - 800) = 800
        # basic_pay = (16h x 100) + 800 premium = 1600 + 800 = 2400
        self.assertEqual(result['calendar_premium'], Decimal('800.00'))
        self.assertEqual(result['basic_pay'], Decimal('2400.00'))

    def test_special_holiday_worked_applies_130_percent(self):
        self._log_day(date(2026, 6, 6))
        Calendar.objects.create(
            date=date(2026, 6, 6), type='special_holiday',
            is_paid=False, rate_multiplier=Decimal('1.30'),
            description='Special Non-Working Day', created_by=self.user,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[])
        # 8h x 100 x 1.3 = 1040 ; premium = 1040 - 800 = 240
        self.assertEqual(result['calendar_premium'], Decimal('240.00'))
        self.assertEqual(result['basic_pay'], Decimal('1040.00'))

    def test_rest_day_worked_applies_multiplier(self):
        self._log_day(date(2026, 6, 7))
        Calendar.objects.create(
            date=date(2026, 6, 7), type='rest',
            is_paid=False, rate_multiplier=Decimal('1.30'),
            description='Saturday rest day', created_by=self.user,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[])
        self.assertEqual(result['calendar_premium'], Decimal('240.00'))

    def test_regular_workday_has_no_premium(self):
        self._log_day(date(2026, 6, 3))  # no Calendar entry = ordinary workday
        result = compute_employee_payroll(self.employee, self.period, components=[])
        self.assertEqual(result['calendar_premium'], Decimal('0.00'))

    def test_holiday_with_no_attendance_has_no_premium(self):
        """
        If the employee was absent on the holiday (no Attendance record
        at all), there's no work to apply a premium to.
        """
        Calendar.objects.create(
            date=date(2026, 6, 12), type='regular_holiday',
            is_paid=True, rate_multiplier=Decimal('2.00'),
            description='Independence Day', created_by=self.user,
        )
        result = compute_employee_payroll(self.employee, self.period, components=[])
        self.assertEqual(result['calendar_premium'], Decimal('0.00'))


# ═══════════════════════════════════════════════════════════════════════════════
#  6. Formula evaluator -- safety + correctness
# ═══════════════════════════════════════════════════════════════════════════════

class FormulaEvaluationTests(TestCase):
    """
    Direct tests for the safe formula evaluator -- the component most
    likely to be exploited (admin-authored formulas) or to silently fail.
    """

    def test_simple_arithmetic(self):
        result = eval_formula('basic_pay * 0.045', {'basic_pay': 1000})
        self.assertEqual(result, Decimal('45.00'))

    def test_multiple_variables(self):
        result = eval_formula(
            'hourly_rate / 60 * late_minutes',
            {'hourly_rate': 120, 'late_minutes': 15},
        )
        self.assertEqual(result, Decimal('30.00'))

    def test_rejects_non_numeric_injection(self):
        """
        A formula containing anything other than digits/operators after
        substitution must return 0 -- and must NEVER reach eval() with
        attacker-controlled code.
        """
        result = eval_formula(
            '__import__("os").system("echo pwned")',
            {'basic_pay': 1000},
        )
        self.assertEqual(result, Decimal('0'))

    def test_empty_formula_returns_zero(self):
        self.assertEqual(eval_formula('', {'basic_pay': 1000}), Decimal('0'))

    def test_division_by_zero_does_not_crash(self):
        result = eval_formula('basic_pay / zero_var', {'basic_pay': 1000, 'zero_var': 0})
        self.assertEqual(result, Decimal('0'))

    def test_longest_variable_substituted_first(self):
        """
        'hourly_rate' must not be partially clobbered by a shorter
        variable name like 'rate' if both exist in vars_ctx.
        """
        result = eval_formula('hourly_rate * 2', {'hourly_rate': 50, 'rate': 999})
        self.assertEqual(result, Decimal('100.00'))


# ═══════════════════════════════════════════════════════════════════════════════
#  7. End-to-end -- everything combined in one realistic period
# ═══════════════════════════════════════════════════════════════════════════════

class EndToEndPayrollTests(PayrollEngineTestBase):

    def test_full_period_combining_hours_components_ot_leave_and_holiday(self):
        # 10 ordinary working days at 8h each
        for day in range(1, 11):
            self._log_day(date(2026, 6, day))

        # One regular holiday actually worked
        self._log_day(date(2026, 6, 12))
        Calendar.objects.create(
            date=date(2026, 6, 12), type='regular_holiday',
            is_paid=True, rate_multiplier=Decimal('2.00'),
            description='Holiday', created_by=self.user,
        )

        sss = PayrollComponent.objects.create(
            name='SSS', type='deduction', operator='-',
            calculation_type='percentage', default_value=Decimal('4.5'),
            pct_base='monthly_equiv', is_active=True, sort_order=1,
        )
        pagibig = PayrollComponent.objects.create(
            name='Pag-IBIG', type='deduction', operator='-',
            calculation_type='fixed', default_value=Decimal('100.00'),
            is_active=True, sort_order=2,
        )

        Adjustment.objects.create(
            employee=self.employee, payroll_period=self.period,
            type='overtime', hours=Decimal('4'), rate=Decimal('0'), amount=Decimal('0'),
            description='Overtime on 2026-06-05', created_by=self.user,
        )

        result = compute_employee_payroll(self.employee, self.period, components=[sss, pagibig])

        # hours_worked = 11 days x 8h = 88h
        self.assertEqual(result['hours_worked'], Decimal('88.0000'))

        # basic_pay = (88h x 100) normal + 800 holiday premium = 8800 + 800 = 9600
        self.assertEqual(result['calendar_premium'], Decimal('800.00'))
        self.assertEqual(result['basic_pay'], Decimal('9600.00'))

        # SSS = 4.5% x 17,600 (monthly_equiv) = 792.00
        # Pag-IBIG = 100.00
        # OT = 4h x 125 = 500.00
        # net = 9600 - 792 - 100 + 500 = 9208.00
        self.assertEqual(result['net_pay'], Decimal('9208.00'))
        self.assertEqual(result['total_deductions'], Decimal('892.00'))


# ═══════════════════════════════════════════════════════════════════════════════
#  8. Payroll period date-uniqueness (no two periods share a date)
# ═══════════════════════════════════════════════════════════════════════════════

class PayrollPeriodOverlapTests(TestCase):

    def test_overlapping_period_is_detected(self):
        PayrollPeriod.objects.create(
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 15), status='open'
        )
        overlap_exists = PayrollPeriod.objects.filter(
            start_date__lte=date(2026, 6, 20),
            end_date__gte=date(2026, 6, 10),
        ).exists()
        self.assertTrue(overlap_exists)

    def test_adjacent_non_overlapping_periods_allowed(self):
        PayrollPeriod.objects.create(
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 15), status='open'
        )
        overlap_exists = PayrollPeriod.objects.filter(
            start_date__lte=date(2026, 6, 30),
            end_date__gte=date(2026, 6, 16),
        ).exists()
        self.assertFalse(overlap_exists)

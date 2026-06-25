"""
apps/attendance/test_crud.py
═══════════════════════════════════════════════════════════════════════════════
CRUD tests for Attendance manual entry/edit, the payroll-lock enforcement
on edits, the quick "grant overtime" action, and OTP generation.

Kept separate from tests.py (which covers pure hours/lateness calculation).
Django auto-discovers any test*.py file in each app, so this runs alongside
tests.py automatically.

Run with:
    python manage.py test attendance
═══════════════════════════════════════════════════════════════════════════════
"""

import json
from datetime import date, time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee, Department, SalaryGrade
from payroll.models import PayrollPeriod, Payroll, Adjustment
from .models import Attendance, OTP


class AttendanceCRUDTestBase(TestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser(
            'superadmin', 'admin@example.com', 'adminpass123'
        )
        self.client.login(username='superadmin', password='adminpass123')

        self.dept  = Department.objects.create(name='Operations')
        self.grade = SalaryGrade.objects.create(
            name='Grade 1', hourly_rate=Decimal('100.00'), overtime_rate=Decimal('125.00')
        )
        self.employee = Employee.objects.create(
            employee_code='2026-0001', first_name='Juan', last_name='Cruz',
            email='juan@example.com', employment_type='regular', status='active',
            department=self.dept, salary_grade=self.grade,
        )
        self.period = PayrollPeriod.objects.create(
            start_date=date(2026, 6, 1), end_date=date(2026, 6, 15), status='open'
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  Manual attendance entry — create
# ═══════════════════════════════════════════════════════════════════════════════

class ManualEntryTests(AttendanceCRUDTestBase):

    def test_manual_entry_creates_attendance_with_computed_hours(self):
        self.client.post(reverse('attendance:manual_entry'), {
            'employee_id': self.employee.id, 'date': '2026-06-01',
            'time_in_am': '08:00', 'time_out_am': '12:00',
            'time_in_pm': '13:00', 'time_out_pm': '17:00',
            'status': 'present',
        })
        att = Attendance.objects.get(employee=self.employee, date=date(2026, 6, 1))
        self.assertEqual(att.total_hours, Decimal('8.0000'))

    def test_manual_entry_does_not_duplicate_existing_record(self):
        """get_or_create semantics — re-submitting for the same employee
        + date must update the existing row, not create a second one."""
        Attendance.objects.create(employee=self.employee, date=date(2026, 6, 1), status='present')
        self.client.post(reverse('attendance:manual_entry'), {
            'employee_id': self.employee.id, 'date': '2026-06-01',
            'time_in_am': '08:00', 'time_out_am': '12:00',
            'status': 'present',
        })
        self.assertEqual(
            Attendance.objects.filter(employee=self.employee, date=date(2026, 6, 1)).count(), 1
        )

    def test_manual_entry_handles_overnight_shift(self):
        self.client.post(reverse('attendance:manual_entry'), {
            'employee_id': self.employee.id, 'date': '2026-06-01',
            'time_in_pm': '22:00', 'time_out_pm': '02:00',
            'status': 'present',
        })
        att = Attendance.objects.get(employee=self.employee, date=date(2026, 6, 1))
        self.assertEqual(att.total_hours, Decimal('4.0000'))


# ═══════════════════════════════════════════════════════════════════════════════
#  Update attendance + payroll lock enforcement
# ═══════════════════════════════════════════════════════════════════════════════

class UpdateAttendanceTests(AttendanceCRUDTestBase):

    def setUp(self):
        super().setUp()
        self.att = Attendance.objects.create(
            employee=self.employee, date=date(2026, 6, 1),
            time_in_am=time(8, 0), time_out_am=time(12, 0),
            time_in_pm=time(13, 0), time_out_pm=time(17, 0),
            total_hours=Decimal('8.0000'), status='present',
        )

    def test_update_recalculates_total_hours(self):
        self.client.post(reverse('attendance:update', args=[self.att.id]), {
            'time_out_pm': '18:00',  # worked 1 extra hour
        })
        self.att.refresh_from_db()
        self.assertEqual(self.att.total_hours, Decimal('9.0000'))

    def test_update_with_empty_field_does_not_clear_existing_time(self):
        """
        REGRESSION TEST: submitting the edit form without changing a
        field must NOT wipe that field — an earlier version of this view
        treated a blank POST value as 'clear this field', which erased
        attendance data on every no-op save.
        """
        self.client.post(reverse('attendance:update', args=[self.att.id]), {
            'time_in_am': '',  # blank = unchanged, not 'clear this field'
            'status': 'present',
        })
        self.att.refresh_from_db()
        self.assertEqual(self.att.time_in_am, time(8, 0))  # still set

    def test_update_with_explicit_clear_sentinel_does_clear_field(self):
        self.client.post(reverse('attendance:update', args=[self.att.id]), {
            'time_out_pm': 'CLEAR',
        })
        self.att.refresh_from_db()
        self.assertIsNone(self.att.time_out_pm)

    def test_update_blocked_when_payroll_already_confirmed(self):
        Payroll.objects.create(
            employee=self.employee, payroll_period=self.period,
            basic_pay=Decimal('800'), gross_pay=Decimal('800'),
            total_deductions=Decimal('0'), net_pay=Decimal('800'),
            is_confirmed=True,
        )
        self.client.post(reverse('attendance:update', args=[self.att.id]), {
            'time_out_pm': '20:00',
        })
        self.att.refresh_from_db()
        self.assertEqual(self.att.time_out_pm, time(17, 0))  # unchanged — edit rejected

    def test_update_allowed_when_payroll_is_still_draft(self):
        Payroll.objects.create(
            employee=self.employee, payroll_period=self.period,
            basic_pay=Decimal('800'), gross_pay=Decimal('800'),
            total_deductions=Decimal('0'), net_pay=Decimal('800'),
            is_confirmed=False,
        )
        self.client.post(reverse('attendance:update', args=[self.att.id]), {
            'time_out_pm': '18:00',
        })
        self.att.refresh_from_db()
        self.assertEqual(self.att.time_out_pm, time(18, 0))

    def test_update_recomputes_draft_payroll_net_pay(self):
        """
        Editing attendance must flow through to the employee's draft
        payroll net_pay — this is the live-recompute integration that
        keeps payroll honest when HR fixes a typo after running payroll.
        """
        payroll = Payroll.objects.create(
            employee=self.employee, payroll_period=self.period,
            basic_pay=Decimal('800'), gross_pay=Decimal('800'),
            total_deductions=Decimal('0'), net_pay=Decimal('800'),
            is_confirmed=False,
        )
        self.client.post(reverse('attendance:update', args=[self.att.id]), {
            'time_out_pm': '18:00',  # 8h -> 9h worked
        })
        payroll.refresh_from_db()
        self.assertEqual(payroll.net_pay, Decimal('900.00'))  # 9h x 100/hr

    def test_update_status_change_persists(self):
        self.client.post(reverse('attendance:update', args=[self.att.id]), {
            'status': 'absent',
        })
        self.att.refresh_from_db()
        self.assertEqual(self.att.status, 'absent')


# ═══════════════════════════════════════════════════════════════════════════════
#  Grant overtime — quick-grant from calendar cell
# ═══════════════════════════════════════════════════════════════════════════════

class GrantOvertimeTests(AttendanceCRUDTestBase):

    def setUp(self):
        super().setUp()
        Attendance.objects.create(
            employee=self.employee, date=date(2026, 6, 1),
            time_in_am=time(8, 0), time_out_am=time(12, 0),
            time_in_pm=time(13, 0), time_out_pm=time(20, 0),  # worked late, 11h total
            total_hours=Decimal('11.0000'), status='present',
        )

    def test_grant_overtime_creates_adjustment_with_correct_amount(self):
        resp = self.client.post(
            reverse('attendance:grant_overtime'),
            data=json.dumps({
                'employee_id': self.employee.id, 'date': '2026-06-01',
                'hours': 3, 'description': 'Overtime on 2026-06-01',
            }),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 200)
        adj = Adjustment.objects.get(employee=self.employee, type='overtime')
        self.assertEqual(adj.hours, Decimal('3'))
        self.assertEqual(adj.amount, Decimal('375.00'))  # 3h x 125/hr

    def test_grant_overtime_blocked_when_payroll_confirmed(self):
        Payroll.objects.create(
            employee=self.employee, payroll_period=self.period,
            basic_pay=Decimal('1100'), gross_pay=Decimal('1100'),
            total_deductions=Decimal('0'), net_pay=Decimal('1100'),
            is_confirmed=True,
        )
        resp = self.client.post(
            reverse('attendance:grant_overtime'),
            data=json.dumps({
                'employee_id': self.employee.id, 'date': '2026-06-01',
                'hours': 3, 'description': 'Late OT',
            }),
            content_type='application/json',
        )
        data = resp.json()
        self.assertFalse(data['ok'])
        self.assertEqual(
            Adjustment.objects.filter(employee=self.employee, type='overtime').count(), 0
        )

    def test_grant_overtime_rejects_zero_hours(self):
        resp = self.client.post(
            reverse('attendance:grant_overtime'),
            data=json.dumps({
                'employee_id': self.employee.id, 'date': '2026-06-01',
                'hours': 0, 'description': 'Invalid',
            }),
            content_type='application/json',
        )
        data = resp.json()
        self.assertFalse(data['ok'])

    def test_grant_overtime_rejects_unknown_employee(self):
        resp = self.client.post(
            reverse('attendance:grant_overtime'),
            data=json.dumps({
                'employee_id': 999999, 'date': '2026-06-01',
                'hours': 2, 'description': 'Invalid employee',
            }),
            content_type='application/json',
        )
        data = resp.json()
        self.assertFalse(data['ok'])


# ═══════════════════════════════════════════════════════════════════════════════
#  OTP generation
# ═══════════════════════════════════════════════════════════════════════════════

class OTPGenerationTests(AttendanceCRUDTestBase):

    def test_generate_global_otp(self):
        resp = self.client.post(
            reverse('attendance:generate_otp'),
            data=json.dumps({'employee_id': None, 'expires_minutes': 5}),
            content_type='application/json',
        )
        data = resp.json()
        self.assertEqual(len(data['code']), 6)
        otp = OTP.objects.get(code=data['code'])
        self.assertIsNone(otp.used_by_employee_id)  # global scope

    def test_generate_employee_specific_otp(self):
        resp = self.client.post(
            reverse('attendance:generate_otp'),
            data=json.dumps({'employee_id': self.employee.id, 'expires_minutes': 5}),
            content_type='application/json',
        )
        data = resp.json()
        otp = OTP.objects.get(code=data['code'])
        self.assertEqual(otp.used_by_employee_id, self.employee.id)

    def test_otp_expiry_respects_requested_minutes(self):
        before = timezone.now()
        resp = self.client.post(
            reverse('attendance:generate_otp'),
            data=json.dumps({'employee_id': None, 'expires_minutes': 10}),
            content_type='application/json',
        )
        data = resp.json()
        otp = OTP.objects.get(code=data['code'])
        expected_expiry = before + timedelta(minutes=10)
        self.assertAlmostEqual(
            otp.expires_at.timestamp(), expected_expiry.timestamp(), delta=5
        )

    def test_each_generated_otp_starts_unused(self):
        resp = self.client.post(
            reverse('attendance:generate_otp'),
            data=json.dumps({'employee_id': None, 'expires_minutes': 5}),
            content_type='application/json',
        )
        otp = OTP.objects.get(code=resp.json()['code'])
        self.assertFalse(otp.is_used)

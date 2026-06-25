"""
apps/payroll/test_crud.py
═══════════════════════════════════════════════════════════════════════════════
CRUD tests for PayrollComponent, PayrollPeriod, and Adjustment.

Kept separate from tests.py (which covers calculation-engine correctness)
so the two concerns don't get tangled in one file. Django's test runner
auto-discovers any file matching test*.py in each app, so this runs
alongside tests.py automatically — no manual wiring needed.

Run with:
    python manage.py test payroll
═══════════════════════════════════════════════════════════════════════════════
"""

import json
from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from employees.models import Employee, Department, SalaryGrade
from accounts.models import LeaveType
from .models import PayrollComponent, PayrollPeriod, Adjustment, PayrollBreakdown, Payroll


class PayrollCRUDTestBase(TestCase):

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
#  PayrollComponent — Full CRUD
# ═══════════════════════════════════════════════════════════════════════════════

class PayrollComponentCRUDTests(PayrollCRUDTestBase):

    def test_create_fixed_component(self):
        self.client.post(reverse('payroll:create_component'), {
            'name': 'Rice Allowance', 'type': 'earning', 'operator': '+',
            'calculation_type': 'fixed', 'default_value': '1500.00',
            'is_active': 'true',
        })
        comp = PayrollComponent.objects.get(name='Rice Allowance')
        self.assertEqual(comp.default_value, Decimal('1500.0000'))
        self.assertFalse(comp.is_locked)  # admin-created components are never locked

    def test_create_percentage_component_with_pct_base(self):
        self.client.post(reverse('payroll:create_component'), {
            'name': 'Union Dues', 'type': 'deduction', 'operator': '-',
            'calculation_type': 'percentage', 'default_value': '1.0',
            'pct_base': 'monthly_equiv', 'is_active': 'true',
        })
        comp = PayrollComponent.objects.get(name='Union Dues')
        self.assertEqual(comp.pct_base, 'monthly_equiv')

    def test_create_formula_component(self):
        self.client.post(reverse('payroll:create_component'), {
            'name': 'Custom Bonus', 'type': 'earning', 'operator': '+',
            'calculation_type': 'formula', 'formula': 'basic_pay * 0.05',
            'default_value': '0', 'is_active': 'true',
        })
        comp = PayrollComponent.objects.get(name='Custom Bonus')
        self.assertEqual(comp.formula, 'basic_pay * 0.05')

    def test_new_component_appended_to_end_of_sort_order(self):
        PayrollComponent.objects.create(
            name='First', type='earning', calculation_type='fixed',
            default_value=Decimal('0'), sort_order=5, is_active=True,
        )
        self.client.post(reverse('payroll:create_component'), {
            'name': 'Second', 'type': 'earning', 'operator': '+',
            'calculation_type': 'fixed', 'default_value': '0', 'is_active': 'true',
        })
        second = PayrollComponent.objects.get(name='Second')
        self.assertGreater(second.sort_order, 5)

    def test_update_component(self):
        comp = PayrollComponent.objects.create(
            name='Allowance', type='earning', calculation_type='fixed',
            default_value=Decimal('500'), is_active=True, sort_order=1,
        )
        self.client.post(reverse('payroll:update_component', args=[comp.id]), {
            'name': 'Allowance', 'type': 'earning', 'operator': '+',
            'calculation_type': 'fixed', 'default_value': '750.00', 'is_active': 'true',
        })
        comp.refresh_from_db()
        self.assertEqual(comp.default_value, Decimal('750.0000'))

    def test_locked_component_cannot_be_updated(self):
        comp = PayrollComponent.objects.create(
            name='SSS', type='deduction', calculation_type='percentage',
            default_value=Decimal('4.5'), is_active=True, is_locked=True, sort_order=1,
        )
        resp = self.client.post(reverse('payroll:update_component', args=[comp.id]), {
            'name': 'SSS', 'type': 'deduction', 'operator': '-',
            'calculation_type': 'percentage', 'default_value': '99.0', 'is_active': 'true',
        })
        self.assertEqual(resp.status_code, 404)
        comp.refresh_from_db()
        self.assertEqual(comp.default_value, Decimal('4.5000'))  # unchanged

    def test_locked_component_cannot_be_deleted(self):
        comp = PayrollComponent.objects.create(
            name='SSS', type='deduction', calculation_type='percentage',
            default_value=Decimal('4.5'), is_active=True, is_locked=True, sort_order=1,
        )
        resp = self.client.post(reverse('payroll:delete_component', args=[comp.id]))
        self.assertEqual(resp.status_code, 404)
        self.assertTrue(PayrollComponent.objects.filter(pk=comp.id).exists())

    def test_delete_unused_component_is_hard_deleted(self):
        comp = PayrollComponent.objects.create(
            name='Temp', type='earning', calculation_type='fixed',
            default_value=Decimal('0'), is_active=True, sort_order=1,
        )
        self.client.post(reverse('payroll:delete_component', args=[comp.id]))
        self.assertFalse(PayrollComponent.objects.filter(pk=comp.id).exists())

    def test_delete_component_used_in_breakdown_is_soft_deactivated_not_deleted(self):
        comp = PayrollComponent.objects.create(
            name='Used Component', type='earning', calculation_type='fixed',
            default_value=Decimal('100'), is_active=True, sort_order=1,
        )
        payroll = Payroll.objects.create(
            employee=self.employee, payroll_period=self.period,
            basic_pay=Decimal('800'), gross_pay=Decimal('900'),
            total_deductions=Decimal('0'), net_pay=Decimal('900'),
        )
        PayrollBreakdown.objects.create(payroll=payroll, component=comp, amount=Decimal('100'))

        self.client.post(reverse('payroll:delete_component', args=[comp.id]))
        comp.refresh_from_db()
        self.assertFalse(comp.is_active)  # deactivated
        self.assertTrue(PayrollComponent.objects.filter(pk=comp.id).exists())  # not deleted

    def test_toggle_component_active_state(self):
        comp = PayrollComponent.objects.create(
            name='Allowance', type='earning', calculation_type='fixed',
            default_value=Decimal('500'), is_active=True, sort_order=1,
        )
        resp = self.client.post(
            reverse('payroll:toggle_component'),
            data=json.dumps({'id': comp.id}),
            content_type='application/json',
        )
        comp.refresh_from_db()
        self.assertFalse(comp.is_active)
        self.assertFalse(resp.json()['is_active'])

    def test_locked_component_cannot_be_toggled(self):
        comp = PayrollComponent.objects.create(
            name='SSS', type='deduction', calculation_type='percentage',
            default_value=Decimal('4.5'), is_active=True, is_locked=True, sort_order=1,
        )
        resp = self.client.post(
            reverse('payroll:toggle_component'),
            data=json.dumps({'id': comp.id}),
            content_type='application/json',
        )
        self.assertEqual(resp.status_code, 403)
        comp.refresh_from_db()
        self.assertTrue(comp.is_active)  # unchanged

    def test_reorder_components_updates_sort_order(self):
        c1 = PayrollComponent.objects.create(
            name='A', type='earning', calculation_type='fixed',
            default_value=Decimal('0'), sort_order=0, is_active=True,
        )
        c2 = PayrollComponent.objects.create(
            name='B', type='earning', calculation_type='fixed',
            default_value=Decimal('0'), sort_order=1, is_active=True,
        )
        self.client.post(
            reverse('payroll:reorder_components'),
            data=json.dumps({'order': [
                {'id': c2.id, 'order': 0, 'type': 'earning'},
                {'id': c1.id, 'order': 1, 'type': 'earning'},
            ]}),
            content_type='application/json',
        )
        c1.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual(c2.sort_order, 0)
        self.assertEqual(c1.sort_order, 1)

    def test_get_component_returns_full_data(self):
        comp = PayrollComponent.objects.create(
            name='Allowance', type='earning', operator='+', calculation_type='fixed',
            default_value=Decimal('500'), is_active=True, sort_order=1,
        )
        resp = self.client.get(reverse('payroll:get_component', args=[comp.id]))
        data = resp.json()
        self.assertEqual(data['name'], 'Allowance')
        self.assertEqual(data['operator'], '+')


# ═══════════════════════════════════════════════════════════════════════════════
#  PayrollPeriod — Create with overlap validation
# ═══════════════════════════════════════════════════════════════════════════════

class PayrollPeriodCRUDTests(PayrollCRUDTestBase):

    def test_create_non_overlapping_period_succeeds(self):
        self.client.post(reverse('payroll:periods'), {
            'start_date': '2026-06-16', 'end_date': '2026-06-30',
        })
        self.assertTrue(PayrollPeriod.objects.filter(start_date=date(2026, 6, 16)).exists())

    def test_create_overlapping_period_is_rejected(self):
        self.client.post(reverse('payroll:periods'), {
            'start_date': '2026-06-10', 'end_date': '2026-06-20',  # overlaps self.period
        })
        self.assertEqual(PayrollPeriod.objects.count(), 1)  # only the setUp fixture survives

    def test_create_period_with_end_before_start_is_rejected(self):
        self.client.post(reverse('payroll:periods'), {
            'start_date': '2026-07-15', 'end_date': '2026-07-01',
        })
        self.assertEqual(PayrollPeriod.objects.filter(start_date=date(2026, 7, 15)).count(), 0)

    def test_create_period_with_missing_dates_is_rejected(self):
        self.client.post(reverse('payroll:periods'), {
            'start_date': '', 'end_date': '',
        })
        self.assertEqual(PayrollPeriod.objects.count(), 1)

    def test_adjacent_non_overlapping_period_is_allowed(self):
        self.client.post(reverse('payroll:periods'), {
            'start_date': '2026-06-16', 'end_date': '2026-06-30',  # starts day after period ends
        })
        self.assertEqual(PayrollPeriod.objects.count(), 2)


# ═══════════════════════════════════════════════════════════════════════════════
#  Adjustment — Full CRUD
# ═══════════════════════════════════════════════════════════════════════════════

class AdjustmentCRUDTests(PayrollCRUDTestBase):

    def setUp(self):
        super().setUp()
        self.leave_type = LeaveType.objects.create(name='Sick Leave', is_paid=True, max_days=5)

    def test_create_overtime_adjustment_auto_computes_amount(self):
        self.client.post(reverse('payroll:adjustments'), {
            'action': 'create', 'employee_id': self.employee.id,
            'payroll_period_id': self.period.id, 'type': 'overtime',
            'hours': '3', 'description': 'Overtime on 2026-06-01',
        })
        adj = Adjustment.objects.get(employee=self.employee, type='overtime')
        # 3h x ot_rate (125.00) = 375.00 -- auto-computed, never trust a submitted amount
        self.assertEqual(adj.amount, Decimal('375.00'))

    def test_create_leave_adjustment_uses_manual_amount(self):
        self.client.post(reverse('payroll:adjustments'), {
            'action': 'create', 'employee_id': self.employee.id,
            'payroll_period_id': self.period.id, 'type': 'leave',
            'hours': '8', 'amount': '800.00',
            'leave_type_id': self.leave_type.id, 'description': 'Paid leave',
        })
        adj = Adjustment.objects.get(employee=self.employee, type='leave')
        self.assertEqual(adj.amount, Decimal('800.00'))

    def test_create_negative_leave_amount_for_unpaid_leave(self):
        self.client.post(reverse('payroll:adjustments'), {
            'action': 'create', 'employee_id': self.employee.id,
            'payroll_period_id': self.period.id, 'type': 'leave',
            'hours': '8', 'amount': '-800.00',
            'leave_type_id': self.leave_type.id, 'description': 'Unpaid leave',
        })
        adj = Adjustment.objects.get(employee=self.employee, type='leave')
        self.assertEqual(adj.amount, Decimal('-800.00'))

    def test_update_adjustment_recomputes_overtime_amount(self):
        adj = Adjustment.objects.create(
            employee=self.employee, payroll_period=self.period, type='overtime',
            hours=Decimal('2'), rate=Decimal('125'), amount=Decimal('250.00'),
            description='Original', created_by=self.admin,
        )
        self.client.post(reverse('payroll:adjustments'), {
            'action': 'update', 'adj_id': adj.id,
            'employee_id': self.employee.id, 'payroll_period_id': self.period.id,
            'type': 'overtime', 'hours': '5', 'description': 'Updated',
        })
        adj.refresh_from_db()
        # 5h x 125 = 625.00, recomputed live -- not whatever 'amount' was submitted
        self.assertEqual(adj.amount, Decimal('625.00'))

    def test_delete_adjustment(self):
        adj = Adjustment.objects.create(
            employee=self.employee, payroll_period=self.period, type='overtime',
            hours=Decimal('2'), rate=Decimal('125'), amount=Decimal('250.00'),
            description='To delete', created_by=self.admin,
        )
        self.client.post(reverse('payroll:adjustments'), {
            'action': 'delete', 'adj_id': adj.id,
        })
        self.assertFalse(Adjustment.objects.filter(pk=adj.id).exists())

    def test_list_view_filters_by_employee(self):
        other_emp = Employee.objects.create(
            employee_code='2026-0002', first_name='Maria', last_name='Santos',
            email='maria@example.com', employment_type='regular', status='active',
            salary_grade=self.grade,
        )
        Adjustment.objects.create(
            employee=self.employee, payroll_period=self.period, type='overtime',
            hours=Decimal('1'), rate=Decimal('125'), amount=Decimal('125.00'),
            description='Mine', created_by=self.admin,
        )
        Adjustment.objects.create(
            employee=other_emp, payroll_period=self.period, type='overtime',
            hours=Decimal('1'), rate=Decimal('125'), amount=Decimal('125.00'),
            description='Not mine', created_by=self.admin,
        )
        resp = self.client.get(reverse('payroll:adjustments'), {'emp_id': self.employee.id})
        self.assertContains(resp, 'Mine')

    def test_list_view_filters_by_type(self):
        Adjustment.objects.create(
            employee=self.employee, payroll_period=self.period, type='overtime',
            hours=Decimal('1'), rate=Decimal('125'), amount=Decimal('125.00'),
            description='OT entry', created_by=self.admin,
        )
        Adjustment.objects.create(
            employee=self.employee, payroll_period=self.period, type='leave',
            hours=Decimal('8'), amount=Decimal('800.00'),
            leave_type_id=self.leave_type.id, description='Leave entry', created_by=self.admin,
        )
        resp = self.client.get(reverse('payroll:adjustments'), {'type': 'leave'})
        self.assertContains(resp, 'Leave entry')
        self.assertNotContains(resp, 'OT entry')

"""
apps/employees/tests.py
═══════════════════════════════════════════════════════════════════════════════
CRUD tests for Employee, Department, Position, SalaryGrade, SystemUser,
Role, and Permission.

Run with:
    python manage.py test employees

All tests log in as a Django superuser, which the access-control layer
(apps.accounts.access.is_super_admin) always treats as SuperAdmin, so
every admin-only view is reachable without needing to seed Role/SystemUser
records just to pass the permission check.
═══════════════════════════════════════════════════════════════════════════════
"""

import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounts.models import Role, Permission, RolePermission, AuditLog
from .models import Employee, Department, Position, SalaryGrade, SystemUser


class EmployeeCRUDTestBase(TestCase):
    """Shared fixtures: a logged-in superadmin, a department/position/grade."""

    def setUp(self):
        self.admin = User.objects.create_superuser(
            'superadmin', 'admin@example.com', 'adminpass123'
        )
        self.client.login(username='superadmin', password='adminpass123')

        self.dept  = Department.objects.create(name='Operations', description='Ops dept')
        self.pos   = Position.objects.create(name='Staff', department=self.dept, base_salary=0)
        self.grade = SalaryGrade.objects.create(
            name='Grade 1', hourly_rate=Decimal('100.00'), overtime_rate=Decimal('125.00')
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  Employee — Create
# ═══════════════════════════════════════════════════════════════════════════════

class EmployeeCreateTests(EmployeeCRUDTestBase):

    def test_create_employee_with_explicit_code(self):
        resp = self.client.post(reverse('employees:create'), {
            'employee_code':   '2026-0099',
            'first_name':      'Maria',
            'last_name':       'Santos',
            'email':           'maria@example.com',
            'phone':           '09171234567',
            'date_hired':      '2026-01-15',
            'employment_type': 'regular',
            'status':          'active',
            'department_id':   self.dept.id,
            'position_id':     self.pos.id,
            'salary_grade_id': self.grade.id,
        })
        self.assertEqual(resp.status_code, 302)
        emp = Employee.objects.get(employee_code='2026-0099')
        self.assertEqual(emp.first_name, 'Maria')
        self.assertEqual(emp.department, self.dept)
        self.assertEqual(emp.salary_grade, self.grade)

    def test_create_employee_auto_generates_code_when_blank(self):
        resp = self.client.post(reverse('employees:create'), {
            'employee_code':   '',
            'first_name':      'Pedro',
            'last_name':       'Reyes',
            'email':           'pedro@example.com',
            'employment_type': 'regular',
            'status':          'active',
        })
        self.assertEqual(resp.status_code, 302)
        emp = Employee.objects.get(email='pedro@example.com')
        self.assertTrue(emp.employee_code)
        self.assertRegex(emp.employee_code, r'^\d{4}-\d{4}$')

    def test_create_employee_with_duplicate_code_auto_regenerates(self):
        Employee.objects.create(
            employee_code='2026-0001', first_name='X', last_name='Y',
            email='existing@example.com', employment_type='regular', status='active',
        )
        resp = self.client.post(reverse('employees:create'), {
            'employee_code':   '2026-0001',  # duplicate
            'first_name':      'New',
            'last_name':       'Person',
            'email':           'new@example.com',
            'employment_type': 'regular',
            'status':          'active',
        })
        emp = Employee.objects.get(email='new@example.com')
        self.assertNotEqual(emp.employee_code, '2026-0001')

    def test_create_writes_audit_log(self):
        self.client.post(reverse('employees:create'), {
            'employee_code':   '2026-0050',
            'first_name':      'Ana', 'last_name': 'Cruz',
            'email':           'ana@example.com',
            'employment_type': 'regular', 'status': 'active',
        })
        log = AuditLog.objects.filter(table_name='employees_employee', action='CREATE').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.new_value['employee_code'], '2026-0050')


# ═══════════════════════════════════════════════════════════════════════════════
#  Employee — Update
# ═══════════════════════════════════════════════════════════════════════════════

class EmployeeUpdateTests(EmployeeCRUDTestBase):

    def setUp(self):
        super().setUp()
        self.emp = Employee.objects.create(
            employee_code='2026-0001', first_name='Juan', last_name='Dela Cruz',
            email='juan@example.com', phone='09171111111',
            employment_type='regular', status='active',
            department=self.dept, position=self.pos, salary_grade=self.grade,
        )

    def test_update_basic_fields(self):
        resp = self.client.post(reverse('employees:update', args=[self.emp.id]), {
            'first_name': 'Juan Carlos', 'last_name': 'Dela Cruz',
            'email': 'juan@example.com', 'phone': '09179999999',
            'employment_type': 'regular', 'status': 'active',
        })
        self.assertEqual(resp.status_code, 302)
        self.emp.refresh_from_db()
        self.assertEqual(self.emp.first_name, 'Juan Carlos')
        self.assertEqual(self.emp.phone, '09179999999')

    def test_update_rejects_duplicate_employee_code(self):
        Employee.objects.create(
            employee_code='2026-0002', first_name='Other', last_name='Person',
            email='other@example.com', employment_type='regular', status='active',
        )
        self.client.post(reverse('employees:update', args=[self.emp.id]), {
            'employee_code': '2026-0002',  # already used by someone else
            'first_name': 'Juan', 'last_name': 'Dela Cruz',
            'email': 'juan@example.com',
            'employment_type': 'regular', 'status': 'active',
        })
        self.emp.refresh_from_db()
        self.assertEqual(self.emp.employee_code, '2026-0001')  # unchanged

    def test_update_does_not_clear_unspecified_fk_fields(self):
        """
        Submitting the edit form without department_id must NOT wipe the
        employee's existing department — a blank field here means
        'unchanged', not 'clear this'.
        """
        self.client.post(reverse('employees:update', args=[self.emp.id]), {
            'first_name': 'Juan', 'last_name': 'Dela Cruz',
            'email': 'juan@example.com',
            'employment_type': 'regular', 'status': 'active',
            # department_id intentionally omitted
        })
        self.emp.refresh_from_db()
        self.assertEqual(self.emp.department, self.dept)

    def test_update_writes_audit_log_with_before_after_values(self):
        self.client.post(reverse('employees:update', args=[self.emp.id]), {
            'first_name': 'Juanito', 'last_name': 'Dela Cruz',
            'email': 'juan@example.com',
            'employment_type': 'regular', 'status': 'active',
        })
        log = AuditLog.objects.filter(
            table_name='employees_employee', action='UPDATE', record_id=self.emp.id
        ).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.old_value['first_name'], 'Juan')
        self.assertEqual(log.new_value['first_name'], 'Juanito')


# ═══════════════════════════════════════════════════════════════════════════════
#  Employee — Delete (soft)
# ═══════════════════════════════════════════════════════════════════════════════

class EmployeeDeleteTests(EmployeeCRUDTestBase):

    def setUp(self):
        super().setUp()
        self.emp = Employee.objects.create(
            employee_code='2026-0001', first_name='Juan', last_name='Dela Cruz',
            email='juan@example.com', employment_type='regular', status='active',
        )

    def test_delete_is_soft_delete_not_hard_delete(self):
        resp = self.client.post(reverse('employees:delete', args=[self.emp.id]))
        self.assertEqual(resp.status_code, 302)
        self.emp.refresh_from_db()
        self.assertEqual(self.emp.status, 'inactive')
        self.assertTrue(Employee.objects.filter(pk=self.emp.id).exists())  # row preserved

    def test_delete_requires_post(self):
        resp = self.client.get(reverse('employees:delete', args=[self.emp.id]))
        self.assertEqual(resp.status_code, 405)


# ═══════════════════════════════════════════════════════════════════════════════
#  Employee — Read
# ═══════════════════════════════════════════════════════════════════════════════

class EmployeeReadTests(EmployeeCRUDTestBase):

    def setUp(self):
        super().setUp()
        self.emp = Employee.objects.create(
            employee_code='2026-0001', first_name='Juan', last_name='Dela Cruz',
            email='juan@example.com', employment_type='regular', status='active',
        )

    def test_list_view_returns_200(self):
        resp = self.client.get(reverse('employees:list'))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Juan')

    def test_list_view_search_filters_results(self):
        Employee.objects.create(
            employee_code='2026-0002', first_name='Maria', last_name='Santos',
            email='maria@example.com', employment_type='regular', status='active',
        )
        resp = self.client.get(reverse('employees:list'), {'q': 'Maria'})
        self.assertContains(resp, 'Maria')
        self.assertNotContains(resp, 'Dela Cruz')

    def test_detail_view_returns_200(self):
        resp = self.client.get(reverse('employees:detail', args=[self.emp.id]))
        self.assertEqual(resp.status_code, 200)

    def test_get_json_returns_employee_data(self):
        resp = self.client.get(reverse('employees:get_json', args=[self.emp.id]))
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['employee_code'], '2026-0001')
        self.assertEqual(data['first_name'], 'Juan')


# ═══════════════════════════════════════════════════════════════════════════════
#  Department + Position — Full CRUD
# ═══════════════════════════════════════════════════════════════════════════════

class DepartmentCRUDTests(EmployeeCRUDTestBase):

    def test_create_department(self):
        self.client.post(reverse('employees:departments'), {
            'action': 'create', 'name': 'Finance', 'description': 'Money stuff',
        })
        self.assertTrue(Department.objects.filter(name='Finance').exists())

    def test_update_department(self):
        self.client.post(reverse('employees:departments'), {
            'action': 'update', 'dept_id': self.dept.id,
            'name': 'Operations Renamed', 'description': 'Updated desc',
        })
        self.dept.refresh_from_db()
        self.assertEqual(self.dept.name, 'Operations Renamed')

    def test_delete_department_unassigns_employees_not_deletes_them(self):
        emp = Employee.objects.create(
            employee_code='2026-0001', first_name='Juan', last_name='Cruz',
            email='juan@example.com', employment_type='regular', status='active',
            department=self.dept,
        )
        self.client.post(reverse('employees:departments'), {
            'action': 'delete', 'dept_id': self.dept.id,
        })
        self.assertFalse(Department.objects.filter(pk=self.dept.id).exists())
        emp.refresh_from_db()
        self.assertIsNone(emp.department)
        self.assertTrue(Employee.objects.filter(pk=emp.id).exists())  # employee survives

    def test_create_position_under_department(self):
        self.client.post(reverse('employees:departments'), {
            'action': 'create_position', 'dept_id': self.dept.id,
            'pos_name': 'Supervisor', 'base_salary': '25000',
        })
        self.assertTrue(Position.objects.filter(name='Supervisor', department=self.dept).exists())

    def test_create_position_requires_department_and_name(self):
        self.client.post(reverse('employees:departments'), {
            'action': 'create_position', 'dept_id': '', 'pos_name': '',
        })
        self.assertEqual(Position.objects.count(), 1)  # only the setUp fixture

    def test_update_position_can_move_to_different_department(self):
        other_dept = Department.objects.create(name='IT')
        self.client.post(reverse('employees:departments'), {
            'action': 'update_position', 'pos_id': self.pos.id,
            'pos_name': 'Staff', 'base_salary': '0', 'dept_id': other_dept.id,
        })
        self.pos.refresh_from_db()
        self.assertEqual(self.pos.department, other_dept)

    def test_delete_position_unassigns_employees_not_deletes_them(self):
        emp = Employee.objects.create(
            employee_code='2026-0001', first_name='Juan', last_name='Cruz',
            email='juan@example.com', employment_type='regular', status='active',
            position=self.pos,
        )
        self.client.post(reverse('employees:departments'), {
            'action': 'delete_position', 'pos_id': self.pos.id,
        })
        self.assertFalse(Position.objects.filter(pk=self.pos.id).exists())
        emp.refresh_from_db()
        self.assertIsNone(emp.position)

    def test_position_is_correctly_scoped_to_its_own_department(self):
        """
        Verifies the FK connection actually filters correctly: a position
        under Department A must never show up when listing positions for
        Department B.
        """
        other_dept = Department.objects.create(name='IT')
        Position.objects.create(name='Developer', department=other_dept, base_salary=0)

        resp = self.client.get(reverse('employees:positions_by_dept'), {'dept_id': self.dept.id})
        names = [p['name'] for p in resp.json()['positions']]
        self.assertIn('Staff', names)
        self.assertNotIn('Developer', names)

    def test_positions_by_dept_includes_salary_and_employee_count(self):
        Employee.objects.create(
            employee_code='2026-0001', first_name='Juan', last_name='Cruz',
            email='juan@example.com', employment_type='regular', status='active',
            position=self.pos,
        )
        resp = self.client.get(reverse('employees:positions_by_dept'), {'dept_id': self.dept.id})
        staff_entry = next(p for p in resp.json()['positions'] if p['name'] == 'Staff')
        self.assertEqual(staff_entry['emp_count'], 1)


# ═══════════════════════════════════════════════════════════════════════════════
#  SalaryGrade — Full CRUD
# ═══════════════════════════════════════════════════════════════════════════════

class SalaryGradeCRUDTests(EmployeeCRUDTestBase):

    def test_create_grade_stores_hourly_rate_as_primary_field(self):
        self.client.post(reverse('employees:create_grade'), {
            'name': 'Grade 2', 'hourly_rate': '150.00', 'overtime_rate': '187.50',
        })
        grade = SalaryGrade.objects.get(name='Grade 2')
        self.assertEqual(grade.hourly_rate, Decimal('150.00'))

    def test_create_grade_auto_computes_base_salary_reference(self):
        self.client.post(reverse('employees:create_grade'), {
            'name': 'Grade 3', 'hourly_rate': '100.00', 'overtime_rate': '125.00',
        })
        grade = SalaryGrade.objects.get(name='Grade 3')
        # base_salary = hourly_rate x 8 x 22 = 17,600.00 -- auto-computed reference value
        self.assertEqual(grade.base_salary, Decimal('17600.00'))

    def test_update_grade_recomputes_base_salary(self):
        self.client.post(reverse('employees:update_grade', args=[self.grade.id]), {
            'name': 'Grade 1', 'hourly_rate': '200.00', 'overtime_rate': '250.00',
        })
        self.grade.refresh_from_db()
        self.assertEqual(self.grade.hourly_rate, Decimal('200.00'))
        self.assertEqual(self.grade.base_salary, Decimal('35200.00'))  # 200 x 8 x 22

    def test_delete_grade_unassigns_employees_not_deletes_them(self):
        emp = Employee.objects.create(
            employee_code='2026-0001', first_name='Juan', last_name='Cruz',
            email='juan@example.com', employment_type='regular', status='active',
            salary_grade=self.grade,
        )
        self.client.post(reverse('employees:delete_grade', args=[self.grade.id]))
        self.assertFalse(SalaryGrade.objects.filter(pk=self.grade.id).exists())
        emp.refresh_from_db()
        self.assertIsNone(emp.salary_grade)

    def test_get_grade_json_returns_hourly_rate(self):
        resp = self.client.get(reverse('employees:get_grade', args=[self.grade.id]))
        data = resp.json()
        self.assertEqual(data['hourly_rate'], str(self.grade.hourly_rate))


# ═══════════════════════════════════════════════════════════════════════════════
#  SystemUser — Full CRUD
# ═══════════════════════════════════════════════════════════════════════════════

class SystemUserCRUDTests(EmployeeCRUDTestBase):

    def setUp(self):
        super().setUp()
        self.role = Role.objects.create(name='HRAdmin', description='HR access')
        self.emp = Employee.objects.create(
            employee_code='2026-0001', first_name='Juan', last_name='Cruz',
            email='juan@example.com', employment_type='regular', status='active',
        )

    def test_create_user_creates_both_auth_user_and_systemuser(self):
        self.client.post(reverse('employees:users'), {
            'action': 'create', 'username': 'jcruz', 'password': 'pass12345',
            'employee_id': self.emp.id, 'role_id': self.role.id, 'is_active': 'true',
        })
        self.assertTrue(User.objects.filter(username='jcruz').exists())
        sys_user = SystemUser.objects.get(username='jcruz')
        self.assertEqual(sys_user.employee, self.emp)
        self.assertEqual(sys_user.role, self.role)

    def test_create_user_rejects_duplicate_username(self):
        User.objects.create_user('jcruz', password='x')
        self.client.post(reverse('employees:users'), {
            'action': 'create', 'username': 'jcruz', 'password': 'pass12345',
            'is_active': 'true',
        })
        self.assertEqual(SystemUser.objects.filter(username='jcruz').count(), 0)

    def test_update_user_changes_role(self):
        auth = User.objects.create_user('jcruz', password='pass12345')
        sys_user = SystemUser.objects.create(
            username='jcruz', password_hash=auth.password,
            employee=self.emp, role=None, is_active=True,
        )
        self.client.post(reverse('employees:users'), {
            'action': 'update', 'user_id': sys_user.id,
            'employee_id': self.emp.id, 'role_id': self.role.id, 'is_active': 'true',
        })
        sys_user.refresh_from_db()
        self.assertEqual(sys_user.role, self.role)

    def test_superadmin_role_grants_django_staff_flag(self):
        super_role = Role.objects.create(name='SuperAdmin')
        auth = User.objects.create_user('jcruz', password='pass12345')
        sys_user = SystemUser.objects.create(
            username='jcruz', password_hash=auth.password,
            employee=self.emp, role=None, is_active=True,
        )
        self.client.post(reverse('employees:users'), {
            'action': 'update', 'user_id': sys_user.id,
            'employee_id': self.emp.id, 'role_id': super_role.id, 'is_active': 'true',
        })
        auth.refresh_from_db()
        self.assertTrue(auth.is_staff)

    def test_reset_password_rejects_mismatched_confirmation(self):
        auth = User.objects.create_user('jcruz', password='oldpass123')
        sys_user = SystemUser.objects.create(
            username='jcruz', password_hash=auth.password, employee=self.emp, is_active=True,
        )
        self.client.post(reverse('employees:users'), {
            'action': 'reset_password', 'user_id': sys_user.id,
            'new_password': 'newpass123', 'confirm_password': 'different456',
        })
        auth.refresh_from_db()
        self.assertTrue(auth.check_password('oldpass123'))

    def test_reset_password_succeeds_with_matching_confirmation(self):
        auth = User.objects.create_user('jcruz', password='oldpass123')
        sys_user = SystemUser.objects.create(
            username='jcruz', password_hash=auth.password, employee=self.emp, is_active=True,
        )
        self.client.post(reverse('employees:users'), {
            'action': 'reset_password', 'user_id': sys_user.id,
            'new_password': 'newpass123', 'confirm_password': 'newpass123',
        })
        auth.refresh_from_db()
        self.assertTrue(auth.check_password('newpass123'))

    def test_deactivate_user_syncs_to_auth_user(self):
        auth = User.objects.create_user('jcruz', password='pass12345', is_active=True)
        sys_user = SystemUser.objects.create(
            username='jcruz', password_hash=auth.password, employee=self.emp, is_active=True,
        )
        self.client.post(reverse('employees:users'), {
            'action': 'deactivate', 'user_id': sys_user.id,
        })
        sys_user.refresh_from_db()
        auth.refresh_from_db()
        self.assertFalse(sys_user.is_active)
        self.assertFalse(auth.is_active)

    def test_activate_user_syncs_to_auth_user(self):
        auth = User.objects.create_user('jcruz', password='pass12345', is_active=False)
        sys_user = SystemUser.objects.create(
            username='jcruz', password_hash=auth.password, employee=self.emp, is_active=False,
        )
        self.client.post(reverse('employees:users'), {
            'action': 'activate', 'user_id': sys_user.id,
        })
        sys_user.refresh_from_db()
        auth.refresh_from_db()
        self.assertTrue(sys_user.is_active)
        self.assertTrue(auth.is_active)


# ═══════════════════════════════════════════════════════════════════════════════
#  Role + Permission — Full CRUD
# ═══════════════════════════════════════════════════════════════════════════════

class RolePermissionCRUDTests(EmployeeCRUDTestBase):

    def test_create_role(self):
        self.client.post(reverse('employees:roles'), {
            'action': 'create', 'name': 'PayrollViewer', 'description': 'View-only payroll',
        })
        self.assertTrue(Role.objects.filter(name='PayrollViewer').exists())

    def test_default_roles_cannot_be_deleted(self):
        role = Role.objects.create(name='SuperAdmin')
        self.client.post(reverse('employees:roles'), {
            'action': 'delete', 'role_id': role.id,
        })
        self.assertTrue(Role.objects.filter(pk=role.id).exists())

    def test_custom_role_can_be_deleted(self):
        role = Role.objects.create(name='TempRole')
        self.client.post(reverse('employees:roles'), {
            'action': 'delete', 'role_id': role.id,
        })
        self.assertFalse(Role.objects.filter(pk=role.id).exists())

    def test_create_permission(self):
        self.client.post(reverse('employees:roles'), {
            'action': 'create_permission', 'perm_name': 'View Payroll', 'perm_code': 'payroll.view',
        })
        self.assertTrue(Permission.objects.filter(code='payroll.view').exists())

    def test_create_permission_rejects_duplicate_code(self):
        Permission.objects.create(name='Existing', code='payroll.view')
        self.client.post(reverse('employees:roles'), {
            'action': 'create_permission', 'perm_name': 'Another', 'perm_code': 'payroll.view',
        })
        self.assertEqual(Permission.objects.filter(code='payroll.view').count(), 1)

    def test_assign_permissions_to_role(self):
        role  = Role.objects.create(name='Custom')
        perm1 = Permission.objects.create(name='P1', code='p1.view')
        perm2 = Permission.objects.create(name='P2', code='p2.view')

        self.client.post(reverse('employees:roles'), {
            'action': 'assign_permissions', 'role_id': role.id,
            'permission_ids': [perm1.id, perm2.id],
        })
        assigned = RolePermission.objects.filter(role=role).values_list('permission_id', flat=True)
        self.assertCountEqual(list(assigned), [perm1.id, perm2.id])

    def test_reassigning_permissions_replaces_old_set_entirely(self):
        role  = Role.objects.create(name='Custom')
        perm1 = Permission.objects.create(name='P1', code='p1.view')
        perm2 = Permission.objects.create(name='P2', code='p2.view')
        RolePermission.objects.create(role=role, permission=perm1)

        self.client.post(reverse('employees:roles'), {
            'action': 'assign_permissions', 'role_id': role.id,
            'permission_ids': [perm2.id],
        })
        assigned = list(RolePermission.objects.filter(role=role).values_list('permission_id', flat=True))
        self.assertEqual(assigned, [perm2.id])  # perm1 removed, perm2 added


# ═══════════════════════════════════════════════════════════════════════════════
#  Change role directly from the employee profile page
# ═══════════════════════════════════════════════════════════════════════════════

class UpdateEmployeeRoleTests(EmployeeCRUDTestBase):

    def setUp(self):
        super().setUp()
        self.role = Role.objects.create(name='HRAdmin')
        self.emp = Employee.objects.create(
            employee_code='2026-0001', first_name='Juan', last_name='Cruz',
            email='juan@example.com', employment_type='regular', status='active',
        )
        auth = User.objects.create_user('jcruz', password='pass12345')
        self.sys_user = SystemUser.objects.create(
            username='jcruz', password_hash=auth.password, employee=self.emp, is_active=True,
        )

    def test_superadmin_can_change_role(self):
        self.client.post(
            reverse('employees:update_employee_role', args=[self.emp.id]),
            {'role_id': self.role.id},
        )
        self.sys_user.refresh_from_db()
        self.assertEqual(self.sys_user.role, self.role)

    def test_non_superadmin_cannot_change_role(self):
        self.client.logout()
        User.objects.create_user('regular', password='pass12345')
        self.client.login(username='regular', password='pass12345')

        self.client.post(
            reverse('employees:update_employee_role', args=[self.emp.id]),
            {'role_id': self.role.id},
        )
        self.sys_user.refresh_from_db()
        self.assertIsNone(self.sys_user.role)  # blocked, unchanged

"""
apps/accounts/tests.py
Verifies the exact 3-tier permission matrix in access.py.

Run with:
    python manage.py test accounts
"""
from django.contrib.auth.models import User
from django.test import TestCase

from employees.models import Employee, SystemUser
from .models import Role
from .access import (
    get_user_role, has_admin_role, is_super_admin, is_hr_admin, is_normal_admin,
    role_display_name, SUPERADMIN, HRADMIN, STAFFADMIN,
)


class RoleMatrixTestBase(TestCase):

    def setUp(self):
        self.super_role = Role.objects.create(name=SUPERADMIN)
        self.hr_role    = Role.objects.create(name=HRADMIN)
        self.staff_role = Role.objects.create(name=STAFFADMIN)

        self.emp = Employee.objects.create(
            employee_code='2026-0001', first_name='Test', last_name='User',
            email='test@example.com', employment_type='regular', status='active',
        )

    def _make_user_with_role(self, username, role):
        auth = User.objects.create_user(username, password='x')
        SystemUser.objects.create(
            username=username, password_hash=auth.password,
            employee=self.emp, role=role, is_active=True,
        )
        return auth


class SuperAdminMatrixTests(RoleMatrixTestBase):

    def test_django_superuser_is_always_superadmin_even_without_systemuser(self):
        su = User.objects.create_superuser('djsuper', 'x@x.com', 'x')
        self.assertTrue(is_super_admin(su))
        self.assertTrue(is_hr_admin(su))       # superset
        self.assertTrue(has_admin_role(su))
        self.assertFalse(is_normal_admin(su))  # not the restricted tier

    def test_superadmin_role_passes_every_check(self):
        user = self._make_user_with_role('boss', self.super_role)
        self.assertTrue(is_super_admin(user))
        self.assertTrue(is_hr_admin(user))
        self.assertTrue(has_admin_role(user))
        self.assertFalse(is_normal_admin(user))


class HRAdminMatrixTests(RoleMatrixTestBase):

    def test_hradmin_passes_hr_check_but_not_superadmin_check(self):
        user = self._make_user_with_role('hrperson', self.hr_role)
        self.assertFalse(is_super_admin(user))
        self.assertTrue(is_hr_admin(user))     # full admin tier includes HR
        self.assertTrue(has_admin_role(user))
        self.assertFalse(is_normal_admin(user))


class NormalAdminMatrixTests(RoleMatrixTestBase):

    def test_staffadmin_only_passes_broadest_check(self):
        user = self._make_user_with_role('clerk', self.staff_role)
        self.assertFalse(is_super_admin(user))
        self.assertFalse(is_hr_admin(user))    # NOT full admin -- this is the whole point
        self.assertTrue(has_admin_role(user))  # still "some kind of admin"
        self.assertTrue(is_normal_admin(user))

    def test_normal_admin_label_is_shown_instead_of_staffadmin(self):
        self.assertEqual(role_display_name(self.staff_role), 'Normal Admin')
        self.assertEqual(role_display_name('StaffAdmin'), 'Normal Admin')


class NoRoleTests(RoleMatrixTestBase):

    def test_employee_with_no_systemuser_has_no_admin_access_at_all(self):
        plain_user = User.objects.create_user('justanemployee', password='x')
        self.assertFalse(is_super_admin(plain_user))
        self.assertFalse(is_hr_admin(plain_user))
        self.assertFalse(is_normal_admin(plain_user))
        self.assertFalse(has_admin_role(plain_user))
        self.assertIsNone(get_user_role(plain_user))

    def test_systemuser_with_role_none_has_no_admin_access(self):
        user = self._make_user_with_role('norole', None)
        self.assertFalse(has_admin_role(user))

    def test_inactive_systemuser_has_no_admin_access_even_with_a_role(self):
        auth = User.objects.create_user('disabled', password='x')
        SystemUser.objects.create(
            username='disabled', password_hash=auth.password,
            employee=self.emp, role=self.super_role, is_active=False,  # deactivated
        )
        self.assertFalse(has_admin_role(auth))
        self.assertFalse(is_super_admin(auth))


class RoleDisplayNameTests(TestCase):

    def test_all_three_roles_have_friendly_labels(self):
        self.assertEqual(role_display_name(SUPERADMIN), 'Super Admin')
        self.assertEqual(role_display_name(HRADMIN), 'HR Admin')
        self.assertEqual(role_display_name(STAFFADMIN), 'Normal Admin')

    def test_unknown_role_name_falls_back_to_itself(self):
        self.assertEqual(role_display_name('SomeCustomThing'), 'SomeCustomThing')

    def test_none_role_falls_back_to_no_role_label(self):
        self.assertEqual(role_display_name(None), 'No Role')

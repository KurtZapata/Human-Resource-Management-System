"""
apps/employees/utils.py
────────────────────────────────────────────────────────────────────────────
Utility functions for:
  1. Auto-generating employee codes in format YYYY-NNNN  (e.g. 2026-0001)
  2. Auto-generating a system username from the employee's name
  3. Auto-generating a random temporary password
  4. Creating the linked Django auth.User + SystemUser in one call

Place this file at:  apps/employees/utils.py
────────────────────────────────────────────────────────────────────────────
"""

import random
import string
from datetime import date

from django.contrib.auth.models import User as AuthUser
from django.utils import timezone


# ── 1. Employee Code Generator ────────────────────────────────────────────────

def generate_employee_code():
    """
    Returns the next available employee code in YYYY-NNNN format.
    Examples:  2026-0001 / 2026-0002 / 2027-0001

    Logic:
      - Uses the current year.
      - Finds the highest existing numeric suffix for that year.
      - Increments by 1, zero-padded to 4 digits.
      - Thread-safe enough for normal HR usage (single-office scenario).
        For high-concurrency, wrap the save in a DB transaction with select_for_update.
    """
    from .models import Employee

    year = date.today().year
    prefix = f"{year}-"

    # Grab all codes for the current year
    existing = Employee.objects.filter(
        employee_code__startswith=prefix
    ).values_list('employee_code', flat=True)

    max_num = 0
    for code in existing:
        try:
            num = int(code.split('-')[1])
            if num > max_num:
                max_num = num
        except (IndexError, ValueError):
            pass

    next_num = max_num + 1
    return f"{year}-{next_num:04d}"


# ── 2. Username Generator ─────────────────────────────────────────────────────

def generate_username(first_name, last_name):
    """
    Generates a unique username from the employee's name.
    Pattern: first initial + last name, all lowercase, no spaces.
    Examples:
      Juan Dela Cruz  →  jdelacruz
      Maria Santos    →  msantos

    If the base username is taken, appends a number:
      jdelacruz2, jdelacruz3, ...
    """
    base = (first_name[:1] + last_name).lower()
    # Remove spaces and special characters
    base = ''.join(c for c in base if c.isalnum())

    if not AuthUser.objects.filter(username=base).exists():
        return base

    # Append incrementing number until unique
    counter = 2
    while True:
        candidate = f"{base}{counter}"
        if not AuthUser.objects.filter(username=candidate).exists():
            return candidate
        counter += 1


# ── 3. Password Generator ─────────────────────────────────────────────────────

def generate_temp_password(length=10):
    """
    Generates a random temporary password.
    Format: 2 uppercase + 4 lowercase + 2 digits + 2 special chars, shuffled.
    Always meets standard password complexity requirements.
    """
    upper   = random.choices(string.ascii_uppercase, k=2)
    lower   = random.choices(string.ascii_lowercase, k=4)
    digits  = random.choices(string.digits, k=2)
    special = random.choices('@#$%&*!', k=2)

    pwd_chars = upper + lower + digits + special
    random.shuffle(pwd_chars)
    return ''.join(pwd_chars)


# ── 4. Create Auth User + SystemUser ─────────────────────────────────────────

def create_system_user_for_employee(employee, password=None, role_id=None):
    """
    Creates a Django auth.User and a linked SystemUser for a new employee.

    Args:
        employee:  The saved Employee model instance.
        password:  Optional. If None, a random temp password is generated.
        role_id:   Optional. Defaults to the lowest-privilege role (StaffAdmin).

    Returns:
        dict with keys: username, password, role_name
        so the view can show them in a confirmation popup.
    """
    from .models import SystemUser
    from accounts.models import Role

    username = generate_username(employee.first_name, employee.last_name)
    temp_pwd = password or generate_temp_password()

    # Resolve default role — use StaffAdmin if no role specified
    if role_id:
        try:
            role = Role.objects.get(pk=role_id)
        except Role.DoesNotExist:
            role = _get_default_role()
    else:
        role = _get_default_role()

    # Create Django auth user
    auth_user = AuthUser.objects.create_user(
        username   = username,
        password   = temp_pwd,
        first_name = employee.first_name,
        last_name  = employee.last_name,
        email      = employee.email,
        is_active  = True,
    )

    # Create SystemUser record
    SystemUser.objects.create(
        username      = username,
        password_hash = auth_user.password,   # stores hashed version
        employee      = employee,
        role          = role,
        is_active     = True,
    )

    return {
        'username':  username,
        'password':  temp_pwd,              # plain text — shown ONCE then discarded
        'role_name': role.name if role else 'None',
    }


def _get_default_role():
    """Returns StaffAdmin role, or the first available role, or None."""
    from accounts.models import Role
    return (
        Role.objects.filter(name='StaffAdmin').first()
        or Role.objects.order_by('id').last()
    )

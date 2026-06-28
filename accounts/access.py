"""
apps/accounts/access.py  (REPLACES the existing file entirely)
═══════════════════════════════════════════════════════════════════════════════
Hardcoded 3-tier role-based access control. No Role/Permission CRUD —
exactly 3 roles always exist (seeded by migration), and what each one can
reach is defined here as plain Python, not configurable data.

THE MATRIX:
  SuperAdmin   -> everything
  HRAdmin      -> everything except Company Settings + the system-wide
                  Audit Log
  StaffAdmin   -> ("Normal Admin" in the UI) ONLY:
                    - Employee / Department / Position / Salary Grade CRUD
                    - View Payroll Report & Payslips (read-only, cannot
                      run payroll, cannot touch Adjustments/Components/
                      Periods, cannot confirm payroll)
                  No access to: Attendance, OTP Manager, Calendar,
                  Salary Components, Adjustments, Run Payroll, Users,
                  Company Settings, Audit Log.

NOTE ON NAMING: the DB value stays 'StaffAdmin' (already baked into the
test suite and migrations) — only the label shown in the UI changes to
"Normal Admin". See ROLE_DISPLAY_NAMES below.
═══════════════════════════════════════════════════════════════════════════════
"""

from functools import wraps
from django.shortcuts import redirect, render
from django.contrib.auth.decorators import login_required

# ── The exact 3 roles that may ever exist ─────────────────────────────────────
SUPERADMIN = 'SuperAdmin'
HRADMIN    = 'HRAdmin'
STAFFADMIN = 'StaffAdmin'   # shown to users as "Normal Admin"

ADMIN_ROLES = {SUPERADMIN, HRADMIN, STAFFADMIN}

# UI label override — use this anywhere a role name is displayed to a person
ROLE_DISPLAY_NAMES = {
    SUPERADMIN: 'Super Admin',
    HRADMIN:    'HR Admin',
    STAFFADMIN: 'Normal Admin',
}


def role_display_name(role):
    """Returns the human-facing label for a Role instance or role name string."""
    name = role.name if hasattr(role, 'name') else role
    return ROLE_DISPLAY_NAMES.get(name, name or 'No Role')


# ── Public paths — never require login or a role ──────────────────────────────
PUBLIC_PATHS = {
    '/login/', '/accounts/login/',
    '/attendance/', '/attendance/log/',
    '/attendance/employee-login/', '/attendance/employee-logout/',
}
PUBLIC_PREFIXES = ('/static/', '/media/', '/admin/')


class _MockRole:
    """Lets a Django superuser pass every check without needing a SystemUser row."""
    def __init__(self, name):
        self.name = name


# ═══════════════════════════════════════════════════════════════════════════════
#  Role lookup
# ═══════════════════════════════════════════════════════════════════════════════

def get_user_role(user):
    """Returns the Role linked to this Django user via SystemUser, or None."""
    if not user or not user.is_authenticated:
        return None
    if user.is_superuser:
        return _MockRole(SUPERADMIN)
    try:
        from employees.models import SystemUser
        su = SystemUser.objects.select_related('role').get(
            username=user.username, is_active=True
        )
        return su.role
    except Exception:
        return None


def has_admin_role(user):
    """True for any of the 3 admin roles (the broadest check)."""
    role = get_user_role(user)
    return role is not None and role.name in ADMIN_ROLES


def is_super_admin(user):
    if getattr(user, 'is_superuser', False):
        return True
    role = get_user_role(user)
    return role is not None and role.name == SUPERADMIN


def is_hr_admin(user):
    """True for SuperAdmin OR HRAdmin — the 'full admin' tier."""
    if getattr(user, 'is_superuser', False):
        return True
    role = get_user_role(user)
    return role is not None and role.name in {SUPERADMIN, HRADMIN}


def is_normal_admin(user):
    """True ONLY for the restricted StaffAdmin / 'Normal Admin' tier."""
    role = get_user_role(user)
    return role is not None and role.name == STAFFADMIN


# ═══════════════════════════════════════════════════════════════════════════════
#  Decorator
# ═══════════════════════════════════════════════════════════════════════════════

def admin_required(view_func=None, *, roles=None, redirect_url=None):
    """
    @admin_required                              -> any of the 3 roles
    @admin_required(roles={'SuperAdmin'})        -> SuperAdmin only
    @admin_required(roles={'SuperAdmin','HRAdmin'}) -> SuperAdmin or HRAdmin
    """
    allowed = set(roles) if roles else ADMIN_ROLES
    redir   = redirect_url or '/attendance/'

    def decorator(fn):
        @wraps(fn)
        @login_required(login_url='/login/')
        def wrapper(request, *args, **kwargs):
            role = get_user_role(request.user)
            if role and role.name in allowed:
                return fn(request, *args, **kwargs)
            return render(request, 'hrms/403.html', status=403)
        return wrapper

    if view_func is not None:
        return decorator(view_func)
    return decorator


# ═══════════════════════════════════════════════════════════════════════════════
#  Middleware — coarse "are you any kind of admin" gate at the URL-prefix level.
#  Fine-grained per-role restriction (e.g. StaffAdmin blocked from Attendance)
#  lives on the individual views via @admin_required(roles={...}) — see the
#  decorator map in the delivery notes for exactly which view gets which.
# ═══════════════════════════════════════════════════════════════════════════════

class AdminAccessMiddleware:

    ADMIN_PREFIXES = (
        '/employees/', '/payroll/', '/attendance/admin/',
        '/attendance/otp/', '/calendar/', '/accounts/audit-log/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        if path in PUBLIC_PATHS:
            return self.get_response(request)
        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return self.get_response(request)

        if any(path.startswith(p) for p in self.ADMIN_PREFIXES):
            if not request.user.is_authenticated:
                return redirect(f'/login/?next={path}')
            if not has_admin_role(request.user):
                return render(request, 'hrms/403.html', status=403)

        return self.get_response(request)

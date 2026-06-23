"""
accounts/access.py
═══════════════════════════════════════════════════════════════════════════════
Role-based access control for the HRMS admin pages.

Provides:
  1. get_user_role()        — fetches role metadata or yields superuser bypasses
  2. has_admin_role()       — structural checker to see if user matches any admin profile
  3. admin_required         — view decorator for fine-grained protection
  4. AdminAccessMiddleware  — blanket middleware that blocks non-admins globally
═══════════════════════════════════════════════════════════════════════════════
"""

from functools import wraps
from django.shortcuts import redirect, render
from django.contrib.auth.decorators import login_required

# ── Roles considered "admin" ──────────────────────────────────────────────────
ADMIN_ROLES = {'SuperAdmin', 'HRAdmin', 'StaffAdmin'}

# ── URLs that are always public (no login or role check required) ─────────────
PUBLIC_PATHS = {
    '/login/',
    '/accounts/login/',
    '/attendance/',
    '/attendance/log/',
    '/attendance/employee-login/',
    '/attendance/employee-logout/',
}

# Paths that start with these prefixes bypass admin checks automatically
PUBLIC_PREFIXES = (
    '/static/',
    '/media/',
    '/admin/',      # Django's built-in core admin (protected natively)
)


# ═══════════════════════════════════════════════════════════════════════════════
#  1. ROLE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

class _MockRole:
    """Lightweight stand-in so native superusers don't need an explicit SystemUser record."""
    def __init__(self, name):
        self.name = name


def get_user_role(user):
    """
    Returns the Role object linked to this Django user via SystemUser,
    or None if no role is explicitly assigned.
    """
    if not user or not user.is_authenticated:
        return None
    # Django superusers always bypass and inherit SuperAdmin privileges
    if user.is_superuser:
        return _MockRole('SuperAdmin')
    try:
        from employees.models import SystemUser
        su = SystemUser.objects.select_related('role').get(
            username=user.username, is_active=True
        )
        return su.role
    except Exception:
        return None


def has_admin_role(user):
    """Returns True if the user matches any configuration within ADMIN_ROLES."""
    role = get_user_role(user)
    return role is not None and role.name in ADMIN_ROLES


def is_super_admin(user):
    """Returns True only for explicit SuperAdmins or native superusers."""
    if getattr(user, 'is_superuser', False):
        return True
    role = get_user_role(user)
    return role is not None and role.name == 'SuperAdmin'


def is_hr_admin(user):
    """Returns True for SuperAdmin and HRAdmin profiles."""
    if getattr(user, 'is_superuser', False):
        return True
    role = get_user_role(user)
    return role is not None and role.name in {'SuperAdmin', 'HRAdmin'}


# ═══════════════════════════════════════════════════════════════════════════════
#  2. DECORATOR
# ═══════════════════════════════════════════════════════════════════════════════

def admin_required(view_func=None, *, roles=None, redirect_url=None):
    """
    Decorator that requires a user to be logged in and match specific allowed roles.
    Defaults to matching any role within ADMIN_ROLES.

    Usage:
        @admin_required
        def my_view(request): ...

        @admin_required(roles={'SuperAdmin'})
        def superadmin_only(request): ...
    """
    allowed = set(roles) if roles else ADMIN_ROLES
    redir = redirect_url or '/attendance/'

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
#  3. MIDDLEWARE
# ═══════════════════════════════════════════════════════════════════════════════

class AdminAccessMiddleware:
    """
    Blanket URL-level protection for secure admin endpoints.
    Blocks non-admin users from accessing secure management modules.

    Add to MIDDLEWARE after AuthenticationMiddleware in settings.py.
    """
    ADMIN_PREFIXES = (
        '/employees/',
        '/payroll/',
        '/attendance/admin/',
        '/attendance/otp/',
        '/calendar/',
        '/accounts/audit-log/',
    )

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        path = request.path

        # Allow entry if path matches whitelist configurations exactly
        if path in PUBLIC_PATHS:
            return self.get_response(request)
        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return self.get_response(request)

        # Enforce administrative gating over active prefix groupings
        if any(path.startswith(p) for p in self.ADMIN_PREFIXES):
            if not request.user.is_authenticated:
                return redirect(f'/login/?next={path}')
            if not has_admin_role(request.user):
                return render(request, 'hrms/403.html', status=403)

        return self.get_response(request)
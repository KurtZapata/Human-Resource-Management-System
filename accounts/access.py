"""
apps/accounts/access.py
═══════════════════════════════════════════════════════════════════════════════
Role-based access control for the HRMS admin pages.

Provides:
  1. admin_required       — decorator for individual views
  2. AdminAccessMiddleware — blanket middleware that protects all /employees/,
                             /payroll/, /attendance/admin/, /calendar/, etc.
  3. has_admin_role()      — helper used by templates to show/hide nav items

HOW ROLES WORK
──────────────
  SuperAdmin   → full access to everything
  HRAdmin      → access to everything except system/settings pages
  StaffAdmin   → read-only; cannot create/edit/delete

Non-admin users (employees with no role, or accounts with role=None) can only
access:
  /login/
  /attendance/          (time-in/out portal)
  /attendance/log/
  /attendance/employee-login/
  /attendance/employee-logout/

Place this file at: apps/accounts/access.py
═══════════════════════════════════════════════════════════════════════════════
"""

from functools import wraps
from django.shortcuts import redirect, render
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden

# ── Roles considered "admin" ──────────────────────────────────────────────────
ADMIN_ROLES = {'SuperAdmin', 'HRAdmin', 'StaffAdmin'}

# ── URLs that are always public (no login or role check) ──────────────────────
PUBLIC_PATHS = {
    '/login/',
    '/accounts/login/',
    '/attendance/',
    '/attendance/log/',
    '/attendance/employee-login/',
    '/attendance/employee-logout/',
}

# Paths that start with these prefixes are also public
PUBLIC_PREFIXES = (
    '/static/',
    '/media/',
    '/admin/',      # Django's own admin — protected separately
)


# ═══════════════════════════════════════════════════════════════════════════════
#  1. HELPER — check if a user has an admin role
# ═══════════════════════════════════════════════════════════════════════════════

def get_user_role(user):
    """
    Returns the Role object linked to this Django user via SystemUser,
    or None if no role is assigned.
    """
    if not user or not user.is_authenticated:
        return None
    # Django superusers always get SuperAdmin access
    if user.is_superuser:
        return _MockRole('SuperAdmin')
    try:
        from employees.models import SystemUser
        sys_user = SystemUser.objects.select_related('role').get(
            username=user.username, is_active=True
        )
        return sys_user.role
    except Exception:
        return None


def has_admin_role(user):
    """Returns True if the user has any admin role."""
    role = get_user_role(user)
    return role is not None and role.name in ADMIN_ROLES


def is_super_admin(user):
    """Returns True only for SuperAdmin."""
    if user.is_superuser:
        return True
    role = get_user_role(user)
    return role is not None and role.name == 'SuperAdmin'


def is_hr_admin(user):
    """Returns True for SuperAdmin and HRAdmin."""
    if user.is_superuser:
        return True
    role = get_user_role(user)
    return role is not None and role.name in {'SuperAdmin', 'HRAdmin'}


class _MockRole:
    """Lightweight stand-in so superusers don't need a SystemUser record."""
    def __init__(self, name):
        self.name = name


# ═══════════════════════════════════════════════════════════════════════════════
#  2. DECORATOR — use on individual admin views
# ═══════════════════════════════════════════════════════════════════════════════

def admin_required(view_func=None, *, redirect_url=None, roles=None):
    """
    Decorator that:
      - Requires the user to be logged in (Django login).
      - Requires the user to have a role in `roles` (default: any ADMIN_ROLES).
      - Redirects to redirect_url (default: /attendance/) if not authorised.

    Usage:
        @admin_required
        def my_view(request): ...

        @admin_required(roles={'SuperAdmin'})
        def superadmin_only(request): ...

        @admin_required(redirect_url='/attendance/')
        def hr_page(request): ...
    """
    allowed_roles = set(roles) if roles else ADMIN_ROLES
    redir = redirect_url or '/attendance/'

    def decorator(fn):
        @wraps(fn)
        @login_required(login_url='/login/')
        def wrapper(request, *args, **kwargs):
            role = get_user_role(request.user)
            if role and role.name in allowed_roles:
                return fn(request, *args, **kwargs)
            # Not an admin — show 403 page or redirect
            return _access_denied(request, redir)
        return wrapper

    if view_func is not None:
        # Called as @admin_required (no parentheses)
        return decorator(view_func)
    # Called as @admin_required(...) with arguments
    return decorator


def _access_denied(request, redirect_url):
    """
    Returns a rendered 403 page if the request wants HTML,
    otherwise redirects to the time-in page.
    """
    if request.headers.get('Accept', '').startswith('text/html'):
        return render(request, 'hrms/403.html', status=403)
    return redirect(redirect_url)


# ═══════════════════════════════════════════════════════════════════════════════
#  3. MIDDLEWARE — blanket protection for all admin URL prefixes
# ═══════════════════════════════════════════════════════════════════════════════

class AdminAccessMiddleware:
    """
    Middleware that blocks non-admin users from accessing any admin URL.

    Admin URLs are anything NOT in PUBLIC_PATHS / PUBLIC_PREFIXES.
    Add to MIDDLEWARE in settings.py AFTER AuthenticationMiddleware:

        MIDDLEWARE = [
            ...
            'django.contrib.auth.middleware.AuthenticationMiddleware',
            'apps.accounts.access.AdminAccessMiddleware',   # ← add here
            ...
        ]
    """

    # URL prefixes that require admin role
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

        # Always allow public paths and static files
        if path in PUBLIC_PATHS:
            return self.get_response(request)
        if any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return self.get_response(request)

        # Check if this is an admin-only URL
        if any(path.startswith(p) for p in self.ADMIN_PREFIXES):
            # Must be logged in
            if not request.user.is_authenticated:
                return redirect(f'/login/?next={path}')
            # Must have an admin role
            if not has_admin_role(request.user):
                return render(request, 'hrms/403.html', status=403)

        return self.get_response(request)

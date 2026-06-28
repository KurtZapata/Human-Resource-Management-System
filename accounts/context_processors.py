"""
apps/accounts/context_processors.py  (REPLACES the existing file)
Adds user_role_label (the friendly "Normal Admin" style name) alongside
the existing boolean flags. No Permission-system references — removed.
"""

from .access import get_user_role, role_display_name


def user_role(request):
    if not request.user.is_authenticated:
        return {
            'user_role':       None,
            'user_role_label': None,
            'is_super_admin':  False,
            'is_hr_admin':     False,
            'is_normal_admin': False,
        }

    role = get_user_role(request.user)
    role_name = role.name if role else ''

    return {
        'user_role':       role,
        'user_role_label': role_display_name(role) if role else None,
        'is_super_admin':  role_name == 'SuperAdmin',
        'is_hr_admin':     role_name in {'SuperAdmin', 'HRAdmin'},
        'is_normal_admin': role_name == 'StaffAdmin',
    }

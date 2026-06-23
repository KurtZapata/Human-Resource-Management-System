"""
accounts/context_processors.py
Makes user_role, is_super_admin, is_hr_admin, and is_staff_admin available globally in templates.

Add to settings.py TEMPLATES -> OPTIONS -> context_processors list:
    'accounts.context_processors.user_role',
"""

from .access import get_user_role


def user_role(request):
    """
    Injects role info into every template context so nav items
    can be shown/hidden based on the logged-in user's role.
    """
    if not request.user.is_authenticated:
        return {
            'user_role':      None,
            'is_super_admin': False,
            'is_hr_admin':    False,
            'is_staff_admin': False,
        }

    role      = get_user_role(request.user)
    role_name = role.name if role else ''
    
    return {
        'user_role':      role,
        'is_super_admin': role_name == 'SuperAdmin',
        'is_hr_admin':    role_name in {'SuperAdmin', 'HRAdmin'},
        'is_staff_admin': role_name in {'SuperAdmin', 'HRAdmin', 'StaffAdmin'},
    }
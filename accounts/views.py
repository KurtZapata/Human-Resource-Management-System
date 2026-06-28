"""
accounts/views.py
Handles login, logout, password reset, and audit log.
Maps to: Webpage #1 (Login)
"""

from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from accounts.access import admin_required, is_super_admin, is_hr_admin
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponse
from django.utils import timezone

from .models import AuditLog
from employees.models import Employee


def login_view(request):
    """
    GET:  Renders login.html with optional company branding context.
    POST: Authenticates username/password; redirects to 'next' or admin home.
    """
    if request.user.is_authenticated:
        return redirect('employees:list')

    # Pull branding from settings (stored in a simple Settings model or env)
    context = {
        'company_name':     _get_setting('company_name', 'Your Company'),
        'company_initials': _get_setting('company_initials', 'HR'),
        'login_bg_image':  _get_setting('login_bg_image', ''),
    }

    if request.method == 'POST':
        username   = request.POST.get('username', '').strip()
        password   = request.POST.get('password', '')
        remember_me = request.POST.get('remember_me')

        user = authenticate(request, username=username, password=password)

        if user is not None and user.is_active:
            login(request, user)

            # Session expiry: if remember_me unchecked → session expires on browser close
            if not remember_me:
                request.session.set_expiry(0)

            # Write audit log
            AuditLog.objects.create(
                user=user,
                action='LOGIN',
                table_name='auth_user',
                record_id=user.id,
                new_value={'username': username, 'ip': _get_ip(request)},
                timestamp=timezone.now(),
            )

            # --- Integrated Role-Aware Redirect Logic ---
            from accounts.access import has_admin_role
            next_url = request.POST.get('next') or request.GET.get('next') or ''

            if next_url:
                return redirect(next_url)
            elif has_admin_role(user):
                return redirect('employees:list')      # admin → employee list
            else:
                return redirect('attendance:timein')   # employee → time-in portal
        else:
            context['form'] = type('F', (), {'errors': True})()   # signal template to show error

    return render(request, 'hrms/login.html', context)


@require_POST
def logout_view(request):
    """
    POST only (CSRF-protected).
    Writes logout audit entry then clears session.
    """
    if request.user.is_authenticated:
        AuditLog.objects.create(
            user=request.user,
            action='LOGOUT',
            table_name='auth_user',
            record_id=request.user.id,
            new_value={'ip': _get_ip(request)},
            timestamp=timezone.now(),
        )
        logout(request)
    return redirect('accounts:login')


@admin_required(roles={'SuperAdmin'})
def audit_log_view(request):
    import csv as _csv
    from django.db.models import Count
    from django.contrib.auth.models import User as AuthUser

    logs = AuditLog.objects.select_related('user').order_by('-timestamp')

    user_filter   = request.GET.get('user_id')
    table_filter  = request.GET.get('table')
    action_filter = request.GET.get('action')
    from_date     = request.GET.get('from')
    to_date       = request.GET.get('to')

    if user_filter:   logs = logs.filter(user_id=user_filter)
    if table_filter:  logs = logs.filter(table_name=table_filter)
    if action_filter: logs = logs.filter(action=action_filter)
    if from_date:     logs = logs.filter(timestamp__date__gte=from_date)
    if to_date:       logs = logs.filter(timestamp__date__lte=to_date)

    # CSV export
    if request.GET.get('export') == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="audit_log.csv"'
        writer = _csv.writer(response)
        writer.writerow(['Timestamp','User','Action','Table','Record ID','Old Value','New Value'])
        for log in logs:
            writer.writerow([
                log.timestamp, log.user.username if log.user else 'System',
                log.action, log.table_name, log.record_id,
                str(log.old_value), str(log.new_value),
            ])
        return response

    # Action summary counts (top 5 actions)
    action_summary = AuditLog.objects.values('action')\
                             .annotate(count=Count('id'))\
                             .order_by('-count')[:5]

    from django.core.paginator import Paginator
    paginator = Paginator(logs, 50)
    page      = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'hrms/audit_log.html', {
        'logs':           page,
        'users':          AuthUser.objects.filter(auditlog__isnull=False).distinct(),
        'tables':         AuditLog.objects.values_list('table_name', flat=True).distinct(),
        'action_types':   AuditLog.objects.values_list('action', flat=True).distinct(),
        'action_summary': action_summary,
    })


# ── Password Reset (Step 1 → 3) ─────────────────────────────────────────────

def password_reset_request(request):
    """
    Step 1: Receive email, generate OTP, store in session, send email.
    Returns JSON for AJAX usage from the modal.
    """
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)

    import json, random, string
    body  = json.loads(request.body)
    email = body.get('email', '').strip().lower()

    from django.contrib.auth.models import User
    try:
        user = User.objects.get(email__iexact=email)
    except User.DoesNotExist:
        # Respond OK to avoid email enumeration
        return JsonResponse({'ok': True})

    otp_code = ''.join(random.choices(string.digits, k=6))
    request.session['reset_otp']    = otp_code
    request.session['reset_uid']    = user.id
    request.session['reset_expiry'] = str(timezone.now() + timezone.timedelta(minutes=5))

    # Send email (configure EMAIL_BACKEND in settings.py)
    from django.core.mail import send_mail
    send_mail(
        subject='Your HRMS Password Reset OTP',
        message=f'Your OTP is: {otp_code}. Valid for 5 minutes.',
        from_email=None,  # uses DEFAULT_FROM_EMAIL from settings
        recipient_list=[email],
        fail_silently=True,
    )
    return JsonResponse({'ok': True})


def password_reset_verify(request):
    """Step 2: Validate OTP from session."""
    import json
    body = json.loads(request.body)
    otp  = body.get('otp', '').strip()
    if otp == request.session.get('reset_otp'):
        return JsonResponse({'ok': True})
    return JsonResponse({'ok': False, 'error': 'Invalid OTP.'})


def password_reset_confirm(request):
    """Step 3: Set new password for user stored in session."""
    import json
    body     = json.loads(request.body)
    password = body.get('password', '')
    uid      = request.session.get('reset_uid')
    if not uid:
        return JsonResponse({'ok': False, 'error': 'Session expired.'})

    from django.contrib.auth.models import User
    try:
        user = User.objects.get(pk=uid)
        user.set_password(password)
        user.save()
        # Clear session keys
        for k in ('reset_otp', 'reset_uid', 'reset_expiry'):
            request.session.pop(k, None)
        return JsonResponse({'ok': True})
    except User.DoesNotExist:
        return JsonResponse({'ok': False, 'error': 'User not found.'})


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_ip(request):
    x_forwarded = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded:
        return x_forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def _get_setting(key, default=''):
    """
    Fetches a value from CompanySettings model.
    Adjust to match your actual settings storage approach.
    """
    try:
        from employees.models import CompanySettings
        return CompanySettings.objects.values_list(key, flat=True).first() or default
    except Exception:
        return default
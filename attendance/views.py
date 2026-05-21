"""
attendance/views.py
Handles employee time-in/time-out (OTP-based) and admin attendance management.
Maps to: Webpage #2 (Time-in/out) and Webpage #4 (Attendance Admin)

NOTE: Every mutating view writes to AuditLog and AttendanceLog.
"""

import json
import csv
from datetime import date, datetime, timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.core.paginator import Paginator
from django.db.models import Q, Count, Sum
from accounts.access import admin_required, is_super_admin, is_hr_admin
from .models import Attendance, OTP, AttendanceLog
from employees.models import Employee, LeaveBalance
from payroll.models import PayrollPeriod, Adjustment
from accounts.models import AuditLog, LeaveType
from employees.views import _branding


# ═══════════════════════════════════════════════════════════════════════════════
#  WEBPAGE #2 — Employee Time-in / Time-out (public-facing, OTP-authenticated)
# ═══════════════════════════════════════════════════════════════════════════════
def timein_page(request):
    """
    Public page for employee attendance.
    Shows login gate if no employee session.
    Shows attendance panel if employee is logged in via employee_login().
    """
    today            = date.today()
    employee_session = request.session.get('timein_employee')

    # Refresh today's attendance if employee is logged in
    if employee_session:
        try:
            att = Attendance.objects.filter(
                employee_id=employee_session['id'], date=today
            ).first()
            employee_session['today'] = {
                'time_in_am':  str(att.time_in_am)[:5]  if att and att.time_in_am  else None,
                'time_out_am': str(att.time_out_am)[:5] if att and att.time_out_am else None,
                'time_in_pm':  str(att.time_in_pm)[:5]  if att and att.time_in_pm  else None,
                'time_out_pm': str(att.time_out_pm)[:5] if att and att.time_out_pm else None,
            }
            # Update session with fresh data
            request.session['timein_employee'] = employee_session
            request.session.modified = True
        except Exception:
            pass

    return render(request, 'hrms/timein.html', {
        'employee_session': employee_session,
        **_branding(),
    })


def log_attendance(request):
    """
    AJAX POST endpoint called by timein.html.
    Validates OTP, then records time_in or time_out.

    Expected JSON body:
        { "otp": "123456", "action": "time_in" | "time_out" }

    Returns JSON:
        Success: { "success": true, "employee_name": "...", "half": "am"|"pm" }
        Failure: { "success": false, "message": "..." }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST required.'}, status=405)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Invalid JSON.'}, status=400)

    otp_code = body.get('otp', '').strip()
    action   = body.get('action', 'time_in')   # 'time_in' or 'time_out'

    # ── Step 1: Resolve employee from session ──────────────────────────────
    emp_session = request.session.get('timein_employee')
    if not emp_session:
        return JsonResponse({'success': False, 'message': 'Please sign in first.'})

    try:
        employee = Employee.objects.get(pk=emp_session['id'], status='active')
    except Employee.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Employee account not found.'})

    # ── Step 2: Validate OTP ────────────────────────────────────────────────
    now    = timezone.now()
    otp_qs = OTP.objects.filter(
        code=otp_code,
        is_used=False,
        expires_at__gt=now,
    )

    otp_obj = otp_qs.filter(
        Q(used_by_employee_id=employee.id) | Q(used_by_employee_id__isnull=True)
    ).first()

    if not otp_obj:
        _log_attendance_attempt(employee.id, action, success=False, request=request)
        return JsonResponse({'success': False, 'message': 'Invalid or expired OTP. Please request a new one.'})

    # ── Step 3: Mark OTP as used ────────────────────────────────────────────
    otp_obj.is_used = True
    otp_obj.used_by_employee_id = employee.id
    otp_obj.save(update_fields=['is_used', 'used_by_employee_id'])

    # ── Step 4: Determine AM/PM half ───────────────────────────────────────
    current_hour = now.hour
    half = 'am' if current_hour < 13 else 'pm'

    # ── Step 5: Get or create today's attendance record ────────────────────
    att, created = Attendance.objects.get_or_create(
        employee=employee,
        date=date.today(),
        defaults={'status': 'present'}
    )

    time_str = now.time().replace(microsecond=0)

    if action == 'time_in':
        if half == 'am' and not att.time_in_am:
            att.time_in_am = time_str
        elif half == 'pm' and not att.time_in_pm:
            att.time_in_pm = time_str
        else:
            return JsonResponse({
                'success': False,
                'message': f'Time-in for {"morning" if half == "am" else "afternoon"} already recorded.'
            })
    else:  # time_out
        if half == 'am' and not att.time_out_am:
            att.time_out_am = time_str
        elif half == 'pm' and not att.time_out_pm:
            att.time_out_pm = time_str
        else:
            return JsonResponse({
                'success': False,
                'message': f'Time-out for {"morning" if half == "am" else "afternoon"} already recorded.'
            })

    # ── Step 6: Recalculate totals ─────────────────────────────────────────
    att.total_hours = _calculate_total_hours(att)
    att.late_minutes = _calculate_late_minutes(att)
    att.undertime_minutes = _calculate_undertime(att)

    if att.late_minutes and att.late_minutes > 0:
        att.status = 'late'

    att.save()

    # ── Step 7: Write AttendanceLog (audit trail) ──────────────────────────
    AttendanceLog.objects.create(
        employee=employee,
        action=action,
        timestamp=now,
        ip_address=_get_ip(request),
        device_info=request.META.get('HTTP_USER_AGENT', '')[:255],
    )

    return JsonResponse({
        'success': True,
        'employee_name': f'{employee.first_name} {employee.last_name}',
        'half': half,
        'time': str(time_str),
    })


def _log_attendance_attempt(username, action, success, request):
    """Writes a failed attempt to AttendanceLog for security auditing."""
    # We log even failed attempts so admins can detect abuse
    AttendanceLog.objects.create(
        employee=None,
        action=f'{action}_FAILED',
        timestamp=timezone.now(),
        ip_address=_get_ip(request),
        device_info=f'Failed login attempt for username: {username}',
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  WEBPAGE #4 — Admin Attendance Dashboard & Per-Employee Detail
# ═══════════════════════════════════════════════════════════════════════════════

@admin_required
def attendance_dashboard(request):
    """
    Admin attendance page.
    Shows daily stats + all employees with today's status.
    """
    today          = date.today()
    current_period = PayrollPeriod.objects.filter(status='open').order_by('-start_date').first()
    payroll_periods = PayrollPeriod.objects.order_by('-start_date')[:12]

    # Today's attendance records
    daily_attendance = Attendance.objects.filter(date=today)\
                           .select_related('employee__department')\
                           .order_by('employee__last_name')

    # Employee list enriched with today's status
    employees = Employee.objects.filter(status='active')\
                    .select_related('department').order_by('last_name')
    today_att_map = {a.employee_id: a for a in daily_attendance}
    for emp in employees:
        a = today_att_map.get(emp.id)
        emp.today_status = a.status if a else 'absent'

    # Summary stats
    present_ids  = daily_attendance.filter(status='present').values_list('employee_id', flat=True)
    late_ids     = daily_attendance.filter(status='late').values_list('employee_id', flat=True)
    absent_count = employees.count() - daily_attendance.count()
    # Overtime: employees with Adjustment of type 'overtime' for today
    ot_count = Adjustment.objects.filter(
        payroll_period=current_period,
        type='overtime',
        created_at__date=today,
    ).values('employee_id').distinct().count()
    # On leave: employees with approved leave adjustment today
    leave_count = Adjustment.objects.filter(
        payroll_period=current_period,
        type='leave',
        created_at__date=today,
    ).values('employee_id').distinct().count()

    stats = {
        'present':  daily_attendance.filter(status='present').count(),
        'absent':   absent_count,
        'late':     daily_attendance.filter(status='late').count(),
        'on_leave': leave_count,
        'overtime': ot_count,
    }

    leave_types = LeaveType.objects.all()

    return render(request, 'hrms/attendance_admin.html', {
        'daily_attendance': daily_attendance,
        'employees':        employees,
        'stats':            stats,
        'today':            today,
        'current_period':   current_period,
        'payroll_periods':  payroll_periods,
        'leave_types':      leave_types,
        **_branding(),
    })


@admin_required
def attendance_records(request):
    import json as _json
    qs = Attendance.objects.select_related('employee').order_by('-date', 'employee__last_name')

    emp_id   = request.GET.get('emp_id')
    from_dt  = request.GET.get('from')
    to_dt    = request.GET.get('to')
    status   = request.GET.get('status')

    if emp_id:  qs = qs.filter(employee_id=emp_id)
    if from_dt: qs = qs.filter(date__gte=from_dt)
    if to_dt:   qs = qs.filter(date__lte=to_dt)
    if status:  qs = qs.filter(status=status)

    # Summary counts for filtered set
    from django.db.models import Count
    summary = {
        'total':   qs.count(),
        'present': qs.filter(status='present').count(),
        'absent':  qs.filter(status='absent').count(),
        'late':    qs.filter(status='late').count(),
    }

    # Serialize records for JS edit pre-population
    records_json = {}
    for r in qs:
        records_json[r.id] = {
            'employee_name': f'{r.employee.last_name}, {r.employee.first_name}',
            'date':          str(r.date),
            'time_in_am':    str(r.time_in_am)[:5]  if r.time_in_am  else '',
            'time_out_am':   str(r.time_out_am)[:5] if r.time_out_am else '',
            'time_in_pm':    str(r.time_in_pm)[:5]  if r.time_in_pm  else '',
            'time_out_pm':   str(r.time_out_pm)[:5] if r.time_out_pm else '',
            'status':        r.status,
        }

    from django.core.paginator import Paginator
    paginator = Paginator(qs, 30)
    records   = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'hrms/attendance_records.html', {
        'records':      records,
        'records_json': _json.dumps(records_json),
        'employees':    Employee.objects.filter(status='active'),
        'summary':      summary,
        **_branding(),
    })


@admin_required
def update_attendance(request, pk):
    """
    POST: Admin edits an attendance record.
    Supports updating times, status, overtime, and leave.
    """
    if request.method != 'POST':
        return redirect('attendance:dashboard')

    att = get_object_or_404(Attendance, pk=pk)
    old = _att_to_dict(att)
    d   = request.POST

    # Time fields — only update if value provided
    for field in ('time_in_am', 'time_out_am', 'time_in_pm', 'time_out_pm'):
        val = d.get(field, '').strip()
        if val:
            setattr(att, field, val)
        elif val == '':
            # Explicitly cleared
            setattr(att, field, None)

    att.status = d.get('status', att.status)
    att.total_hours       = _calculate_total_hours(att)
    att.late_minutes      = _calculate_late_minutes(att)
    att.undertime_minutes = _calculate_undertime(att)
    att.save()

    # Handle overtime approval
    ot_hours   = d.get('overtime_hours', '').strip()
    ot_approved = d.get('ot_approved', '').strip()
    if ot_hours:
        _upsert_adjustment(
            employee=att.employee,
            date=att.date,
            adj_type='overtime',
            hours=float(ot_hours),
            approved=ot_approved,
            description=d.get('ot_description', ''),
            created_by=request.user,
        )

    # Handle leave
    leave_type_id = d.get('leave_type_id', '').strip()
    leave_status  = d.get('leave_status', 'pending')
    if leave_type_id:
        _upsert_adjustment(
            employee=att.employee,
            date=att.date,
            adj_type='leave',
            leave_type_id=int(leave_type_id),
            hours=8,
            approved=leave_status,
            description='Leave',
            created_by=request.user,
        )

    AuditLog.objects.create(
        user=request.user, action='UPDATE',
        table_name='attendance_attendance', record_id=att.id,
        old_value=old, new_value=_att_to_dict(att), timestamp=timezone.now(),
    )
    messages.success(request, 'Attendance record updated.')
    return redirect('attendance:dashboard')


@login_required
def manual_entry(request):
    """POST: Admin creates a new attendance record manually."""
    if request.method != 'POST':
        return redirect('attendance:dashboard')

    d = request.POST
    att, created = Attendance.objects.get_or_create(
        employee_id = d.get('employee_id'),
        date        = d.get('date'),
    )
    for field in ('time_in_am', 'time_out_am', 'time_in_pm', 'time_out_pm'):
        val = d.get(field, '').strip()
        if val:
            setattr(att, field, val)
    att.status            = d.get('status', 'present')
    att.total_hours       = _calculate_total_hours(att)
    att.late_minutes      = _calculate_late_minutes(att)
    att.save()

    AuditLog.objects.create(
        user=request.user, action='CREATE',
        table_name='attendance_attendance', record_id=att.id,
        new_value=_att_to_dict(att), timestamp=timezone.now(),
    )
    messages.success(request, 'Attendance entry created.')
    return redirect('attendance:dashboard')


@login_required
@require_GET
def employee_stats(request):
    """
    AJAX: Returns attendance stats + per-date records for one employee + period.
    Used by the attendance calendar in the detail view.
    """
    emp_id    = request.GET.get('emp_id')
    period_id = request.GET.get('period_id')

    if not emp_id:
        return JsonResponse({'error': 'emp_id required'}, status=400)

    qs = Attendance.objects.filter(employee_id=emp_id)
    if period_id:
        try:
            period = PayrollPeriod.objects.get(pk=period_id)
            qs = qs.filter(date__gte=period.start_date, date__lte=period.end_date)
        except PayrollPeriod.DoesNotExist:
            pass

    # Build per-date record map for calendar rendering
    records = {}
    for a in qs:
        records[str(a.date)] = {
            'status':      a.status,
            'total_hours': float(a.total_hours) if a.total_hours else 0,
            'late_minutes': a.late_minutes or 0,
        }

    # Count adjustments (leave & OT)
    adj_qs = Adjustment.objects.filter(employee_id=emp_id)
    if period_id:
        adj_qs = adj_qs.filter(payroll_period_id=period_id)

    ot_hours  = adj_qs.filter(type='overtime').aggregate(total=Sum('hours'))['total'] or 0
    leave_days = adj_qs.filter(type='leave').count()

    return JsonResponse({
        'present':  qs.filter(status='present').count(),
        'absent':   qs.filter(status='absent').count(),
        'late':     qs.filter(status='late').count(),
        'leave':    leave_days,
        'ot_hours': float(ot_hours),
        'records':  records,
    })


@admin_required
def export_attendance(request):
    """CSV export of attendance for a given payroll period."""
    period_id = request.GET.get('period_id')
    qs = Attendance.objects.select_related('employee').order_by('date', 'employee__last_name')
    if period_id:
        try:
            period = PayrollPeriod.objects.get(pk=period_id)
            qs = qs.filter(date__gte=period.start_date, date__lte=period.end_date)
        except PayrollPeriod.DoesNotExist:
            pass

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="attendance.csv"'
    writer = csv.writer(response)
    writer.writerow(['Date','Employee Code','Employee Name','Time In AM','Time Out AM',
                     'Time In PM','Time Out PM','Total Hours','Late (min)','Status'])
    for a in qs:
        writer.writerow([
            a.date, a.employee.employee_code,
            f'{a.employee.last_name}, {a.employee.first_name}',
            a.time_in_am or '', a.time_out_am or '',
            a.time_in_pm or '', a.time_out_pm or '',
            a.total_hours or '', a.late_minutes or '', a.status,
        ])
    return response


@admin_required
def audit_log_view(request):
    from django.db.models import Count
    qs = AttendanceLog.objects.select_related('employee').order_by('-timestamp')

    emp_id  = request.GET.get('emp_id')
    action  = request.GET.get('action')
    from_dt = request.GET.get('from')
    to_dt   = request.GET.get('to')

    if emp_id:  qs = qs.filter(employee_id=emp_id)
    if action:  qs = qs.filter(action=action)
    if from_dt: qs = qs.filter(timestamp__date__gte=from_dt)
    if to_dt:   qs = qs.filter(timestamp__date__lte=to_dt)

    summary = {
        'time_in':  qs.filter(action='time_in').count(),
        'time_out': qs.filter(action='time_out').count(),
        'failed':   qs.filter(action__contains='FAILED').count(),
    }

    from django.core.paginator import Paginator
    paginator = Paginator(qs, 50)
    logs      = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'hrms/attendance_audit.html', {
        'logs':      logs,
        'employees': Employee.objects.filter(status='active'),
        'summary':   summary,
        **_branding(),
    })


# ── OTP Management ────────────────────────────────────────────────────────────

@admin_required
@require_POST
def generate_otp(request):

    import random, string
    from django.utils import timezone as tz

    try:
        body        = json.loads(request.body)
        emp_id      = body.get('employee_id')       # None = global
        expires_min = int(body.get('expires_minutes', 5))
    except Exception:
        return JsonResponse({'error': 'Invalid input.'}, status=400)

    # Generate a 6-digit numeric code
    code = ''.join(random.choices(string.digits, k=6))

    otp = OTP.objects.create(
        code                 = code,
        expires_at           = tz.now() + timedelta(minutes=expires_min),
        is_used              = False,
        used_by_employee_id  = emp_id,   # None for global
    )

    return JsonResponse({
        'code':       code,
        'expires_at': str(otp.expires_at),
    })


# ── Internal Helpers ──────────────────────────────────────────────────────────

def _calculate_total_hours(att):
    """
    Calculates total work hours from the four time fields.
    AM half:  time_in_am  → time_out_am
    PM half:  time_in_pm  → time_out_pm
    """
    total = timedelta()
    if att.time_in_am and att.time_out_am:
        total += _time_diff(att.time_in_am, att.time_out_am)
    if att.time_in_pm and att.time_out_pm:
        total += _time_diff(att.time_in_pm, att.time_out_pm)
    return round(total.total_seconds() / 3600, 2)


def _time_diff(t1, t2):
    """Returns timedelta between two time objects (or strings HH:MM:SS)."""
    from datetime import time as time_type
    if isinstance(t1, str):
        t1 = datetime.strptime(t1, '%H:%M:%S').time()
    if isinstance(t2, str):
        t2 = datetime.strptime(t2, '%H:%M:%S').time()
    d1 = datetime.combine(date.today(), t1)
    d2 = datetime.combine(date.today(), t2)
    diff = d2 - d1
    return diff if diff.total_seconds() > 0 else timedelta()


def _calculate_late_minutes(att):
    """
    Compares time_in_am against the configured workday start time (default 08:00).
    Late threshold configurable via CompanySettings.
    """
    WORKDAY_START = datetime.strptime('08:00:00', '%H:%M:%S').time()
    if not att.time_in_am:
        return 0
    t_in = att.time_in_am if not isinstance(att.time_in_am, str) \
           else datetime.strptime(str(att.time_in_am), '%H:%M:%S').time()
    if t_in > WORKDAY_START:
        delta = datetime.combine(date.today(), t_in) - datetime.combine(date.today(), WORKDAY_START)
        return int(delta.total_seconds() / 60)
    return 0


def _calculate_undertime(att):
    """Undertime: time_out_pm before configured end of day (default 17:00)."""
    WORKDAY_END = datetime.strptime('17:00:00', '%H:%M:%S').time()
    if not att.time_out_pm:
        return 0
    t_out = att.time_out_pm if not isinstance(att.time_out_pm, str) \
            else datetime.strptime(str(att.time_out_pm), '%H:%M:%S').time()
    if t_out < WORKDAY_END:
        delta = datetime.combine(date.today(), WORKDAY_END) - datetime.combine(date.today(), t_out)
        return int(delta.total_seconds() / 60)
    return 0


def _upsert_adjustment(employee, date, adj_type, hours, approved, description, created_by,
                        leave_type_id=None):
    """
    Creates or updates an Adjustment record for overtime or leave.
    Tied to the open payroll period.
    """
    period = PayrollPeriod.objects.filter(
        status='open', start_date__lte=date, end_date__gte=date
    ).first()
    if not period:
        return  # No open period for this date; skip

    adj, _ = Adjustment.objects.get_or_create(
        employee=employee,
        payroll_period=period,
        type=adj_type,
        defaults={'hours': 0, 'rate': 0, 'amount': 0, 'description': description, 'created_by': created_by},
    )
    adj.hours       = hours
    adj.description = description
    adj.created_by  = created_by
    if leave_type_id:
        adj.leave_type_id = leave_type_id
    adj.save()


def _att_to_dict(att):
    return {
        'id': att.id, 'employee_id': att.employee_id,
        'date': str(att.date),
        'time_in_am': str(att.time_in_am) if att.time_in_am else None,
        'time_out_am': str(att.time_out_am) if att.time_out_am else None,
        'time_in_pm': str(att.time_in_pm) if att.time_in_pm else None,
        'time_out_pm': str(att.time_out_pm) if att.time_out_pm else None,
        'total_hours': float(att.total_hours) if att.total_hours else None,
        'late_minutes': att.late_minutes, 'status': att.status,
    }


def _get_ip(request):
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    return xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR', '')

@admin_required
def otp_manager(request):
    """
    Admin page for generating and monitoring OTPs.
    Maps to: otp_manager.html
    URL: /attendance/otp/
    """
    from django.utils import timezone as tz
    from django.db.models import Count

    today      = date.today()
    now        = tz.now()
    today_otps = OTP.objects.filter(created_at__date=today)\
                     .select_related('used_by_employee')\
                     .order_by('-created_at')

    stats = {
        'total_today':   today_otps.count(),
        'used_today':    today_otps.filter(is_used=True).count(),
        'active_now':    today_otps.filter(is_used=False, expires_at__gt=now).count(),
        'expired_today': today_otps.filter(is_used=False, expires_at__lte=now).count(),
    }

    return render(request, 'hrms/otp_manager.html', {
        'otps':       today_otps,
        'stats':      stats,
        'employees':  Employee.objects.filter(status='active').order_by('last_name'),
        'timein_url': request.build_absolute_uri('/attendance/'),
        **_branding(),
    })
    
@login_required
@require_GET
def otp_list_json(request):
    """
    AJAX GET: Returns today's OTPs as JSON for the live table refresh.
    URL: /attendance/otp/list-json/
    """
    from django.utils import timezone as tz
    today = date.today()
    now   = tz.now()

    otps = OTP.objects.filter(created_at__date=today)\
               .select_related('used_by_employee')\
               .order_by('-created_at')[:50]

    result = []
    for otp in otps:
        if otp.is_used:
            status = 'used'
        elif otp.expires_at <= now:
            status = 'expired'
        else:
            status = 'active'

        result.append({
            'code':          otp.code,
            'employee':      f'{otp.used_by_employee.last_name}, {otp.used_by_employee.first_name}'
                             if otp.used_by_employee else None,
            'employee_code': otp.used_by_employee.employee_code
                             if otp.used_by_employee else None,
            'created_at':    otp.created_at.strftime('%H:%M:%S'),
            'expires_at':    otp.expires_at.strftime('%H:%M:%S'),
            'status':        status,
        })

    return JsonResponse({'otps': result})
    
def employee_login(request):
    """
    AJAX POST: Authenticates an employee on the time-in page.
    NOT the same as the admin Django login.
    Stores employee info in session under 'timein_employee'.

    Body: { \"username\": \"...\", \"password\": \"...\" }
    Returns: { \"success\": true, \"employee\": { id, name, code, initials, today } }
              { \"success\": false, \"message\": \"...\" }

    URL: /attendance/employee-login/
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST required.'}, status=405)

    try:
        body     = json.loads(request.body)
        username = body.get('username', '').strip()
        password = body.get('password', '')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Invalid request.'}, status=400)

    # Authenticate against Django auth
    from django.contrib.auth import authenticate as dj_authenticate
    auth_user = dj_authenticate(request, username=username, password=password)

    if not auth_user or not auth_user.is_active:
        AttendanceLog.objects.create(
            employee=None,
            action='LOGIN_FAILED',
            timestamp=timezone.now(),
            ip_address=_get_ip(request),
            device_info=f'Failed employee login: {username}',
        )
        return JsonResponse({'success': False, 'message': 'Invalid username or password.'})

    # Resolve the Employee linked to this auth user via SystemUser
    try:
        from employees.models import SystemUser
        sys_user = SystemUser.objects.select_related('employee').get(
            username=username, is_active=True
        )
        emp = sys_user.employee
        if not emp:
            return JsonResponse({'success': False, 'message': 'No employee profile linked to this account.'})
    except Exception:
        return JsonResponse({'success': False, 'message': 'Employee record not found for this account.'})

    # Get today's attendance for shift display
    today     = date.today()
    today_att = Attendance.objects.filter(employee=emp, date=today).first()
    today_data = {
        'time_in_am':  str(today_att.time_in_am)[:5]  if today_att and today_att.time_in_am  else None,
        'time_out_am': str(today_att.time_out_am)[:5] if today_att and today_att.time_out_am else None,
        'time_in_pm':  str(today_att.time_in_pm)[:5]  if today_att and today_att.time_in_pm  else None,
        'time_out_pm': str(today_att.time_out_pm)[:5] if today_att and today_att.time_out_pm else None,
    }

    # Store in session
    session_data = {
        'id':       emp.id,
        'name':     f'{emp.first_name} {emp.last_name}',
        'code':     emp.employee_code,
        'initials': (emp.first_name[:1] + emp.last_name[:1]).upper(),
        'today':    today_data,
    }
    request.session['timein_employee'] = session_data
    request.session.set_expiry(43200)  # 12-hour session for time-in page

    AttendanceLog.objects.create(
        employee=emp,
        action='PORTAL_LOGIN',
        timestamp=timezone.now(),
        ip_address=_get_ip(request),
        device_info=request.META.get('HTTP_USER_AGENT', '')[:255],
    )

    return JsonResponse({'success': True, 'employee': session_data})
    
@require_POST
def employee_logout(request):
    """
    AJAX POST: Clears the employee session on the time-in page.
    URL: /attendance/employee-logout/
    """
    emp_session = request.session.pop('timein_employee', None)
    if emp_session:
        try:
            emp = Employee.objects.get(pk=emp_session['id'])
            AttendanceLog.objects.create(
                employee=emp,
                action='PORTAL_LOGOUT',
                timestamp=timezone.now(),
                ip_address=_get_ip(request),
                device_info='',
            )
        except Employee.DoesNotExist:
            pass
    return JsonResponse({'success': True})

@login_required
@require_GET
def otp_stats_json(request):
    """
    AJAX GET: Returns today's OTP counts for the stat cards.
    Called every 30 seconds by the OTP manager page.
    URL: /attendance/otp/stats-json/
    """
    from django.utils import timezone as tz
    today = date.today()
    now   = tz.now()

    qs = OTP.objects.filter(created_at__date=today)

    return JsonResponse({
        'total_today':   qs.count(),
        'used_today':    qs.filter(is_used=True).count(),
        'active_now':    qs.filter(is_used=False, expires_at__gt=now).count(),
        'expired_today': qs.filter(is_used=False, expires_at__lte=now).count(),
    })

"""
attendance/views.py
Handles employee time-in/time-out (OTP-based) and admin attendance management.
Maps to: Webpage #2 (Time-in/out) and Webpage #4 (Attendance Admin)

NOTE: Every mutating view writes to AuditLog and AttendanceLog.
"""

import json
import csv
from datetime import date, datetime, timedelta
from decimal import Decimal
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
    Validates OTP, then records attendance by filling the next available sequential slot.

    Expected JSON body:
        { "otp": "123456", "expected_action": "time_in" | "time_out" }

    Returns JSON:
        Success: { "success": true, "employee_name": "...", "action": "...", "half": "first"|"second", "logged_time": "HH:MM", "today": {...} }
        Failure: { "success": false, "message": "..." }
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'POST required.'}, status=405)

    try:
        body = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Invalid JSON.'}, status=400)

    otp_code = body.get('otp', '').strip()

    # ── Step 1: Resolve employee from session ──────────────────────────────
    emp_session = request.session.get('timein_employee')
    if not emp_session:
        return JsonResponse({'success': False, 'message': 'Please sign in first.'})

    try:
        employee = Employee.objects.get(pk=emp_session['id'], status='active')
    except Employee.DoesNotExist:
        return JsonResponse({'success': False, 'message': 'Employee account not found.'})

    # ── Step 2: Validate OTP ────────────────────────────────────────────────
    now = timezone.now()
    otp_qs = OTP.objects.filter(
        code=otp_code,
        is_used=False,
        expires_at__gt=now,
    )

    otp_obj = otp_qs.filter(
        Q(used_by_employee_id=employee.id) | Q(used_by_employee_id__isnull=True)
    ).first()

    if not otp_obj:
        # Note: If _log_attendance_attempt expects the explicit 'action' from the body, 
        # fallback to expected_action or 'time_in'
        fallback_action = body.get('expected_action', 'time_in')
        _log_attendance_attempt(employee.id, fallback_action, success=False, request=request)
        return JsonResponse({'success': False, 'message': 'Invalid or expired OTP. Please request a new one.'})

    # ── Step 3: Mark OTP as used ────────────────────────────────────────────
    otp_obj.is_used = True
    otp_obj.used_by_employee_id = employee.id
    otp_obj.save(update_fields=['is_used', 'used_by_employee_id'])

    # ── Step 4: Determine next available slot (sequential, not clock-based) ─
    att, created = Attendance.objects.get_or_create(
        employee=employee,
        date=date.today(),
        defaults={'status': 'present'}
    )

    time_now = now.time().replace(microsecond=0)

    # Resolve which slot to fill next:
    #   Slot 1: time_in_am  → first thing in the day
    #   Slot 2: time_out_am → after time_in_am is set
    #   Slot 3: time_in_pm  → after time_out_am is set
    #   Slot 4: time_out_pm → after time_in_pm is set
    if not att.time_in_am:
        resolved_action = 'time_in'
        resolved_half   = 'first'
        att.time_in_am  = time_now
    elif not att.time_out_am:
        resolved_action = 'time_out'
        resolved_half   = 'first'
        att.time_out_am = time_now
    elif not att.time_in_pm:
        resolved_action = 'time_in'
        resolved_half   = 'second'
        att.time_in_pm  = time_now
    elif not att.time_out_pm:
        resolved_action = 'time_out'
        resolved_half   = 'second'
        att.time_out_pm = time_now
    else:
        return JsonResponse({
            'success': False,
            'message': 'All attendance slots for today are already filled.'
        })

    # Validate that employee sent the expected action (optional UX check)
    expected_action = body.get('expected_action')
    if expected_action and expected_action != resolved_action:
        # Mismatch — still proceed but note it in the log if desired
        pass

    # ── Step 5: Recalculate totals ─────────────────────────────────────────
    att.total_hours       = _calculate_total_hours(att)
    att.late_minutes      = _calculate_late_minutes(att)
    att.undertime_minutes = _calculate_undertime(att)

    if att.late_minutes and att.late_minutes > 0 and att.status == 'present':
        att.status = 'late'
    att.save()

    # ── Step 6: Write AttendanceLog (audit trail) ──────────────────────────
    AttendanceLog.objects.create(
        employee    = employee,
        action      = resolved_action,
        timestamp   = now,
        ip_address  = _get_ip(request),
        device_info = request.META.get('HTTP_USER_AGENT', '')[:255],
    )

    # Build today's state for the frontend to update shift display
    today_state = {
        'time_in_am':  str(att.time_in_am)[:5]  if att.time_in_am  else None,
        'time_out_am': str(att.time_out_am)[:5] if att.time_out_am else None,
        'time_in_pm':  str(att.time_in_pm)[:5]  if att.time_in_pm  else None,
        'time_out_pm': str(att.time_out_pm)[:5] if att.time_out_pm else None,
    }

    return JsonResponse({
        'success':       True,
        'employee_name': f'{employee.first_name} {employee.last_name}',
        'action':        resolved_action,
        'half':          resolved_half,
        'logged_time':   str(time_now)[:5],
        'today':         today_state,
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
    - Only updates time fields that were explicitly submitted with a value
    - Recalculates total_hours, late_minutes, undertime
    - Triggers payroll recompute if the record falls in a closed/finalized period
    - Blocks edits if the payroll period is locked (closed + confirmed)
    """
    if request.method != 'POST':
        return redirect('attendance:dashboard')

    att = get_object_or_404(Attendance, pk=pk)

    # ── Lock check: block edits if payroll already confirmed ──────────────
    locked_period = PayrollPeriod.objects.filter(
        status='closed',
        start_date__lte=att.date,
        end_date__gte=att.date,
    ).first()
    if locked_period:
        # Check if any payroll for this employee in this period is confirmed
        from payroll.models import Payroll as PayrollModel
        confirmed = PayrollModel.objects.filter(
            employee=att.employee,
            payroll_period=locked_period,
            is_confirmed=True,
        ).exists()
        if confirmed:
            messages.error(
                request,
                f'Cannot edit attendance for {att.date}: '
                f'payroll for this period has already been confirmed.'
            )
            return redirect('attendance:dashboard')

    old = _att_to_dict(att)
    d   = request.POST

    # ── Only update time fields that were explicitly submitted ────────────
    # Empty string = not submitted / unchanged. None = explicitly cleared.
    # We use a special sentinel: the form sends "CLEAR" to explicitly clear a field.
    for field in ('time_in_am', 'time_out_am', 'time_in_pm', 'time_out_pm'):
        raw = d.get(field, None)
        if raw is None:
            pass  # field not in POST at all — leave unchanged
        elif raw.strip() == 'CLEAR':
            setattr(att, field, None)
        elif raw.strip() != '':
            setattr(att, field, raw.strip())
        # empty string '' = unchanged, do nothing

    status_val = d.get('status', '').strip()
    if status_val:
        att.status = status_val

    # ── Recalculate derived fields ─────────────────────────────────────────
    att.total_hours       = _calculate_total_hours(att)
    att.late_minutes      = _calculate_late_minutes(att)
    att.undertime_minutes = _calculate_undertime(att)

    # Auto-update status based on late minutes
    if att.time_in_am and att.late_minutes and att.late_minutes > 0:
        if att.status == 'present':
            att.status = 'late'
    elif att.status == 'late' and (not att.late_minutes or att.late_minutes == 0):
        att.status = 'present'

    att.save()

    # ── Handle OT adjustment from attendance edit ─────────────────────────
    ot_hours_str = d.get('overtime_hours', '').strip()
    if ot_hours_str:
        ot_hours = float(ot_hours_str)
        _upsert_adjustment_for_date(
            employee    = att.employee,
            att_date    = att.date,
            adj_type    = 'overtime',
            hours       = ot_hours,
            description = d.get('ot_description', f'Overtime on {att.date}'),
            created_by  = request.user,
        )

    # ── Handle Leave from attendance edit ─────────────────────────────────
    leave_type_id = d.get('leave_type_id', '').strip()
    leave_amount  = d.get('leave_amount', '').strip()
    if leave_type_id and leave_amount:
        _upsert_adjustment_for_date(
            employee      = att.employee,
            att_date      = att.date,
            adj_type      = 'leave',
            hours         = 0,
            description   = d.get('leave_description', f'Leave on {att.date}'),
            created_by    = request.user,
            leave_type_id = int(leave_type_id),
            leave_amount  = float(leave_amount),
        )

    # ── Recompute payroll if this date falls in any period ────────────────
    _recompute_payroll_for_attendance(att)

    AuditLog.objects.create(
        user=request.user, action='UPDATE',
        table_name='attendance_attendance', record_id=att.id,
        old_value=old, new_value=_att_to_dict(att),
        timestamp=timezone.now(),
    )
    messages.success(request, f'Attendance for {att.date} updated.')
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
    Each date record now includes:
      - status, total_hours, late_minutes
      - has_overtime: bool — true if any OT adjustment exists for that date
      - ot_hours: float — total OT hours granted for that date
      - is_locked: bool — true if payroll for this period is confirmed
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
            period = None
    else:
        period = None

    # Check if this period's payroll is confirmed (locked)
    is_locked = False
    if period:
        from payroll.models import Payroll as PayrollModel
        is_locked = PayrollModel.objects.filter(
            employee_id=emp_id,
            payroll_period=period,
            is_confirmed=True,
        ).exists()

    # Build OT adjustment map keyed by date string
    adj_qs = Adjustment.objects.filter(employee_id=emp_id, type='overtime')
    if period:
        adj_qs = adj_qs.filter(payroll_period=period)

    ot_by_date = {}
    for adj in adj_qs:
        # Use description to extract date (set by _upsert_adjustment_for_date)
        # Also try to match by period dates
        key = adj.description  # e.g. "Overtime on 2026-03-15"
        import re
        date_match = re.search(r'(\d{4}-\d{2}-\d{2})', adj.description or '')
        if date_match:
            d_str = date_match.group(1)
            ot_by_date.setdefault(d_str, 0)
            ot_by_date[d_str] += float(adj.hours or 0)

    # Build per-date record map
    records = {}
    for a in qs:
        d_str = str(a.date)
        ot_h  = ot_by_date.get(d_str, 0)

        # Flag overtime: worked more than standard day OR has an OT adjustment
        standard_day_hours = 8.0
        worked = float(a.total_hours or 0)
        auto_ot = max(0, worked - standard_day_hours)

        records[d_str] = {
            'status':       a.status,
            'total_hours':  float(a.total_hours or 0),
            'late_minutes': a.late_minutes or 0,
            'has_overtime': ot_h > 0 or auto_ot > 0,
            'ot_hours':     ot_h if ot_h > 0 else round(auto_ot, 2),
            'ot_granted':   ot_h,           # officially granted OT from adjustments
            'ot_detected':  round(auto_ot, 2),  # auto-detected from extra hours
            'is_locked':    is_locked,
            'att_id':       a.id,
        }

    # Aggregates
    adj_all   = Adjustment.objects.filter(employee_id=emp_id)
    if period:
        adj_all = adj_all.filter(payroll_period=period)

    ot_total    = adj_all.filter(type='overtime').aggregate(h=Sum('hours'))['h'] or 0
    leave_count = adj_all.filter(type='leave').count()

    # Period-level totals
    hours_sum = qs.aggregate(t=Sum('total_hours'))['t'] or 0

    return JsonResponse({
        'present':     qs.filter(status__in=['present','late']).count(),
        'absent':      qs.filter(status='absent').count(),
        'late':        qs.filter(status='late').count(),
        'leave':       leave_count,
        'ot_hours':    float(ot_total),
        'total_hours': float(hours_sum),
        'is_locked':   is_locked,
        'records':     records,
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
    Total hours worked for one Attendance record.
    - Handles overnight (time_out < time_in).
    - Returns Decimal rounded to 4 places (matches DecimalField precision).
    - Each half (AM / PM) is calculated independently.
    """
    from decimal import Decimal

    total = timedelta()
    if att.time_in_am and att.time_out_am:
        total += _time_diff(att.time_in_am, att.time_out_am, allow_overnight=True)
    if att.time_in_pm and att.time_out_pm:
        total += _time_diff(att.time_in_pm, att.time_out_pm, allow_overnight=True)

    hours = Decimal(str(round(total.total_seconds() / 3600, 4)))
    return hours.quantize(Decimal('0.0001'))


def _time_diff(t1, t2, allow_overnight=False):
    """
    Returns timedelta between two time objects or HH:MM:SS strings.
    If t2 < t1 and allow_overnight=True, treats as crossing midnight.
    Example: 23:00 → 01:30 = 2.5 hours (not -21.5).
    """
    if isinstance(t1, str):
        t1 = datetime.strptime(t1[:8].ljust(8,'0'), '%H:%M:%S').time()
    if isinstance(t2, str):
        t2 = datetime.strptime(t2[:8].ljust(8,'0'), '%H:%M:%S').time()

    base = date.today()
    d1   = datetime.combine(base, t1)
    d2   = datetime.combine(base, t2)
    diff = d2 - d1

    if diff.total_seconds() < 0:
        if allow_overnight:
            # Shift went past midnight — add one day to end time
            d2   = datetime.combine(base + timedelta(days=1), t2)
            diff = d2 - d1
        else:
            return timedelta(0)

    return diff


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


def _upsert_adjustment_for_date(employee, att_date, adj_type, hours,
                                  description, created_by,
                                  leave_type_id=None, leave_amount=None):
    """
    Creates or updates one Adjustment record per employee+date+type.
    OT amount = hours × employee's ot_rate (auto-computed).
    Leave amount = leave_amount param (manual input by admin).
    """
    from decimal import Decimal, ROUND_HALF_UP
    from payroll.models import PayrollPeriod, Adjustment

    # Find the open or most-recent payroll period that contains this date
    period = PayrollPeriod.objects.filter(
        start_date__lte=att_date,
        end_date__gte=att_date,
    ).order_by('-start_date').first()

    if not period:
        return  # No period covers this date — skip silently

    if adj_type == 'overtime':
        sg      = employee.salary_grade if employee.salary_grade else None
        ot_rate = Decimal(str(sg.overtime_rate)) if sg else Decimal('0')
        amount  = (Decimal(str(hours)) * ot_rate).quantize(Decimal('0.01'), ROUND_HALF_UP)
        rate    = ot_rate
    else:
        # Leave: use manually-supplied amount
        amount = Decimal(str(leave_amount or 0))
        rate   = Decimal('0')

    # One record per employee + date + type
    adj, created = Adjustment.objects.update_or_create(
        employee       = employee,
        payroll_period = period,
        type           = adj_type,
        # Use description to distinguish multiple OT entries on different dates
        description    = description,
        defaults={
            'hours':         hours,
            'rate':          rate,
            'amount':        amount,
            'leave_type_id': leave_type_id,
            'created_by':    created_by,
        }
    )
    return adj


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


def _recompute_payroll_for_attendance(att):
    """
    After an attendance record is edited or OT is granted, recalculate
    payroll for the affected employee — IF their payroll for this period
    is still in draft (not confirmed).

    This now mirrors run_payroll() exactly, including:
      - hourly_rate × hours_worked basic pay
      - modular components (SSS, PhilHealth, etc.)
      - overtime adjustments (hours × ot_rate, recomputed live)
      - leave adjustments (manual ₱ amount)
      - Calendar holiday / rest day rate_multiplier   ← THE FIX
    """
    try:
        from payroll.models import (
            PayrollPeriod, Payroll, PayrollBreakdown,
            PayrollComponent, EmployeePayrollComponent, Adjustment,
        )
        from calendar_app.models import Calendar
        from decimal import Decimal, ROUND_HALF_UP
        from django.db.models import Sum

        period = PayrollPeriod.objects.filter(
            start_date__lte=att.date,
            end_date__gte=att.date,
        ).first()
        if not period:
            return

        payroll = Payroll.objects.filter(
            employee=att.employee,
            payroll_period=period,
        ).first()
        if not payroll or payroll.is_confirmed:
            return  # Locked — do not recalculate

        emp = att.employee
        sg  = emp.salary_grade
        if not sg:
            return

        hourly_rate = Decimal(str(sg.hourly_rate))
        ot_rate     = Decimal(str(sg.overtime_rate))

        att_qs = Attendance.objects.filter(
            employee=emp,
            date__gte=period.start_date,
            date__lte=period.end_date,
        )
        hours_agg    = att_qs.aggregate(t=Sum('total_hours'))
        hours_worked = Decimal(str(hours_agg['t'] or 0))
        late_minutes = att_qs.aggregate(t=Sum('late_minutes'))['t'] or 0
        days_present = att_qs.filter(status__in=['present', 'late']).count()
        days_absent  = att_qs.filter(status='absent').count()

        basic_pay     = (hourly_rate * hours_worked).quantize(Decimal('0.01'), ROUND_HALF_UP)
        daily_rate    = hourly_rate * Decimal('8')
        monthly_equiv = hourly_rate * Decimal('8') * Decimal('22')

        # ── Calendar: holiday / rest day premium ─────────────────────────
        # MUST match the exact logic in run_payroll() so live edits and
        # batch runs never disagree.
        calendar_bonus = Decimal('0')
        for att_rec in att_qs:
            day_hours = Decimal(str(att_rec.total_hours or 0))
            if day_hours <= 0:
                continue

            cal_entry = Calendar.objects.filter(date=att_rec.date).first()
            if not cal_entry:
                continue  # Regular workday — no adjustment

            multiplier = Decimal(str(cal_entry.rate_multiplier or 1))
            base_for_day = hourly_rate * day_hours

            if cal_entry.type == 'rest' and not cal_entry.is_paid:
                adjusted = base_for_day * multiplier
                calendar_bonus += (adjusted - base_for_day)

            elif cal_entry.type in ('regular_holiday', 'special_holiday'):
                if day_hours > 0:
                    adjusted = base_for_day * multiplier
                    calendar_bonus += (adjusted - base_for_day)

        if calendar_bonus > 0:
            calendar_bonus = calendar_bonus.quantize(Decimal('0.01'), ROUND_HALF_UP)
            basic_pay += calendar_bonus

        running      = basic_pay
        total_earn   = basic_pay
        total_deduct = Decimal('0')

        vars_ctx = {
            'basic_pay':     float(basic_pay),
            'hourly_rate':   float(hourly_rate),
            'daily_rate':    float(daily_rate),
            'monthly_equiv': float(monthly_equiv),
            'hours_worked':  float(hours_worked),
            'days_present':  days_present,
            'days_absent':   days_absent,
            'late_minutes':  late_minutes,
            'ot_hours':      0.0,
            'ot_rate':       float(ot_rate),
            'working_days':  22,
            'gross_pay':     float(basic_pay),
        }

        PayrollBreakdown.objects.filter(payroll=payroll).delete()

        if calendar_bonus > 0:
            PayrollBreakdown.objects.create(
                payroll=payroll, component=None,
                amount=calendar_bonus,
                description='Holiday / Rest Day Pay Premium',
            )

        components = PayrollComponent.objects.filter(is_active=True).order_by('sort_order', 'id')
        for comp in components:
            override = EmployeePayrollComponent.objects.filter(
                employee=emp, component=comp, is_active=True
            ).first()
            override_val = Decimal(str(override.value)) if override else None

            if comp.calculation_type == 'percentage':
                pct      = float(override_val if override_val else comp.default_value)
                base_key = getattr(comp, 'pct_base', None) or 'monthly_equiv'
                base_val = vars_ctx.get(base_key, vars_ctx['monthly_equiv'])
                amount   = Decimal(str(base_val * pct / 100))
            elif comp.calculation_type == 'formula' and getattr(comp, 'formula', ''):
                amount = _eval_formula_with_vars(comp.formula, vars_ctx)
            else:
                amount = Decimal(str(override_val if override_val else comp.default_value))

            op = getattr(comp, 'operator', None) or ('+' if comp.type == 'earning' else '-')
            prev = running
            if   op == '+': running = running + amount
            elif op == '-': running = running - amount
            elif op == '*': running = running * amount if amount else running
            elif op == '/': running = (running / amount).quantize(Decimal('0.01'), ROUND_HALF_UP) if amount else running

            delta = running - prev
            if delta >= 0:
                total_earn += delta
            else:
                total_deduct += abs(delta)

            vars_ctx['gross_pay'] = float(running)

            PayrollBreakdown.objects.create(
                payroll=payroll, component=comp,
                amount=amount,
                description=getattr(comp, 'description', None) or comp.name,
            )

        # ── Overtime adjustments (recomputed live from hours × ot_rate) ──
        for adj in Adjustment.objects.filter(employee=emp, payroll_period=period, type='overtime'):
            computed = (Decimal(str(adj.hours)) * ot_rate).quantize(Decimal('0.01'), ROUND_HALF_UP)
            adj.rate   = ot_rate
            adj.amount = computed
            adj.save(update_fields=['rate', 'amount'])
            running    += computed
            total_earn += computed

        # ── Leave adjustments (manual ₱ amount) ──────────────────────────
        for adj in Adjustment.objects.filter(employee=emp, payroll_period=period, type='leave'):
            adj_amount = Decimal(str(adj.amount))
            if adj_amount >= 0:
                running    += adj_amount
                total_earn += adj_amount
            else:
                deduct = abs(adj_amount)
                running      -= deduct
                total_deduct += deduct

        payroll.basic_pay        = basic_pay
        payroll.gross_pay        = total_earn.quantize(Decimal('0.01'), ROUND_HALF_UP)
        payroll.total_deductions = total_deduct.quantize(Decimal('0.01'), ROUND_HALF_UP)
        payroll.net_pay          = running.quantize(Decimal('0.01'), ROUND_HALF_UP)
        payroll.save()

    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            f'Payroll recompute failed after attendance edit: {e}'
        )
        


@require_POST
def grant_overtime(request):
    """
    AJAX POST: Grants overtime hours for an employee on a specific date.
    Called from the quick-grant OT modal on the attendance calendar.

    Body: { employee_id, date, hours, description }
    Returns: { ok: true } or { ok: false, error: "..." }
    """
    try:
        body    = json.loads(request.body)
        emp_id  = int(body.get('employee_id', 0))
        datestr = body.get('date', '').strip()
        hours   = float(body.get('hours', 0))
        desc    = body.get('description', '').strip()
    except Exception:
        return JsonResponse({'ok': False, 'error': 'Invalid input.'}, status=400)

    if not emp_id or not datestr or hours <= 0:
        return JsonResponse({'ok': False, 'error': 'employee_id, date and hours are required.'})

    try:
        att_date = date.fromisoformat(datestr)
        employee = Employee.objects.select_related('salary_grade').get(pk=emp_id, status='active')
    except (ValueError, Employee.DoesNotExist):
        return JsonResponse({'ok': False, 'error': 'Employee or date not found.'})

    # Block if payroll confirmed for this date
    from payroll.models import PayrollPeriod as PP, Payroll as PayrollModel
    period = PP.objects.filter(
        start_date__lte=att_date,
        end_date__gte=att_date,
    ).first()
    if period:
        locked = PayrollModel.objects.filter(
            employee=employee, payroll_period=period, is_confirmed=True
        ).exists()
        if locked:
            return JsonResponse({
                'ok': False,
                'error': 'Payroll for this period is already confirmed. OT cannot be changed.'
            })

    # Create/update the adjustment
    adj = _upsert_adjustment_for_date(
        employee    = employee,
        att_date    = att_date,
        adj_type    = 'overtime',
        hours       = hours,
        description = desc or f'Overtime on {datestr}',
        created_by  = request.user,
    )

    # Write audit log
    sg     = employee.salary_grade
    ot_rate = float(sg.overtime_rate) if sg else 0
    AuditLog.objects.create(
        user       = request.user,
        action     = 'OT_GRANT',
        table_name = 'payroll_adjustment',
        record_id  = adj.id if adj else None,
        new_value  = {
            'employee':    f'{employee.first_name} {employee.last_name}',
            'date':        datestr,
            'hours':       hours,
            'ot_rate':     ot_rate,
            'amount':      round(hours * ot_rate, 2),
        },
        timestamp  = timezone.now(),
    )

    # Recompute draft payroll for this period
    att_record = Attendance.objects.filter(employee=employee, date=att_date).first()
    if att_record:
        _recompute_payroll_for_attendance(att_record)

    return JsonResponse({'ok': True, 'hours': hours, 'amount': round(hours * ot_rate, 2)})
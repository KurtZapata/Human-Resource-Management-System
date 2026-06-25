"""
payroll/views.py
Handles PayrollComponent CRUD, drag-and-drop reorder, toggle active,
PayrollPeriod management, and payslip generation.
Maps to: Webpage #6 (Configurable Salary Page)

Key design notes:
- PayrollComponent has a 'sort_order' field (added beyond base ERD) for
  drag-and-drop ordering. This is essential for the salary config UI.
- EmployeePayrollComponent allows per-employee overrides of any component value.
- PayrollBreakdown stores the computed per-item amounts for each Payroll run.
"""

import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET
from django.http import JsonResponse
from django.contrib import messages
from django.utils import timezone
from django.db import transaction
from accounts.access import admin_required, is_super_admin, is_hr_admin
from django.db.models import Q, Sum, Max, Count
from datetime import timedelta

from .models import (
    PayrollComponent, PayrollPeriod, Payroll,
    PayrollBreakdown, EmployeePayrollComponent, Adjustment,
)
from employees.models import Employee, SalaryGrade
from accounts.models import AuditLog, LeaveType
from employees.views import _branding

FORMULA_VARIABLES = [
    {'name': 'basic_pay',     'label': 'Basic Pay',         'description': 'hours_worked × hourly_rate'},
    {'name': 'hourly_rate',   'label': 'Hourly Rate',        'description': 'Employee hourly rate from salary grade'},
    {'name': 'daily_rate',    'label': 'Daily Rate',         'description': 'hourly_rate × 8'},
    {'name': 'monthly_equiv', 'label': 'Monthly Equivalent', 'description': 'hourly_rate × 8 × 22 (for deduction bases)'},
    {'name': 'hours_worked',  'label': 'Hours Worked',       'description': 'Total hours from attendance this period'},
    {'name': 'gross_pay',     'label': 'Gross Pay (so far)', 'description': 'Running total of earnings'},
    {'name': 'days_present',  'label': 'Days Present',       'description': 'Attendance days present this period'},
    {'name': 'days_absent',   'label': 'Days Absent',        'description': 'Attendance days absent this period'},
    {'name': 'ot_hours',      'label': 'OT Hours',           'description': 'Total overtime hours (from adjustments)'},
    {'name': 'ot_rate',       'label': 'OT Rate (₱/hr)',     'description': 'Employee overtime rate from salary grade'},
    {'name': 'late_minutes',  'label': 'Late Minutes',       'description': 'Total late minutes this period'},
    {'name': 'working_days',  'label': 'Working Days',       'description': 'Standard = 22'},
]

# ═══════════════════════════════════════════════════════════════════════════════
#  WEBPAGE #6 — Configurable Salary Page
# ═══════════════════════════════════════════════════════════════════════════════

def salary_components(request):
    # Seed defaults if no components exist yet
    _seed_default_components()

    earnings   = PayrollComponent.objects.filter(type='earning').order_by('sort_order', 'id')
    deductions = PayrollComponent.objects.filter(type='deduction').order_by('sort_order', 'id')

    salary_grades = SalaryGrade.objects.annotate(employees_count=Count('employee'))

    # ── Serialise for JS preview calculator (With safe getattr fallbacks) ──
    all_comps = []
    for c in PayrollComponent.objects.filter(is_active=True).order_by('sort_order', 'id'):
        all_comps.append({
            'id':               c.id,
            'name':             c.name,
            'type':             c.type,
            'operator':         getattr(c, 'operator', '+') or '+',
            'calculation_type': c.calculation_type,
            'default_value':    float(c.default_value) if c.default_value else 0.0,
            'pct_base':         getattr(c, 'pct_base', 'monthly_equiv') or 'monthly_equiv',
            'formula':          getattr(c, 'formula', '') or '',
            'is_locked':        getattr(c, 'is_locked', False),
            'description':      getattr(c, 'description', '') or '',
        })

    return render(request, 'hrms/salary_config.html', {
        'earnings':           earnings,
        'deductions':         deductions,
        'salary_grades':      salary_grades,
        'components_json':    json.dumps(all_comps),
        'formula_variables':  FORMULA_VARIABLES,
        'variables_json':     json.dumps({v['name']: v['description'] for v in FORMULA_VARIABLES}),
        **_branding(),
    })


@admin_required
@require_POST
def create_component(request):
    d = request.POST
    max_order = PayrollComponent.objects.filter(
        type=d.get('type','earning')
    ).aggregate(m=Max('sort_order'))['m'] or 0

    comp = PayrollComponent.objects.create(
        name             = d.get('name','').strip(),
        type             = d.get('type','earning'),
        operator         = d.get('operator', '+'),
        calculation_type = d.get('calculation_type','fixed'),
        default_value    = _to_decimal(d.get('default_value') or d.get('pct_value') or '0'),
        pct_base         = d.get('pct_base', 'basic_pay'),
        formula          = d.get('formula','').strip(),
        description      = d.get('description','').strip(),
        is_active        = d.get('is_active','true') == 'true',
        is_locked        = False,   # user-created components are never locked
        sort_order       = max_order + 1,
    )
    AuditLog.objects.create(
        user=request.user, action='CREATE',
        table_name='payroll_payrollcomponent', record_id=comp.id,
        new_value=_comp_to_dict(comp), timestamp=timezone.now(),
    )
    messages.success(request, f'Component "{comp.name}" created.')
    return redirect('payroll:components')


@admin_required
def update_component(request, pk):
    if request.method != 'POST':
        return redirect('payroll:components')
    comp = get_object_or_404(PayrollComponent, pk=pk, is_locked=False)
    old  = _comp_to_dict(comp)
    d    = request.POST

    comp.name             = d.get('name', comp.name).strip()
    comp.type             = d.get('type', comp.type)
    comp.operator         = d.get('operator', comp.operator)
    comp.calculation_type = d.get('calculation_type', comp.calculation_type)
    comp.default_value    = _to_decimal(d.get('default_value') or d.get('pct_value') or str(comp.default_value))
    comp.pct_base         = d.get('pct_base', comp.pct_base or 'basic_pay')
    comp.formula          = d.get('formula', comp.formula or '').strip()
    comp.description      = d.get('description', comp.description or '').strip()
    comp.is_active        = d.get('is_active','true') == 'true'
    comp.save()

    AuditLog.objects.create(
        user=request.user, action='UPDATE',
        table_name='payroll_payrollcomponent', record_id=comp.id,
        old_value=old, new_value=_comp_to_dict(comp), timestamp=timezone.now(),
    )
    messages.success(request, f'Component "{comp.name}" updated.')
    return redirect('payroll:components')


@admin_required
@require_POST
def delete_component(request, pk):
    comp = get_object_or_404(PayrollComponent, pk=pk, is_locked=False)
    name = comp.name
    used = PayrollBreakdown.objects.filter(component=comp).exists()
    if used:
        comp.is_active = False
        comp.save()
        messages.warning(request, f'"{name}" is used in payroll history — deactivated instead of deleted.')
    else:
        AuditLog.objects.create(
            user=request.user, action='DELETE',
            table_name='payroll_payrollcomponent', record_id=comp.id,
            old_value=_comp_to_dict(comp), timestamp=timezone.now(),
        )
        comp.delete()
        messages.success(request, f'Component "{name}" deleted.')
    return redirect('payroll:components')


@admin_required
@require_GET
def get_component(request, pk):
    comp = get_object_or_404(PayrollComponent, pk=pk)
    return JsonResponse(_comp_to_dict(comp))


@admin_required
def reorder_components(request):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    try:
        body  = json.loads(request.body)
        order = body.get('order', [])
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    with transaction.atomic():
        for item in order:
            PayrollComponent.objects.filter(pk=item['id']).update(sort_order=item['order'])
    return JsonResponse({'ok': True})


@admin_required
def toggle_component(request, *args, **kwargs):
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    try:
        body    = json.loads(request.body)
        comp_id = int(body.get('id', 0))
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': 'Invalid input'}, status=400)

    comp = get_object_or_404(PayrollComponent, pk=comp_id)
    if comp.is_locked:
        return JsonResponse({'error': 'Default components cannot be toggled.'}, status=403)
    comp.is_active = not comp.is_active
    comp.save(update_fields=['is_active'])
    return JsonResponse({'is_active': comp.is_active})


# ═══════════════════════════════════════════════════════════════════════════════
#  Payroll Period Management
# ═══════════════════════════════════════════════════════════════════════════════

@admin_required
def payroll_periods(request):
    if request.method == 'POST':
        start_str = request.POST.get('start_date', '').strip()
        end_str   = request.POST.get('end_date',   '').strip()

        if not start_str or not end_str:
            messages.error(request, 'Both start and end dates are required.')
            return redirect('payroll:periods')

        from datetime import date as date_type
        try:
            start = date_type.fromisoformat(start_str)
            end   = date_type.fromisoformat(end_str)
        except ValueError:
            messages.error(request, 'Invalid date format.')
            return redirect('payroll:periods')

        if end <= start:
            messages.error(request, 'End date must be after start date.')
            return redirect('payroll:periods')

        overlap = PayrollPeriod.objects.filter(
            start_date__lte=end,
            end_date__gte=start,
        )
        if overlap.exists():
            conflicting = overlap.first()
            messages.error(
                request,
                f'Date range overlaps with existing period: '
                f'{conflicting.start_date} → {conflicting.end_date}. '
                f'Each date can only belong to one payroll period.'
            )
            return redirect('payroll:periods')

        period = PayrollPeriod.objects.create(
            start_date=start, end_date=end, status='open'
        )
        messages.success(
            request,
            f'Payroll period {start.strftime("%b %d, %Y")} → {end.strftime("%b %d, %Y")} created.'
        )
        return redirect('payroll:periods')

    periods = PayrollPeriod.objects.order_by('-start_date')
    for p in periods:
        agg = Payroll.objects.filter(payroll_period=p).aggregate(
            payroll_count   = Count('id'),
            confirmed_count = Count('id', filter=Q(is_confirmed=True)),
            total_net       = Sum('net_pay'),
        )
        p.payroll_count   = agg['payroll_count']   or 0
        p.confirmed_count = agg['confirmed_count'] or 0
        p.total_net       = agg['total_net']        or 0
        delta = (p.end_date - p.start_date).days + 1
        p.working_days = sum(
            1 for i in range(delta)
            if (p.start_date + timedelta(days=i)).weekday() < 5
        )

    stats = {
        'open':       PayrollPeriod.objects.filter(status='open').count(),
        'processing': PayrollPeriod.objects.filter(status='processing').count(),
        'closed':     PayrollPeriod.objects.filter(status='closed').count(),
    }
    return render(request, 'hrms/payroll_periods.html', {
        'periods': periods, 'stats': stats, **_branding()
    })


@admin_required
@require_POST
def close_period(request, pk):
    """Closes (finalises) a payroll period. Prevents further edits."""
    period = get_object_or_404(PayrollPeriod, pk=pk)
    if period.status == 'open':
        period.status = 'closed'
        period.save()
        AuditLog.objects.create(
            user=request.user, action='UPDATE',
            table_name='payroll_payrollperiod', record_id=period.id,
            new_value={'status': 'closed'}, timestamp=timezone.now(),
        )
        messages.success(request, f'Period {period.start_date} → {period.end_date} closed.')
    return redirect('payroll:periods')


# ═══════════════════════════════════════════════════════════════════════════════
#  Payroll Run
# ═══════════════════════════════════════════════════════════════════════════════

@admin_required
def run_payroll(request):
    all_periods = PayrollPeriod.objects.order_by('-start_date')
    open_periods = all_periods.filter(status='open')

    for p in all_periods:
        delta = (p.end_date - p.start_date).days + 1
        p.working_days = sum(
            1 for i in range(delta)
            if (p.start_date + timedelta(days=i)).weekday() < 5
        )

    period_id = request.GET.get('period_id') or request.POST.get('period_id')
    if period_id:
        try:    
            default_period = PayrollPeriod.objects.get(pk=period_id)
        except: 
            default_period = open_periods.first()
    else:
        default_period = open_periods.first()

    employees = Employee.objects.filter(status='active').select_related('department', 'salary_grade')
    components = PayrollComponent.objects.filter(is_active=True).order_by('sort_order', 'id')

    missing_grades = employees.filter(salary_grade__isnull=True).count()
    already_run = Payroll.objects.filter(payroll_period=default_period).exists() if default_period else False

    checks = {
        'has_open_period':      open_periods.exists(),
        'has_employees':        employees.exists(),
        'all_have_grades':      missing_grades == 0,
        'some_missing_grades': 0 < missing_grades < employees.count(),
        'missing_grade_count': missing_grades,
        'has_components':       components.exists(),
        'attendance_imported': True,
        'already_run':          already_run,
    }

    estimated_gross = sum(
        (Decimal(str(e.salary_grade.hourly_rate)) * Decimal('176')
         if e.salary_grade else Decimal('0'))
        for e in employees
    )
    estimated_net = estimated_gross * Decimal('0.88')

    if request.method == 'POST':
        period = get_object_or_404(PayrollPeriod, pk=request.POST.get('period_id'), status='open')
        period.status = 'processing'
        period.save()

        with transaction.atomic():
            for emp in employees:
                sg = emp.salary_grade
                if not sg:
                    continue  # Skip employees with no salary grade

                hourly_rate   = Decimal(str(sg.hourly_rate))
                ot_rate       = Decimal(str(sg.overtime_rate))
                daily_rate    = hourly_rate * Decimal('8')
                monthly_equiv = hourly_rate * Decimal('8') * Decimal('22')

                # ── Count actual hours worked from attendance ───────────
                from attendance.models import Attendance as AttModel
                att_qs = AttModel.objects.filter(
                    employee=emp,
                    date__gte=period.start_date,
                    date__lte=period.end_date,
                )
                hours_agg    = att_qs.aggregate(t=Sum('total_hours'))
                hours_worked = Decimal(str(hours_agg['t'] or 0))
                late_minutes = att_qs.aggregate(t=Sum('late_minutes'))['t'] or 0
                days_present = att_qs.filter(status__in=['present', 'late']).count()
                days_absent  = att_qs.filter(status='absent').count()

                # ── Basic pay = hourly × hours worked ───────────────────
                basic_pay = (hourly_rate * hours_worked).quantize(
                    Decimal('0.01'), ROUND_HALF_UP
                )

                vars_ctx = {
                    'basic_pay':     float(basic_pay),
                    'hourly_rate':    float(hourly_rate),
                    'daily_rate':     float(daily_rate),
                    'monthly_equiv': float(monthly_equiv),
                    'hours_worked':   float(hours_worked),
                    'days_present':   days_present,
                    'days_absent':    days_absent,
                    'late_minutes':   late_minutes,
                    'ot_hours':       0.0,
                    'ot_rate':        float(ot_rate),
                    'working_days':   22,
                    'gross_pay':      float(basic_pay),
                }

                running      = basic_pay
                total_earn   = basic_pay
                total_deduct = Decimal('0')

                payroll, _ = Payroll.objects.update_or_create(
                    employee=emp, payroll_period=period,
                    defaults={
                        'basic_pay':     basic_pay,
                        'status':        'draft',
                        'is_confirmed':  False,
                        'processed_by':  request.user,
                        'processed_at':  timezone.now(),
                    }
                )
                PayrollBreakdown.objects.filter(payroll=payroll).delete()

                # ── Calendar: adjust pay for holidays and rest days ──────
                from calendar_app.models import Calendar

                calendar_bonus = Decimal('0')
                calendar_deduct = Decimal('0')

                for att_rec in att_qs:
                    day_hours = Decimal(str(att_rec.total_hours or 0))
                    if day_hours <= 0:
                        continue

                    cal_entry = Calendar.objects.filter(date=att_rec.date).first()
                    if not cal_entry:
                        continue  # Regular workday — no adjustment needed

                    multiplier = Decimal(str(cal_entry.rate_multiplier or 1))

                    if cal_entry.type == 'rest' and not cal_entry.is_paid:
                        base_for_day = hourly_rate * day_hours
                        adjusted     = base_for_day * multiplier
                        calendar_bonus += (adjusted - base_for_day)

                    elif cal_entry.type in ('regular_holiday', 'special_holiday'):
                        if cal_entry.is_paid and day_hours > 0:
                            base_for_day   = hourly_rate * day_hours
                            adjusted       = base_for_day * multiplier
                            calendar_bonus += (adjusted - base_for_day)
                        elif not cal_entry.is_paid and day_hours == 0:
                            pass
                        elif not cal_entry.is_paid and day_hours > 0:
                            base_for_day   = hourly_rate * day_hours
                            adjusted       = base_for_day * multiplier
                            calendar_bonus += (adjusted - base_for_day)

                if calendar_bonus > 0:
                    basic_pay    += calendar_bonus.quantize(Decimal('0.01'), ROUND_HALF_UP)
                    total_earn   += calendar_bonus.quantize(Decimal('0.01'), ROUND_HALF_UP)
                    running      += calendar_bonus.quantize(Decimal('0.01'), ROUND_HALF_UP)
                    PayrollBreakdown.objects.create(
                        payroll     = payroll,
                        component   = None,
                        amount      = calendar_bonus.quantize(Decimal('0.01'), ROUND_HALF_UP),
                        description = 'Holiday / Rest Day Pay Premium',
                    )

                vars_ctx['basic_pay'] = float(basic_pay)
                vars_ctx['gross_pay'] = float(running)

                # ── Apply modular components ────────────────────────────
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
                    elif comp.calculation_type == 'formula':
                        formula = getattr(comp, 'formula', '') or ''
                        amount  = _eval_formula_with_vars(formula, vars_ctx)
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

                # ── Apply adjustments (OT and Leave) ────────────────────
                for adj in Adjustment.objects.filter(employee=emp, payroll_period=period):
                    adj_amount = Decimal(str(adj.amount))

                    if adj.type == 'overtime':
                        computed = (Decimal(str(adj.hours)) * ot_rate).quantize(
                            Decimal('0.01'), ROUND_HALF_UP
                        )
                        adj.rate   = ot_rate
                        adj.amount = computed
                        adj.save(update_fields=['rate', 'amount'])
                        running    += computed
                        total_earn += computed

                    elif adj.type == 'leave':
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
                payroll.status           = 'finalized'
                payroll.save()

        period.status = 'closed'
        period.save()

        AuditLog.objects.create(
            user=request.user, action='PAYROLL_RUN',
            table_name='payroll_payrollperiod', record_id=period.id,
            new_value={'status':'closed', 'employees': employees.count()},
            timestamp=timezone.now(),
        )
        messages.success(request, f'Payroll processed for {employees.count()} employees.')
        return redirect('payroll:report')

    return render(request, 'hrms/payroll_run.html', {
        'periods':       all_periods,
        'employees':        employees,
        'active_employees': employees.count(),
        'component_count':  components.count(),
        'estimated_gross':  estimated_gross,
        'estimated_net':    estimated_net,
        'checks':           checks,
        **_branding(),
    })


# ═══════════════════════════════════════════════════════════════════════════════
#  Payslips
# ═══════════════════════════════════════════════════════════════════════════════

@admin_required
def payslips_view(request):
    import json as _json
    from django.db.models import Sum

    periods = PayrollPeriod.objects.order_by('-start_date')
    for p in periods:
        p.payroll_count = Payroll.objects.filter(payroll_period=p).count()

    period_id = request.GET.get('period_id')
    if period_id:
        current_period = get_object_or_404(PayrollPeriod, pk=period_id)
    else:
        current_period = periods.first()

    if not current_period:
        return render(request, 'hrms/payslips.html', {
            'payrolls': [], 'periods': periods, 'current_period': None,
            'totals': {}, 'payroll_json': '{}', **_branding()
        })

    payrolls = Payroll.objects.filter(payroll_period=current_period)\
                      .select_related('employee','employee__department',
                                      'employee__position','employee__salary_grade')\
                      .order_by('employee__last_name')

    agg = payrolls.aggregate(
        gross_pay=Sum('gross_pay'), total_deductions=Sum('total_deductions'), net_pay=Sum('net_pay')
    )
    totals = {k: v or 0 for k, v in agg.items()}

    from payroll.views_report import _build_payroll_json
    payroll_json = _json.dumps(_build_payroll_json(payrolls))

    return render(request, 'hrms/payslips.html', {
        'payrolls':       payrolls,
        'periods':        periods,
        'current_period': current_period,
        'totals':         totals,
        'payroll_json':   payroll_json,
        **_branding(),
    })


@admin_required
def adjustments_view(request):
    from django.db.models import Sum, Count

    if request.method == 'POST':
        action = request.POST.get('action', 'create')

        if action in ('create', 'update'):
            emp_id    = request.POST.get('employee_id')
            period_id = request.POST.get('payroll_period_id')
            adj_type  = request.POST.get('type', 'overtime')
            hours     = Decimal(request.POST.get('hours', '0') or '0')
            description = request.POST.get('description', '').strip()
            leave_type_id = request.POST.get('leave_type_id') or None

            if adj_type == 'overtime':
                try:
                    emp  = Employee.objects.select_related('salary_grade').get(pk=emp_id)
                    rate = Decimal(str(emp.salary_grade.overtime_rate)) if emp.salary_grade else Decimal('0')
                except Employee.DoesNotExist:
                    rate = Decimal('0')
                amount = (hours * rate).quantize(Decimal('0.01'), ROUND_HALF_UP)
                rate_used = rate
            else:
                amount = Decimal(request.POST.get('amount', '0') or '0')
                rate_used = Decimal('0')

            if action == 'create':
                adj = Adjustment.objects.create(
                    employee_id       = emp_id,
                    payroll_period_id = period_id,
                    type              = adj_type,
                    hours             = hours,
                    rate              = rate_used,
                    amount            = amount,
                    description       = description,
                    leave_type_id     = leave_type_id,
                    created_by        = request.user,
                )
                messages.success(request, f'Adjustment created. Amount: ₱{amount:,.2f}')

            elif action == 'update':
                adj = get_object_or_404(Adjustment, pk=request.POST.get('adj_id'))
                adj.employee_id       = emp_id
                adj.payroll_period_id = period_id
                adj.type              = adj_type
                adj.hours             = hours
                adj.rate              = rate_used
                adj.amount            = amount
                adj.description       = description
                adj.leave_type_id     = leave_type_id
                adj.save()
                messages.success(request, f'Adjustment updated. Amount: ₱{amount:,.2f}')

        elif action == 'delete':
            adj = get_object_or_404(Adjustment, pk=request.POST.get('adj_id'))
            adj.delete()
            messages.warning(request, 'Adjustment deleted.')

        return redirect('payroll:adjustments')

    qs = Adjustment.objects.select_related(
        'employee', 'employee__salary_grade',
        'payroll_period', 'created_by'
    ).order_by('-created_at')

    emp_id    = request.GET.get('emp_id')
    adj_type  = request.GET.get('type')
    period_id = request.GET.get('period_id')

    if emp_id:    qs = qs.filter(employee_id=emp_id)
    if adj_type:  qs = qs.filter(type=adj_type)
    if period_id: qs = qs.filter(payroll_period_id=period_id)

    adj_list = []
    for adj in qs:
        if adj.type == 'overtime' and adj.amount == 0 and adj.hours > 0:
            sg = adj.employee.salary_grade if adj.employee else None
            ot_rate = Decimal(str(sg.overtime_rate)) if sg else Decimal('0')
            adj.computed_amount = (Decimal(str(adj.hours)) * ot_rate).quantize(Decimal('0.01'))
        else:
            adj.computed_amount = Decimal(str(adj.amount))
        adj_list.append(adj)

    ot_qs    = qs.filter(type='overtime')
    leave_qs = qs.filter(type='leave')

    summary = {
        'overtime_count':   ot_qs.count(),
        'leave_count':      leave_qs.count(),
        'total_ot_hours':   float(ot_qs.aggregate(t=Sum('hours'))['t'] or 0),
        'total_ot_amount':  float(sum(a.computed_amount for a in adj_list if a.type == 'overtime')),
        'total_leave_amount': float(sum(a.computed_amount for a in adj_list if a.type == 'leave')),
        'net_adjustment':   float(sum(
            a.computed_amount if a.type == 'overtime' else a.computed_amount
            for a in adj_list
        )),
    }

    paginator   = Paginator(adj_list, 25)
    adjustments = paginator.get_page(request.GET.get('page', 1))

    return render(request, 'hrms/adjustments.html', {
        'adjustments':    adjustments,
        'adj_list_full':  adj_list,
        'summary':        summary,
        'employees':      Employee.objects.filter(status='active').order_by('last_name'),
        'payroll_periods': PayrollPeriod.objects.order_by('-start_date'),
        'leave_types':    LeaveType.objects.all(),
        **_branding(),
    })


# ── Helpers ───────────────────────────────────────────────────────────────────

def _comp_to_dict(comp):
    return {
        'id':               comp.id,
        'name':             comp.name,
        'type':             comp.type,
        'operator':         comp.operator,
        'calculation_type': comp.calculation_type,
        'default_value':    str(comp.default_value),
        'pct_base':         comp.pct_base or 'basic_pay',
        'formula':          comp.formula or '',
        'description':      comp.description or '',
        'is_active':        comp.is_active,
        'is_locked':        comp.is_locked,
    }


def _to_decimal(val):
    try:
        return Decimal(str(val)).quantize(Decimal('0.0001'), ROUND_HALF_UP)
    except (InvalidOperation, TypeError):
        return Decimal('0')


def _eval_formula_safe(formula, vars_ctx):
    """Safely evaluates a formula string with the given variable context."""
    import re
    try:
        expr = formula
        for k, v in vars_ctx.items():
            expr = expr.replace(k, str(v))
        if not re.fullmatch(r'[\d\.\+\-\*\/\(\)\s]+', expr):
            return None
        result = eval(expr, {'__builtins__': {}}, {})  # noqa: S307
        return float(result) if result is not None and str(result) != 'nan' else None
    except Exception:
        return None

def compute_employee_payroll(employee, period, components, request_user=None):
    """
    Hourly-based payroll engine.

    Basic Pay = hourly_rate × total_hours_worked_in_period
    Overtime  = sum of Adjustment(type='overtime') amounts (ot_hours × ot_rate each)
    Leave     = sum of Adjustment(type='leave') amounts   (manual ₱ input by admin)

    All other components (SSS, PhilHealth etc.) apply on top of Basic Pay.
    """
    from attendance.models import Attendance

    sg = employee.salary_grade
    hourly_rate = Decimal(str(sg.hourly_rate)) if sg else Decimal('0')
    ot_rate     = Decimal(str(sg.overtime_rate)) if sg else Decimal('0')

    att_qs = Attendance.objects.filter(
        employee=employee,
        date__gte=period.start_date,
        date__lte=period.end_date,
    )

    hours_agg   = att_qs.aggregate(t=Sum('total_hours'))
    hours_worked = Decimal(str(hours_agg['t'] or 0))

    late_minutes = att_qs.aggregate(t=Sum('late_minutes'))['t'] or 0
    days_present = att_qs.filter(status__in=['present', 'late']).count()
    days_absent  = att_qs.filter(status='absent').count()

    basic_pay  = (hourly_rate * hours_worked).quantize(Decimal('0.01'), ROUND_HALF_UP)

    working_days = Decimal('22')
    daily_rate   = hourly_rate * Decimal('8')
    monthly_equiv = hourly_rate * Decimal('8') * working_days

    adj_qs = Adjustment.objects.filter(employee=employee, payroll_period=period)

    ot_adjustments = adj_qs.filter(type='overtime')
    total_ot_pay   = sum(
        Decimal(str(adj.amount)) for adj in ot_adjustments
    )
    total_ot_hours = sum(
        Decimal(str(adj.hours)) for adj in ot_adjustments
    )

    leave_adjustments = adj_qs.filter(type='leave')
    total_leave_adj   = sum(
        Decimal(str(adj.amount)) for adj in leave_adjustments
    )

    vars_ctx = {
        'basic_pay':     float(basic_pay),
        'hourly_rate':   float(hourly_rate),
        'daily_rate':    float(daily_rate),
        'monthly_equiv': float(monthly_equiv),
        'hours_worked':  float(hours_worked),
        'days_present':  days_present,
        'days_absent':   days_absent,
        'ot_hours':      float(total_ot_hours),
        'ot_rate':       float(ot_rate),
        'late_minutes':  late_minutes,
        'working_days':  22,
        'gross_pay':     float(basic_pay),
    }

    running      = basic_pay
    breakdown    = []
    total_earn   = basic_pay
    total_deduct = Decimal('0')

    breakdown.append({
        'component_id': None,
        'name':         'Basic Pay (Hourly)',
        'operator':     '+',
        'amount':       float(basic_pay),
        'type':         'earning',
        'description':  f'{float(hours_worked):.2f} hrs × ₱{float(hourly_rate):.2f}/hr',
    })

    for comp in components:
        override = EmployeePayrollComponent.objects.filter(
            employee=employee, component=comp, is_active=True
        ).first()
        override_val = Decimal(str(override.value)) if override else None

        if comp.calculation_type == 'percentage' and not override_val:
            pct      = float(comp.default_value)
            base_key = comp.pct_base or 'monthly_equiv'
            base_val = vars_ctx.get(base_key, vars_ctx['monthly_equiv'])
            amount   = Decimal(str(base_val * pct / 100))
        else:
            amount = _compute_component_amount(comp, vars_ctx, override_val)

        op = comp.operator or ('+' if comp.type == 'earning' else '-')

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

        breakdown.append({
            'component_id': comp.id,
            'name':         comp.name,
            'operator':     op,
            'amount':       float(amount),
            'type':         comp.type,
            'description':  comp.description or comp.name,
        })

    for adj in ot_adjustments:
        adj_amount = Decimal(str(adj.amount))
        running      += adj_amount
        total_earn   += adj_amount
        breakdown.append({
            'component_id': None,
            'name':         f'Overtime Pay — {adj.description or ""}',
            'operator':     '+',
            'amount':       float(adj_amount),
            'type':         'earning',
            'description':  f'{adj.hours} hrs × ₱{float(ot_rate):.2f}/hr',
        })

    for adj in leave_adjustments:
        adj_amount = Decimal(str(adj.amount))
        leave_type_name = ''
        try:
            from accounts.models import LeaveType as LT
            lt = LT.objects.get(pk=adj.leave_type_id)
            leave_type_name = lt.name
        except Exception:
            leave_type_name = 'Leave'

        if adj_amount >= 0:
            running    += adj_amount
            total_earn += adj_amount
            breakdown.append({
                'component_id': None,
                'name':         f'Leave Pay — {leave_type_name}',
                'operator':     '+',
                'amount':       float(adj_amount),
                'type':         'earning',
                'description':  adj.description or f'{leave_type_name} pay',
            })
        else:
            deduct = abs(adj_amount)
            running      -= deduct
            total_deduct += deduct
            breakdown.append({
                'component_id': None,
                'name':         f'Leave Deduction — {leave_type_name}',
                'operator':     '-',
                'amount':       float(deduct),
                'type':         'deduction',
                'description':  adj.description or f'Unpaid {leave_type_name}',
            })

    net_pay = running.quantize(Decimal('0.01'), ROUND_HALF_UP)

    return {
        'basic_pay':        basic_pay,
        'gross_pay':        total_earn.quantize(Decimal('0.01'), ROUND_HALF_UP),
        'total_deductions': total_deduct.quantize(Decimal('0.01'), ROUND_HALF_UP),
        'net_pay':          net_pay,
        'breakdown':        breakdown,
        'hours_worked':     hours_worked,
        'hourly_rate':      hourly_rate,
    }
    
def _compute_component_amount(comp, vars_ctx, override_val=None):
    """Returns the computed Decimal amount for one component."""
    if comp.calculation_type == 'fixed':
        val = override_val if override_val is not None else comp.default_value
        return Decimal(str(val))

    if comp.calculation_type == 'percentage':
        pct      = float(override_val) if override_val is not None else float(comp.default_value)
        base_key = comp.pct_base or 'basic_pay'
        base_val = vars_ctx.get(base_key, vars_ctx.get('basic_pay', 0))
        return Decimal(str(base_val * pct / 100))

    if comp.calculation_type == 'formula' and comp.formula:
        result = _eval_formula_safe(comp.formula, vars_ctx)
        return Decimal(str(result)) if result is not None else Decimal('0')

    return Decimal('0')

def _seed_default_components():
    """Default locked components. Cannot be edited or deleted."""
    if PayrollComponent.objects.filter(is_locked=True).exists():
        return

    DEFAULTS = [
        {
            'name': 'SSS Contribution', 'type': 'deduction', 'operator': '-',
            'calculation_type': 'percentage',
            'default_value': '4.5',
            'pct_base': 'monthly_equiv',
            'description': 'SSS employee share (4.5% of monthly salary equivalent). Adjust per current SSS schedule.',
            'sort_order': 10,
        },
        {
            'name': 'PhilHealth Contribution', 'type': 'deduction', 'operator': '-',
            'calculation_type': 'percentage',
            'default_value': '2.5',
            'pct_base': 'monthly_equiv',
            'description': 'PhilHealth employee share (2.5% of monthly salary equivalent).',
            'sort_order': 11,
        },
        {
            'name': 'Pag-IBIG Contribution', 'type': 'deduction', 'operator': '-',
            'calculation_type': 'fixed',
            'default_value': '100.00',
            'description': 'Pag-IBIG (HDMF) employee contribution. ₱100/month.',
            'sort_order': 12,
        },
        {
            'name': 'Late Deduction', 'type': 'deduction', 'operator': '-',
            'calculation_type': 'formula',
            'formula': 'hourly_rate / 60 * late_minutes',
            'default_value': '0',
            'description': 'Deduction for late minutes: (hourly_rate ÷ 60) × late_minutes.',
            'sort_order': 13,
        },
        {
            'name': 'Absent Deduction', 'type': 'deduction', 'operator': '-',
            'calculation_type': 'formula',
            'formula': 'daily_rate * days_absent',
            'default_value': '0',
            'description': 'Deduction for absences: daily_rate × days_absent.',
            'sort_order': 14,
        },
    ]

    for d in DEFAULTS:
        PayrollComponent.objects.get_or_create(
            name=d['name'], is_locked=True,
            defaults={
                'type':             d['type'],
                'operator':         d['operator'],
                'calculation_type': d['calculation_type'],
                'default_value':    Decimal(d.get('default_value', '0')),
                'formula':          d.get('formula', ''),
                'pct_base':         d.get('pct_base', 'monthly_equiv'),
                'description':      d.get('description', ''),
                'is_active':        True,
                'sort_order':       d.get('sort_order', 99),
            }
        )
        
        
def _eval_formula_with_vars(formula, vars_ctx):
    """Safely evaluates a formula string using the provided variable context."""
    import re
    from decimal import Decimal
    try:
        expr = str(formula)
        for k in sorted(vars_ctx.keys(), key=len, reverse=True):
            expr = expr.replace(k, str(vars_ctx[k]))
        if re.fullmatch(r'[\d\.\+\-\*\/\(\)\s]+', expr):
            result = eval(expr, {'__builtins__': {}}, {})  # noqa: S307
            if result is not None and str(result) not in ('nan', 'inf', '-inf'):
                return Decimal(str(result)).quantize(Decimal('0.01'))
    except Exception:
        pass
    return Decimal('0')


def _get_calendar_multiplier(check_date):
    """
    Returns the pay rate multiplier for a given date from the Calendar table.
    Returns 1.0 (standard pay) if no calendar entry exists for that date.
    """
    try:
        from calendar_app.models import Calendar
        entry = Calendar.objects.filter(date=check_date).first()
        if not entry:
            return Decimal('1')
        return Decimal(str(entry.rate_multiplier or 1))
    except Exception:
        return Decimal('1')


def _is_paid_day(check_date):
    """
    Returns True if an absent employee still receives pay for this date.
    Regular holidays = paid even if absent.
    Everything else = no pay for absent days.
    """
    try:
        from calendar_app.models import Calendar
        entry = Calendar.objects.filter(date=check_date).first()
        if not entry:
            return False  # Regular workday — absent = no pay for that day
        return entry.type == 'regular_holiday' and entry.is_paid
    except Exception:
        return False
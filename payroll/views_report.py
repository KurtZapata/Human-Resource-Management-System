"""
payroll/views_report.py
Handles Payroll Report (Webpage #7):
  - Paginated payroll register per period
  - Confirm / unconfirm individual or all payrolls
  - Payslip JSON data endpoint for JS preview/print
  - CSV export
  - Batch print data endpoint

NOTE: is_confirmed is added as a BooleanField to the Payroll model
beyond the base ERD. This is required for the confirmation workflow.
Add to your Payroll model:
    is_confirmed   = models.BooleanField(default=False)
    confirmed_by   = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                        blank=True, related_name='confirmed_payrolls')
    confirmed_at   = models.DateTimeField(null=True, blank=True)
"""

import json
import csv
from decimal import Decimal

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET
from django.http import JsonResponse, HttpResponse
from django.contrib import messages
from django.utils import timezone
from django.core.paginator import Paginator
from django.db.models import Sum, Count, Q

from .models import (
    Payroll, PayrollPeriod, PayrollBreakdown,
    PayrollComponent, Adjustment,
)
from employees.models import Employee, Department
from attendance.models import Attendance
from accounts.models import AuditLog
from employees.views import _branding


# ═══════════════════════════════════════════════════════════════════════════════
#  WEBPAGE #7 — Payroll Report / Payslip Printing
# ═══════════════════════════════════════════════════════════════════════════════

@login_required
def payroll_report(request):
    """
    Main payroll report page.
    Shows all employees in the selected payroll period with their payroll details,
    confirmation status, and print/export actions.
    """
    # ── Period selection ────────────────────────────────────────────────────
    payroll_periods = PayrollPeriod.objects.order_by('-start_date')

    period_id = request.GET.get('period_id')
    if period_id:
        current_period = get_object_or_404(PayrollPeriod, pk=period_id)
    else:
        # Default: most recent closed or open period
        current_period = payroll_periods.first()

    if not current_period:
        messages.warning(request, 'No payroll periods found. Please create one first.')
        return redirect('payroll:periods')

    # ── Payroll records for this period ─────────────────────────────────────
    payrolls_qs = Payroll.objects.filter(payroll_period=current_period)\
                      .select_related(
                          'employee', 'employee__department',
                          'employee__position', 'employee__salary_grade'
                      ).order_by('employee__last_name', 'employee__first_name')

    # Search / filter
    q      = request.GET.get('q', '').strip()
    status = request.GET.get('status', '').strip()
    if q:
        payrolls_qs = payrolls_qs.filter(
            Q(employee__first_name__icontains=q) |
            Q(employee__last_name__icontains=q)  |
            Q(employee__employee_code__icontains=q)
        )
    if status == 'confirmed':
        payrolls_qs = payrolls_qs.filter(is_confirmed=True)
    elif status == 'pending':
        payrolls_qs = payrolls_qs.filter(is_confirmed=False)

    # Pagination (25 per page for reports)
    paginator = Paginator(payrolls_qs, 25)
    payrolls  = paginator.get_page(request.GET.get('page', 1))

    # ── Stats ───────────────────────────────────────────────────────────────
    all_payrolls = Payroll.objects.filter(payroll_period=current_period)
    agg = all_payrolls.aggregate(
        total_gross = Sum('gross_pay'),
        total_net   = Sum('net_pay'),
        total_basic = Sum('basic_pay'),
        total_deductions = Sum('total_deductions'),
    )
    stats = {
        'total_employees': all_payrolls.count(),
        'confirmed':       all_payrolls.filter(is_confirmed=True).count(),
        'pending':         all_payrolls.filter(is_confirmed=False).count(),
        'total_gross':     agg['total_gross'] or 0,
        'total_net':       agg['total_net']   or 0,
    }

    # ── Totals for table footer (current page's filtered set) ───────────────
    filtered_agg = payrolls_qs.aggregate(
        basic_pay        = Sum('basic_pay'),
        gross_pay        = Sum('gross_pay'),
        total_deductions = Sum('total_deductions'),
        net_pay          = Sum('net_pay'),
    )
    totals = {
        'basic_pay':        filtered_agg['basic_pay']        or Decimal('0'),
        'gross_pay':        filtered_agg['gross_pay']        or Decimal('0'),
        'total_deductions': filtered_agg['total_deductions'] or Decimal('0'),
        'net_pay':          filtered_agg['net_pay']          or Decimal('0'),
    }

    # ── Build payroll JSON for JS (payslip preview + print) ─────────────────
    payroll_json = _build_payroll_json(payrolls_qs)

    context = {
        'payrolls':        payrolls,
        'payroll_periods': payroll_periods,
        'current_period':  current_period,
        'stats':           stats,
        'totals':          totals,
        'departments':     Department.objects.all(),
        'payroll_json':    json.dumps(payroll_json),
        **_branding(),
    }
    return render(request, 'hrms/payroll_report.html', context)


# ── Confirm / Unconfirm ───────────────────────────────────────────────────────

@login_required
@require_POST
def confirm_payroll(request):
    """
    AJAX POST: Confirm or unconfirm one or more payroll records.

    Body: {
        "payroll_ids": [1, 2, 3],
        "confirmed":   true | false,
        "period_id":   int
    }

    NOTE: is_confirmed, confirmed_by, confirmed_at are fields added to
    the Payroll model beyond the base ERD.
    """
    try:
        body        = json.loads(request.body)
        payroll_ids = body.get('payroll_ids', [])
        confirmed   = bool(body.get('confirmed', True))
    except json.JSONDecodeError:
        return JsonResponse({'ok': False, 'error': 'Invalid JSON.'}, status=400)

    if not payroll_ids:
        return JsonResponse({'ok': False, 'error': 'No payroll IDs provided.'})

    updated = Payroll.objects.filter(pk__in=payroll_ids)
    count   = updated.count()
    if not count:
        return JsonResponse({'ok': False, 'error': 'No matching payroll records found.'})

    now = timezone.now()
    if confirmed:
        updated.update(
            is_confirmed   = True,
            confirmed_by   = request.user,
            confirmed_at   = now,
            status         = 'finalized',
        )
        action_label = 'CONFIRM'
    else:
        updated.update(
            is_confirmed   = False,
            confirmed_by   = None,
            confirmed_at   = None,
            status         = 'draft',
        )
        action_label = 'UNCONFIRM'

    # Write audit logs
    for p in updated:
        AuditLog.objects.create(
            user=request.user,
            action=action_label,
            table_name='payroll_payroll',
            record_id=p.id,
            new_value={
                'is_confirmed': confirmed,
                'employee_id':  p.employee_id,
                'net_pay':      str(p.net_pay),
            },
            timestamp=now,
        )

    return JsonResponse({
        'ok':    True,
        'count': count,
        'confirmed': confirmed,
    })


# ── Single payslip JSON ───────────────────────────────────────────────────────

@login_required
@require_GET
def payslip_json(request, pk):
    """
    AJAX GET: Returns full payslip data for one payroll record.
    Used by the preview modal JS to render the payslip template.
    """
    payroll = get_object_or_404(
        Payroll.objects.select_related(
            'employee', 'employee__department',
            'employee__position', 'employee__salary_grade',
            'payroll_period',
        ),
        pk=pk
    )
    data = _payroll_to_dict(payroll)
    return JsonResponse(data)


# ── CSV Export ────────────────────────────────────────────────────────────────

@login_required
@require_GET
def export_payroll(request):
    """
    CSV export of all payroll records for a period.
    Query param: ?period_id=<id>
    """
    period_id = request.GET.get('period_id')
    if period_id:
        period = get_object_or_404(PayrollPeriod, pk=period_id)
    else:
        period = PayrollPeriod.objects.order_by('-start_date').first()

    filename = f'payroll_{period.start_date}_{period.end_date}.csv'
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        'Employee Code', 'Last Name', 'First Name',
        'Department', 'Position', 'Salary Grade',
        'Basic Pay', 'Gross Pay', 'Total Deductions', 'Net Pay',
        'Status', 'Confirmed',
    ])

    payrolls = Payroll.objects.filter(payroll_period=period)\
                   .select_related('employee', 'employee__department',
                                   'employee__position', 'employee__salary_grade')\
                   .order_by('employee__last_name')

    for p in payrolls:
        writer.writerow([
            p.employee.employee_code,
            p.employee.last_name,
            p.employee.first_name,
            p.employee.department.name if p.employee.department else '',
            p.employee.position.name   if p.employee.position   else '',
            p.employee.salary_grade.name if p.employee.salary_grade else '',
            str(p.basic_pay),
            str(p.gross_pay),
            str(p.total_deductions),
            str(p.net_pay),
            p.status,
            'Yes' if p.is_confirmed else 'No',
        ])

    return response


# ── Internal helpers ──────────────────────────────────────────────────────────

def _build_payroll_json(payrolls_qs):
    """
    Serialises the full payroll queryset to a dict keyed by payroll ID.
    This is passed to the template as JSON and used by the JS payslip renderer.
    """
    result = {}
    for p in payrolls_qs:
        result[p.id] = _payroll_to_dict(p)
    return result


def _payroll_to_dict(p):
    """
    Converts a Payroll model instance (with select_related) to a
    JSON-serialisable dict including the PayrollBreakdown line items
    and attendance summary.
    """
    emp = p.employee

    # Breakdown items (earnings and deductions)
    breakdown = []
    for b in PayrollBreakdown.objects.filter(payroll=p).select_related('component'):
        if b.component:
            breakdown.append({
                'name':   b.component.name,
                'type':   b.component.type,
                'amount': float(b.amount),
                'description': b.description or b.component.name,
            })

    # Attendance summary for the payroll period
    att_qs = Attendance.objects.filter(
        employee=emp,
        date__gte=p.payroll_period.start_date,
        date__lte=p.payroll_period.end_date,
    )
    att_summary = {
        'days_present': att_qs.filter(status='present').count(),
        'days_absent':  att_qs.filter(status='absent').count(),
        'late_days':    att_qs.filter(status='late').count(),
        'ot_hours':     float(
            Adjustment.objects.filter(
                employee=emp,
                payroll_period=p.payroll_period,
                type='overtime',
            ).aggregate(h=Sum('hours'))['h'] or 0
        ),
    }

    return {
        'id':               p.id,
        'employee_id':      emp.id,
        'employee_code':    emp.employee_code,
        'first_name':       emp.first_name,
        'last_name':        emp.last_name,
        'department':       emp.department.name if emp.department else '',
        'position':         emp.position.name   if emp.position   else '',
        'salary_grade':     emp.salary_grade.name if emp.salary_grade else '',
        'employment_type':  emp.employment_type,
        'basic_pay':        float(p.basic_pay),
        'gross_pay':        float(p.gross_pay),
        'total_deductions': float(p.total_deductions),
        'net_pay':          float(p.net_pay),
        'status':           p.status,
        'is_confirmed':     p.is_confirmed,
        'confirmed_at':     p.confirmed_at.strftime('%b %d, %Y %H:%M') if p.confirmed_at else None,
        'breakdown':        breakdown,
        'attendance':       att_summary,
        'period_label':     f"{p.payroll_period.start_date.strftime('%b %d')} – {p.payroll_period.end_date.strftime('%b %d, %Y')}",
    }

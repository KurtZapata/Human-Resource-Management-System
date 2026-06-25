"""
apps/payroll/engine.py  (NEW FILE)
═══════════════════════════════════════════════════════════════════════════════
THE single source of truth for payroll calculation.

WHY THIS FILE EXISTS:
Previously, the exact same payroll math was duplicated in two places:
  - run_payroll()                       (the batch payroll run)
  - _recompute_payroll_for_attendance() (fired on attendance edit / OT grant)

Duplicated formulas drift apart over time — exactly what happened when the
Calendar holiday-premium logic was added to one and not the other. This
module fixes that by being the ONLY place the formula is written. Both
call sites now just call compute_employee_payroll() and persist the result.

THE MASTER FORMULA (applied in this exact order):
  1. hours_worked     = SUM(Attendance.total_hours) for the period
  2. basic_pay        = hourly_rate × hours_worked
  3. basic_pay       += calendar_premium  (holiday / rest day extra pay)
  4. running_total    = basic_pay
     for each active PayrollComponent (in sort_order):
         amount = fixed | percentage-of-variable | formula
         running_total = running_total <operator> amount      (+, -, ×, ÷)
  5. running_total   += Σ(overtime_hours × overtime_rate)       [from Adjustment]
  6. running_total   += Σ(leave adjustment amounts)              [manual, ± by HR]
  net_pay = running_total

This function is PURE with respect to the database: it only reads
(Attendance, Adjustment, PayrollComponent, EmployeePayrollComponent,
Calendar) and never writes. The caller persists the result.
═══════════════════════════════════════════════════════════════════════════════
"""

import re
from decimal import Decimal, ROUND_HALF_UP

from django.db.models import Sum

STANDARD_WORKING_DAYS  = Decimal('22')
STANDARD_HOURS_PER_DAY = Decimal('8')


# ═══════════════════════════════════════════════════════════════════════════════
#  Calendar — holiday / rest day rate lookup
# ═══════════════════════════════════════════════════════════════════════════════

def get_calendar_multiplier(check_date):
    """
    Returns (multiplier: Decimal, entry: Calendar|None) for a given date.
    multiplier defaults to 1 (no change) if no Calendar entry exists.
    """
    try:
        from calendar_app.models import Calendar
        entry = Calendar.objects.filter(date=check_date).first()
        if not entry:
            return Decimal('1'), None
        return Decimal(str(entry.rate_multiplier or 1)), entry
    except Exception:
        return Decimal('1'), None


def compute_calendar_premium(employee, period, hourly_rate):
    """
    Walks every Attendance record for the employee in this period and
    computes the EXTRA pay (premium) owed for holidays / rest days
    actually worked. A "premium day" is:
      - a regular_holiday or special_holiday worked, OR
      - an unpaid rest day worked
    Regular workdays (no Calendar entry, multiplier == 1) contribute 0.

    Returns (total_premium: Decimal, breakdown: list[dict]).
    """
    from attendance.models import Attendance

    total_premium = Decimal('0')
    breakdown = []

    att_qs = Attendance.objects.filter(
        employee=employee,
        date__gte=period.start_date,
        date__lte=period.end_date,
    )

    for att in att_qs:
        day_hours = Decimal(str(att.total_hours or 0))
        if day_hours <= 0:
            continue

        multiplier, cal_entry = get_calendar_multiplier(att.date)
        if not cal_entry or multiplier == Decimal('1'):
            continue  # Regular workday — no premium

        is_premium_day = (
            cal_entry.type in ('regular_holiday', 'special_holiday')
            or (cal_entry.type == 'rest' and not cal_entry.is_paid)
        )
        if not is_premium_day:
            continue

        base_for_day = hourly_rate * day_hours
        adjusted     = base_for_day * multiplier
        premium      = (adjusted - base_for_day).quantize(Decimal('0.01'), ROUND_HALF_UP)

        if premium != 0:
            total_premium += premium
            breakdown.append({
                'date':       str(att.date),
                'type':       cal_entry.type,
                'hours':      float(day_hours),
                'multiplier': float(multiplier),
                'premium':    float(premium),
            })

    return total_premium.quantize(Decimal('0.01'), ROUND_HALF_UP), breakdown


# ═══════════════════════════════════════════════════════════════════════════════
#  Formula evaluation (safe — no eval() on untrusted raw strings)
# ═══════════════════════════════════════════════════════════════════════════════

def eval_formula(formula, vars_ctx):
    """
    Safely evaluates an arithmetic formula string after substituting
    variable names with numeric values from vars_ctx.

    SECURITY: after substitution, the expression must consist ONLY of
    digits, decimal points, + - * / ( ) and whitespace. Anything else
    (letters, quotes, underscores, etc.) causes an immediate Decimal('0')
    return WITHOUT ever reaching eval(). This blocks code-injection
    attempts like __import__(...) even though admins write the formulas.
    """
    if not formula:
        return Decimal('0')
    try:
        expr = str(formula)
        # Substitute longest variable names first so 'hourly_rate' doesn't
        # get partially clobbered by a shorter name like 'rate'.
        for key in sorted(vars_ctx.keys(), key=len, reverse=True):
            expr = expr.replace(key, str(vars_ctx[key]))

        if not re.fullmatch(r'[\d\.\+\-\*\/\(\)\s]+', expr):
            return Decimal('0')

        result = eval(expr, {'__builtins__': {}}, {})  # noqa: S307 — validated above
        if result is None or str(result) in ('nan', 'inf', '-inf'):
            return Decimal('0')
        return Decimal(str(result)).quantize(Decimal('0.01'))
    except Exception:
        return Decimal('0')


def compute_component_amount(component, vars_ctx, override_value=None):
    """
    Computes the ₱ amount for one PayrollComponent given the current
    variable context. Honors a per-employee override value if provided.
    """
    if component.calculation_type == 'fixed':
        val = override_value if override_value is not None else component.default_value
        return Decimal(str(val))

    if component.calculation_type == 'percentage':
        pct      = float(override_value) if override_value is not None else float(component.default_value)
        base_key = getattr(component, 'pct_base', None) or 'monthly_equiv'
        base_val = vars_ctx.get(base_key, vars_ctx.get('monthly_equiv', 0))
        return Decimal(str(base_val * pct / 100)).quantize(Decimal('0.01'))

    if component.calculation_type == 'formula':
        return eval_formula(getattr(component, 'formula', '') or '', vars_ctx)

    return Decimal('0')


# ═══════════════════════════════════════════════════════════════════════════════
#  THE canonical payroll computation
# ═══════════════════════════════════════════════════════════════════════════════

def compute_employee_payroll(employee, period, components=None):
    """
    Computes full payroll for one employee + one period.
    Does NOT save anything — returns a result dict for the caller to persist.

    Args:
        employee:   Employee instance (must have salary_grade set)
        period:     PayrollPeriod instance
        components: iterable of active PayrollComponent, in sort_order.
                    If None, fetches all active components automatically.

    Returns dict:
        {
          'hours_worked':     Decimal,
          'hourly_rate':      Decimal,
          'basic_pay':        Decimal,   # includes calendar premium
          'calendar_premium': Decimal,
          'gross_pay':        Decimal,
          'total_deductions': Decimal,
          'net_pay':          Decimal,
          'breakdown':        [ {name, operator, amount, type, description}, ... ],
        }

    Raises:
        ValueError if employee has no salary_grade assigned.
    """
    from payroll.models import PayrollComponent, EmployeePayrollComponent, Adjustment
    from attendance.models import Attendance

    sg = employee.salary_grade
    if not sg:
        raise ValueError(f'Employee {employee} has no salary grade assigned.')

    hourly_rate = Decimal(str(sg.hourly_rate))
    ot_rate     = Decimal(str(sg.overtime_rate))

    # ── Step 1: Hours worked from attendance ────────────────────────────────
    att_qs = Attendance.objects.filter(
        employee=employee,
        date__gte=period.start_date,
        date__lte=period.end_date,
    )
    hours_worked = Decimal(str(att_qs.aggregate(t=Sum('total_hours'))['t'] or 0))
    late_minutes = att_qs.aggregate(t=Sum('late_minutes'))['t'] or 0
    days_present = att_qs.filter(status__in=['present', 'late']).count()
    days_absent  = att_qs.filter(status='absent').count()

    # ── Step 2: Basic pay ─────────────────────────────────────────────────────
    basic_pay = (hourly_rate * hours_worked).quantize(Decimal('0.01'), ROUND_HALF_UP)

    # ── Step 3: Calendar holiday / rest day premium ──────────────────────────
    calendar_premium, calendar_breakdown = compute_calendar_premium(employee, period, hourly_rate)
    basic_pay += calendar_premium

    daily_rate    = hourly_rate * STANDARD_HOURS_PER_DAY
    monthly_equiv = daily_rate * STANDARD_WORKING_DAYS

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

    running      = basic_pay
    total_earn   = basic_pay
    total_deduct = Decimal('0')
    breakdown    = []

    if calendar_premium > 0:
        breakdown.append({
            'name':        'Holiday / Rest Day Pay Premium',
            'operator':    '+',
            'amount':      float(calendar_premium),
            'type':        'earning',
            'description': f'{len(calendar_breakdown)} premium day(s) worked',
        })

    # ── Step 4: Modular components ────────────────────────────────────────────
    if components is None:
        components = PayrollComponent.objects.filter(is_active=True).order_by('sort_order', 'id')

    for comp in components:
        override = EmployeePayrollComponent.objects.filter(
            employee=employee, component=comp, is_active=True
        ).first()
        override_val = Decimal(str(override.value)) if override else None

        amount = compute_component_amount(comp, vars_ctx, override_val)
        op     = getattr(comp, 'operator', None) or ('+' if comp.type == 'earning' else '-')

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
            'name':        comp.name,
            'operator':    op,
            'amount':      float(amount),
            'type':        comp.type,
            'description': getattr(comp, 'description', None) or comp.name,
        })

    # ── Step 5: Overtime adjustments — recomputed live, never trust stale ─────
    #            stored amounts (the rate may have changed since they were saved)
    for adj in Adjustment.objects.filter(employee=employee, payroll_period=period, type='overtime'):
        computed = (Decimal(str(adj.hours)) * ot_rate).quantize(Decimal('0.01'), ROUND_HALF_UP)
        running    += computed
        total_earn += computed
        breakdown.append({
            'name':        f'Overtime Pay ({adj.description or ""})',
            'operator':    '+',
            'amount':      float(computed),
            'type':        'earning',
            'description': f'{adj.hours}h × ₱{float(ot_rate):.2f}/hr',
        })

    # ── Step 6: Leave adjustments — manual ₱ amount, sign decides effect ──────
    for adj in Adjustment.objects.filter(employee=employee, payroll_period=period, type='leave'):
        adj_amount = Decimal(str(adj.amount))
        if adj_amount >= 0:
            running    += adj_amount
            total_earn += adj_amount
            breakdown.append({
                'name':        f'Leave Pay ({adj.description or ""})',
                'operator':    '+',
                'amount':      float(adj_amount),
                'type':        'earning',
                'description': adj.description or 'Leave pay',
            })
        else:
            deduct = abs(adj_amount)
            running      -= deduct
            total_deduct += deduct
            breakdown.append({
                'name':        f'Leave Deduction ({adj.description or ""})',
                'operator':    '-',
                'amount':      float(deduct),
                'type':        'deduction',
                'description': adj.description or 'Unpaid leave',
            })

    net_pay = running.quantize(Decimal('0.01'), ROUND_HALF_UP)

    return {
        'hours_worked':     hours_worked,
        'hourly_rate':      hourly_rate,
        'basic_pay':        basic_pay,
        'calendar_premium': calendar_premium,
        'gross_pay':        total_earn.quantize(Decimal('0.01'), ROUND_HALF_UP),
        'total_deductions': total_deduct.quantize(Decimal('0.01'), ROUND_HALF_UP),
        'net_pay':          net_pay,
        'breakdown':        breakdown,
    }

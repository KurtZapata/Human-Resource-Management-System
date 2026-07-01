from django.db import models
from django.contrib.auth.models import User
from employees.models import Employee


class PayrollPeriod(models.Model):
    # ── 13: PayrollPeriod ──
    STATUS = [('open','Open'),('processing','Processing'),('closed','Closed')]
    start_date = models.DateField()
    end_date   = models.DateField()
    status     = models.CharField(max_length=20, choices=STATUS, default='open')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self): return f'{self.start_date} → {self.end_date} ({self.status})'


class Payroll(models.Model):
    STATUS = [('draft','Draft'),('finalized','Finalized')]

    employee         = models.ForeignKey(Employee, on_delete=models.CASCADE)
    payroll_period   = models.ForeignKey(PayrollPeriod, on_delete=models.CASCADE)
    basic_pay        = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gross_pay        = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_deductions = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    net_pay          = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    status           = models.CharField(max_length=20, choices=STATUS, default='draft')
    processed_by     = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                          related_name='processed_payrolls')
    processed_at     = models.DateTimeField(null=True, blank=True)

    # NOTE: Added for Webpage #7 — Payroll confirmation workflow
    is_confirmed     = models.BooleanField(default=False)
    confirmed_by     = models.ForeignKey(User, on_delete=models.SET_NULL, null=True,
                                          blank=True, related_name='confirmed_payrolls')
    confirmed_at     = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ('employee', 'payroll_period')

    def __str__(self):
        return f'{self.employee} — {self.payroll_period} ({self.status})'


class PayrollComponent(models.Model):
    TYPE_CHOICES = [('earning', 'Earning'), ('deduction', 'Deduction')]
    CALC_TYPES   = [
        ('fixed',      'Fixed Amount'),
        ('percentage', 'Percentage of Variable'),
        ('formula',    'Custom Formula'),
    ]
    OPERATOR_CHOICES = [
        ('+', 'Add (+)'),
        ('-', 'Subtract (−)'),
        ('*', 'Multiply (×)'),
        ('/', 'Divide (÷)'),
    ]

    name             = models.CharField(max_length=150)
    type             = models.CharField(max_length=20, choices=TYPE_CHOICES)
    operator         = models.CharField(max_length=1, choices=OPERATOR_CHOICES, default='+')  # NEW
    calculation_type = models.CharField(max_length=20, choices=CALC_TYPES, default='fixed')
    default_value    = models.DecimalField(max_digits=14, decimal_places=4, default=0)
    pct_base         = models.CharField(max_length=50, blank=True, default='basic_pay')       # NEW
    formula          = models.CharField(max_length=500, blank=True)
    description      = models.TextField(blank=True)                                           # NEW (was CharField)
    is_active        = models.BooleanField(default=True)
    is_locked        = models.BooleanField(default=False)                                     # NEW
    sort_order       = models.PositiveIntegerField(default=0)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['sort_order', 'id']

    def __str__(self):
        return f'{self.name} ({self.type})'


class EmployeePayrollComponent(models.Model):
    # ── 16: EmployeePayrollComponent (Per Employee Override) ──
    employee  = models.ForeignKey(Employee, on_delete=models.CASCADE)
    component = models.ForeignKey(PayrollComponent, on_delete=models.CASCADE)
    value     = models.DecimalField(max_digits=12, decimal_places=4)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('employee', 'component')


class PayrollBreakdown(models.Model):
    # ── 17: PayrollBreakdown ──
    payroll     = models.ForeignKey(Payroll, on_delete=models.CASCADE, related_name='breakdowns')
    component   = models.ForeignKey(PayrollComponent, on_delete=models.SET_NULL, null=True)
    amount      = models.DecimalField(max_digits=12, decimal_places=2)
    description = models.CharField(max_length=200, blank=True)


class Adjustment(models.Model):
    # ── 18: Adjustment (Manual Leave & Overtime) ──
    # NOTE: 'deduction' and 'allowance' added beyond base ERD.
    #   - 'deduction' : flat peso amount, always subtracted (e.g. cash advance)
    #   - 'allowance' : flat peso amount, always added -- a free-form, one-off
    #                   addition to salary (e.g. transportation allowance,
    #                   meal allowance, bonus, reimbursement). Unlike the
    #                   fixed 'leave'/'overtime'/'deduction' categories,
    #                   the admin gives it its own custom `name` so the
    #                   payslip/breakdown shows exactly what it's for.
    TYPE_CHOICES = [
        ('leave', 'Leave'),
        ('overtime', 'Overtime'),
        ('deduction', 'Deduction'),
        ('allowance', 'Allowance'),
    ]
    employee       = models.ForeignKey(Employee, on_delete=models.CASCADE)
    payroll_period = models.ForeignKey(PayrollPeriod, on_delete=models.CASCADE)
    type           = models.CharField(max_length=20, choices=TYPE_CHOICES)
    # NOTE: Added so admins can give allowance/deduction adjustments a
    # custom, user-defined label (e.g. "Transportation Allowance",
    # "13th Month Advance", "Uniform Deduction") instead of a generic one.
    # Optional -- falls back to the type's default label when blank.
    name           = models.CharField(max_length=150, blank=True, default='')
    hours          = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    rate           = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    amount         = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    description    = models.TextField(blank=True)
    leave_type_id  = models.PositiveIntegerField(null=True, blank=True)  # FK to LeaveType
    created_by     = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at     = models.DateTimeField(auto_now_add=True)

    def display_name(self):
        """Returns the admin-given name if set, else a sensible default per type."""
        if self.name:
            return self.name
        defaults = {
            'leave': 'Leave', 'overtime': 'Overtime',
            'deduction': 'Custom Deduction', 'allowance': 'Allowance',
        }
        return defaults.get(self.type, 'Adjustment')


class Payslip(models.Model):
    # ── 21: Payslip ──
    employee     = models.ForeignKey(Employee, on_delete=models.CASCADE)
    payroll      = models.OneToOneField(Payroll, on_delete=models.CASCADE)
    generated_at = models.DateTimeField(auto_now_add=True)
    file_path    = models.FileField(upload_to='payslips/', blank=True, null=True)
    

class SalaryGrade(models.Model):
    """
    Salary grade now stores hourly_rate as the primary field.
    base_salary is kept as a regular field for display/reference
    but payroll is always computed from hourly_rate.
    """
    name          = models.CharField(max_length=100)
    hourly_rate   = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # PRIMARY
    overtime_rate = models.DecimalField(max_digits=8,  decimal_places=2, default=0)
    # base_salary kept for reference / display (= hourly_rate × 8 × 22)
    base_salary   = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at    = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # Auto-compute base_salary from hourly_rate for display purposes
        self.base_salary = (self.hourly_rate * 8 * 22).quantize(
            __import__('decimal').Decimal('0.01')
        )
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name} (₱{self.hourly_rate}/hr)'
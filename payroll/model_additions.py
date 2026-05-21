"""
payroll/model_additions.py
─────────────────────────────────────────────────────────────────────────────
Fields to ADD to the existing Payroll model (payroll/models.py) for Webpage #7.

These are beyond the base ERD and are required for the confirmation workflow.
Add them to the Payroll class and run makemigrations + migrate.

CHANGES FROM BASE ERD (clearly marked):
  - Payroll.is_confirmed  : BooleanField — tracks whether admin confirmed this payroll
  - Payroll.confirmed_by  : FK to User — who confirmed it
  - Payroll.confirmed_at  : DateTimeField — when it was confirmed

─────────────────────────────────────────────────────────────────────────────
In payroll/models.py, update the Payroll model to include:
─────────────────────────────────────────────────────────────────────────────
"""

# Add these three fields to the Payroll model class:
PAYROLL_MODEL_ADDITIONS = """
    # NOTE: Fields added beyond base ERD for Webpage #7 (payroll confirmation workflow)
    is_confirmed   = models.BooleanField(default=False)
    confirmed_by   = models.ForeignKey(
                        'auth.User',
                        on_delete=models.SET_NULL,
                        null=True, blank=True,
                        related_name='confirmed_payrolls'
                     )
    confirmed_at   = models.DateTimeField(null=True, blank=True)
"""

# Full updated Payroll model for reference:
FULL_PAYROLL_MODEL = """
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
"""

# After adding the fields, run:
#   python manage.py makemigrations payroll
#   python manage.py migrate

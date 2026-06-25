import decimal
from django.db import models
from django.contrib.auth.models import User
from accounts.models import Role


class Department(models.Model):
    name        = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    created_at  = models.DateTimeField(auto_now_add=True)

    def __str__(self): return self.name


class Position(models.Model):
    name        = models.CharField(max_length=100)
    department  = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True)
    base_salary = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    created_at  = models.DateTimeField(auto_now_add=True)

    def __str__(self): return self.name


class SalaryGrade(models.Model):
    """
    hourly_rate is the PRIMARY input field.
    base_salary is AUTO-COMPUTED as hourly_rate × 8 hrs × 22 days.
    Payroll always uses hourly_rate × hours_worked — never base_salary directly.
    """
    name          = models.CharField(max_length=100)
    hourly_rate   = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    overtime_rate = models.DecimalField(max_digits=10, decimal_places=4, default=0)
    # base_salary kept as read-only computed reference (shown on UI only)
    base_salary   = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at    = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        # Auto-compute base_salary from hourly_rate for reference/display
        self.base_salary = (
            decimal.Decimal(str(self.hourly_rate)) * 8 * 22
        ).quantize(decimal.Decimal('0.01'))
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name} (₱{self.hourly_rate}/hr)'


class Employee(models.Model):
    EMPLOYMENT_TYPES = [('regular', 'Regular'), ('contract', 'Contract')]
    STATUS_CHOICES   = [('active', 'Active'), ('inactive', 'Inactive')]
    GENDER_CHOICES   = [('male', 'Male'), ('female', 'Female'), ('other', 'Other')]
    CIVIL_CHOICES    = [
        ('single', 'Single'), ('married', 'Married'),
        ('widowed', 'Widowed'), ('separated', 'Separated'),
    ]

    # ── Existing fields ──────────────────────────────────────────────────
    employee_code   = models.CharField(max_length=50, unique=True)
    first_name      = models.CharField(max_length=100)
    last_name       = models.CharField(max_length=100)
    email           = models.EmailField(unique=True)
    phone           = models.CharField(max_length=30, blank=True)
    address         = models.TextField(blank=True)
    date_hired      = models.DateField(null=True, blank=True)
    employment_type = models.CharField(max_length=20, choices=EMPLOYMENT_TYPES, default='regular')
    status          = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    department      = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True)
    position        = models.ForeignKey(Position, on_delete=models.SET_NULL, null=True, blank=True)
    salary_grade    = models.ForeignKey(SalaryGrade, on_delete=models.SET_NULL, null=True, blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    updated_at      = models.DateTimeField(auto_now=True)

    # ── NEW: Personal details ──────────────────────────────────────────────
    middle_name     = models.CharField(max_length=100, blank=True)
    birthdate       = models.DateField(null=True, blank=True)
    gender          = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True)
    civil_status    = models.CharField(max_length=15, choices=CIVIL_CHOICES, blank=True)
    nationality     = models.CharField(max_length=60, blank=True, default='Filipino')

    # ── NEW: Government IDs ────────────────────────────────────────────────
    tin_number        = models.CharField(max_length=30, blank=True)
    sss_number        = models.CharField(max_length=30, blank=True)
    philhealth_number = models.CharField(max_length=30, blank=True)
    pagibig_number    = models.CharField(max_length=30, blank=True)

    # ── NEW: Emergency contact ─────────────────────────────────────────────
    emergency_contact_name         = models.CharField(max_length=150, blank=True)
    emergency_contact_phone        = models.CharField(max_length=30, blank=True)
    emergency_contact_relationship = models.CharField(max_length=60, blank=True)

    # ── NEW: Contract dates (for contractual employees) ────────────────────
    contract_start  = models.DateField(null=True, blank=True)
    contract_end    = models.DateField(null=True, blank=True)

    def __str__(self): 
        return f'{self.last_name}, {self.first_name}'

    @property
    def full_name(self): 
        return f'{self.first_name} {self.last_name}'

    @property
    def is_contract_expired(self):
        if self.employment_type == 'contract' and self.contract_end:
            from datetime import date
            return date.today() > self.contract_end
        return False


class SystemUser(models.Model):
    username      = models.CharField(max_length=150, unique=True)
    password_hash = models.CharField(max_length=255)
    employee      = models.OneToOneField(Employee, on_delete=models.SET_NULL, null=True, blank=True)
    role          = models.ForeignKey(Role, on_delete=models.SET_NULL, null=True)
    is_active     = models.BooleanField(default=True)
    last_login    = models.DateTimeField(null=True, blank=True)
    created_at    = models.DateTimeField(auto_now_add=True)

    def __str__(self): return self.username


class LeaveBalance(models.Model):
    from accounts.models import LeaveType
    employee       = models.ForeignKey(Employee, on_delete=models.CASCADE)
    leave_type     = models.ForeignKey(LeaveType, on_delete=models.CASCADE)
    remaining_days = models.DecimalField(max_digits=5, decimal_places=1, default=0)


class CompanySettings(models.Model):
    company_name    = models.CharField(max_length=200, default='Your Company')
    company_initials= models.CharField(max_length=5, default='HR')
    company_logo    = models.ImageField(upload_to='company/', blank=True, null=True)
    login_bg_image  = models.ImageField(upload_to='company/', blank=True, null=True)
    workday_start   = models.TimeField(default='08:00:00')
    workday_end     = models.TimeField(default='17:00:00')
    lunch_start     = models.TimeField(default='12:00:00')
    lunch_end       = models.TimeField(default='13:00:00')

    class Meta:
        verbose_name = 'Company Settings'
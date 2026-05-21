from django.db import models
from employees.models import Employee


class Attendance(models.Model):
    # ── 9: Attendance ──
    # NOTE: Split into AM/PM halves beyond base ERD (required for 1st/2nd half UI)
    STATUS_CHOICES = [('present','Present'),('absent','Absent'),('late','Late')]

    employee          = models.ForeignKey(Employee, on_delete=models.CASCADE)
    date              = models.DateField()
    time_in_am        = models.TimeField(null=True, blank=True)  # NOTE: added
    time_out_am       = models.TimeField(null=True, blank=True)  # NOTE: added
    time_in_pm        = models.TimeField(null=True, blank=True)  # NOTE: added
    time_out_pm       = models.TimeField(null=True, blank=True)  # NOTE: added
    total_hours       = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    late_minutes      = models.PositiveIntegerField(default=0)
    undertime_minutes = models.PositiveIntegerField(default=0)
    status            = models.CharField(max_length=20, choices=STATUS_CHOICES, default='present')
    created_at        = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('employee', 'date')

    def __str__(self): return f'{self.employee} — {self.date} ({self.status})'


class OTP(models.Model):
    # ── 10: OTP ──
    code                 = models.CharField(max_length=10)
    created_at           = models.DateTimeField(auto_now_add=True)
    expires_at           = models.DateTimeField()
    is_used              = models.BooleanField(default=False)
    used_by_employee     = models.ForeignKey(Employee, on_delete=models.SET_NULL,
                                              null=True, blank=True)


class AttendanceLog(models.Model):
    # ── 11: AttendanceLog (Audit Trail) ──
    employee    = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True)
    action      = models.CharField(max_length=50)   # time_in / time_out / time_in_FAILED
    timestamp   = models.DateTimeField(auto_now_add=True)
    ip_address  = models.GenericIPAddressField(null=True, blank=True)
    device_info = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-timestamp']
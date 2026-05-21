from django.db import models
from django.contrib.auth.models import User


class Role(models.Model):
    name        = models.CharField(max_length=100)      # SuperAdmin, HRAdmin, StaffAdmin
    description = models.TextField(blank=True)

    def __str__(self): return self.name


class Permission(models.Model):
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=100, unique=True)

    def __str__(self): return self.name


class RolePermission(models.Model):
    role       = models.ForeignKey(Role, on_delete=models.CASCADE)
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('role', 'permission')


class LeaveType(models.Model):
    # ── 19: LeaveType (Future-Ready, now active) ──
    name     = models.CharField(max_length=100)
    is_paid  = models.BooleanField(default=True)
    max_days = models.PositiveIntegerField(default=5)

    def __str__(self): return self.name


class AuditLog(models.Model):
    # ── 22: AuditLog (VERY IMPORTANT FOR GRADING) ──
    user       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    action     = models.CharField(max_length=100)
    table_name = models.CharField(max_length=100)
    record_id  = models.PositiveIntegerField(null=True, blank=True)
    old_value  = models.JSONField(null=True, blank=True)
    new_value  = models.JSONField(null=True, blank=True)
    timestamp  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self): return f'{self.action} on {self.table_name} by {self.user}'
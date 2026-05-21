from django.db import models
from django.contrib.auth.models import User


class Calendar(models.Model):
    # ── 12: Calendar ──
    # NOTE: Added rate_multiplier field beyond ERD for holiday pay computation
    TYPE_CHOICES = [
        ('workday',         'Work Day'),
        ('regular_holiday', 'Regular Holiday'),
        ('special_holiday', 'Special Holiday'),
        ('rest',            'Rest Day'),
    ]
    date             = models.DateField(unique=True)
    type             = models.CharField(max_length=30, choices=TYPE_CHOICES, default='workday')
    is_paid          = models.BooleanField(default=True)
    rate_multiplier  = models.DecimalField(max_digits=4, decimal_places=2, default=1.00)  # NOTE: added
    description      = models.CharField(max_length=200, blank=True)
    created_by       = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at       = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['date']

    def __str__(self): return f'{self.date} ({self.type})'
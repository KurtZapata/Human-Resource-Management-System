"""
calendar_app/views.py
Company Calendar management — admin can configure each date
(holiday type, pay rate multiplier, description).
Maps to: Webpage #5 (Company Calendar)

Database table: Calendar
    id, date (UNIQUE), type (workday/holiday/special), is_paid, description,
    created_by (FK→User), created_at
"""

import json
from datetime import date, timedelta

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.contrib import messages
from django.utils import timezone
from accounts.access import admin_required, is_super_admin, is_hr_admin
from .models import Calendar
from accounts.models import AuditLog
from employees.views import _branding


@admin_required
def calendar_index(request):
    """
    Main company calendar page (Webpage #5).
    Passes all configured Calendar entries as JSON for JS rendering.
    Upcoming holidays shown in the side panel.
    """
    today = date.today()

    # All calendar entries → serialise as {date_str: entry_dict} for JS
    all_entries = Calendar.objects.all()
    calendar_json = {}
    for entry in all_entries:
        calendar_json[str(entry.date)] = {
            'type':             entry.type,
            'description':      entry.description,
            'is_paid':          entry.is_paid,
            'rate_multiplier':  float(entry.rate_multiplier) if entry.rate_multiplier else 1.0,
        }

    # Upcoming holidays (next 60 days)
    upcoming = Calendar.objects.filter(
        date__gte=today,
        date__lte=today + timedelta(days=60),
        type__in=['regular_holiday', 'special_holiday'],
    ).order_by('date')[:10]

    context = {
        'calendar_json':     json.dumps(calendar_json),
        'upcoming_holidays': upcoming,
        'today':             today,
        **_branding(),
    }
    return render(request, 'hrms/calendar_admin.html', context)


@admin_required
@require_POST
def configure_day(request):
    """
    POST: Create or update a Calendar entry for a specific date.
    Fields map directly to Calendar model columns.

    NOTE: rate_multiplier is stored in this view even though the Calendar model
    doesn't have it natively in the original ERD. We added it as a DecimalField
    (migration required). This is necessary for holiday pay computation in Payroll.
    """
    d = request.POST

    date_str        = d.get('date', '').strip()
    day_type        = d.get('type', 'workday')
    is_paid_str     = d.get('is_paid', 'true')
    rate_multiplier = d.get('rate_multiplier', '1.00')
    description     = d.get('description', '').strip()

    if not date_str:
        messages.error(request, 'Date is required.')
        return redirect('calendar_app:index')

    try:
        day_date = date.fromisoformat(date_str)
    except ValueError:
        messages.error(request, 'Invalid date format.')
        return redirect('calendar_app:index')

    is_paid = (is_paid_str.lower() == 'true')

    cal_entry, created = Calendar.objects.update_or_create(
        date=day_date,
        defaults={
            'type':             day_type,
            'is_paid':          is_paid,
            'rate_multiplier':  float(rate_multiplier),
            'description':      description,
            'created_by':       request.user,
        }
    )

    action = 'CREATE' if created else 'UPDATE'
    AuditLog.objects.create(
        user=request.user,
        action=action,
        table_name='calendar_app_calendar',
        record_id=cal_entry.id,
        new_value={
            'date': date_str, 'type': day_type,
            'is_paid': is_paid, 'rate_multiplier': rate_multiplier,
            'description': description,
        },
        timestamp=timezone.now(),
    )

    label = description or day_type.replace('_', ' ').title()
    messages.success(request, f'Calendar updated: {date_str} → {label}')
    return redirect('calendar_app:index')


@admin_required
@require_POST
def remove_day(request):
    """
    POST: Remove a Calendar entry (revert date to system default — workday or weekend).
    """
    date_str = request.POST.get('date', '').strip()
    try:
        day_date = date.fromisoformat(date_str)
    except ValueError:
        messages.error(request, 'Invalid date.')
        return redirect('calendar_app:index')

    entry = Calendar.objects.filter(date=day_date).first()
    if entry:
        AuditLog.objects.create(
            user=request.user, action='DELETE',
            table_name='calendar_app_calendar', record_id=entry.id,
            old_value={'date': date_str, 'type': entry.type, 'description': entry.description},
            timestamp=timezone.now(),
        )
        entry.delete()
        messages.success(request, f'Calendar entry for {date_str} removed.')
    else:
        messages.info(request, f'No custom entry found for {date_str}.')
    return redirect('calendar_app:index')


@admin_required
@require_POST
def add_holiday(request):
    """
    POST: Shortcut to add a holiday from the side panel modal.
    Delegates to configure_day logic.
    """
    # Reuse configure_day by forwarding to it
    # (avoids code duplication)
    return configure_day(request)


@admin_required
def get_day_info(request):
    """
    AJAX GET: Returns configuration for a specific date.
    Used to pre-populate the day config modal.
    """
    date_str = request.GET.get('date', '').strip()
    try:
        day_date = date.fromisoformat(date_str)
    except ValueError:
        return JsonResponse({'error': 'Invalid date'}, status=400)

    entry = Calendar.objects.filter(date=day_date).first()
    if entry:
        return JsonResponse({
            'date':             str(entry.date),
            'type':             entry.type,
            'is_paid':          entry.is_paid,
            'rate_multiplier':  float(entry.rate_multiplier) if entry.rate_multiplier else 1.0,
            'description':      entry.description,
        })
    # Return default workday info
    dow = day_date.weekday()  # 0=Mon … 6=Sun
    return JsonResponse({
        'date':            date_str,
        'type':            'rest' if dow >= 5 else 'workday',
        'is_paid':         False if dow >= 5 else True,
        'rate_multiplier': 1.0,
        'description':     '',
    })


@admin_required
def calendar_api_month(request):
    """
    AJAX GET: Returns all Calendar entries for a given year+month.
    Used by JS when navigating months so the calendar doesn't need a full reload.

    Query params: ?year=2025&month=6  (month is 1-indexed)
    """
    year  = int(request.GET.get('year',  date.today().year))
    month = int(request.GET.get('month', date.today().month))

    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)

    entries = Calendar.objects.filter(date__gte=start, date__lte=end)
    data = {}
    for e in entries:
        data[str(e.date)] = {
            'type':            e.type,
            'description':     e.description,
            'is_paid':         e.is_paid,
            'rate_multiplier': float(e.rate_multiplier) if e.rate_multiplier else 1.0,
        }
    return JsonResponse({'entries': data, 'year': year, 'month': month})

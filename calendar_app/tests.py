"""
apps/calendar_app/tests.py
═══════════════════════════════════════════════════════════════════════════════
CRUD tests for the company Calendar — configuring holidays, rest days,
and pay rate multipliers.

Run with:
    python manage.py test calendar_app
═══════════════════════════════════════════════════════════════════════════════
"""

from datetime import date
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounts.models import AuditLog
from .models import Calendar


class CalendarCRUDTestBase(TestCase):

    def setUp(self):
        self.admin = User.objects.create_superuser(
            'superadmin', 'admin@example.com', 'adminpass123'
        )
        self.client.login(username='superadmin', password='adminpass123')


# ═══════════════════════════════════════════════════════════════════════════════
#  Configure day — create / update
# ═══════════════════════════════════════════════════════════════════════════════

class ConfigureDayTests(CalendarCRUDTestBase):

    def test_create_regular_holiday(self):
        self.client.post(reverse('calendar_app:configure_day'), {
            'date': '2026-06-12', 'type': 'regular_holiday',
            'is_paid': 'true', 'rate_multiplier': '2.00',
            'description': 'Independence Day',
        })
        entry = Calendar.objects.get(date=date(2026, 6, 12))
        self.assertEqual(entry.type, 'regular_holiday')
        self.assertEqual(entry.rate_multiplier, Decimal('2.00'))
        self.assertEqual(entry.description, 'Independence Day')

    def test_configure_day_is_update_or_create_not_duplicate(self):
        """
        Configuring the same date twice must UPDATE the existing entry,
        never create a second row for that date.
        """
        self.client.post(reverse('calendar_app:configure_day'), {
            'date': '2026-06-12', 'type': 'regular_holiday',
            'is_paid': 'true', 'rate_multiplier': '2.00',
            'description': 'Independence Day',
        })
        self.client.post(reverse('calendar_app:configure_day'), {
            'date': '2026-06-12', 'type': 'special_holiday',
            'is_paid': 'false', 'rate_multiplier': '1.30',
            'description': 'Reclassified',
        })
        self.assertEqual(Calendar.objects.filter(date=date(2026, 6, 12)).count(), 1)
        entry = Calendar.objects.get(date=date(2026, 6, 12))
        self.assertEqual(entry.type, 'special_holiday')
        self.assertEqual(entry.rate_multiplier, Decimal('1.30'))

    def test_configure_day_requires_a_valid_date(self):
        self.client.post(reverse('calendar_app:configure_day'), {
            'date': 'not-a-date', 'type': 'regular_holiday',
            'is_paid': 'true', 'rate_multiplier': '2.00',
        })
        self.assertEqual(Calendar.objects.count(), 0)

    def test_create_writes_audit_log_as_create(self):
        self.client.post(reverse('calendar_app:configure_day'), {
            'date': '2026-06-12', 'type': 'regular_holiday',
            'is_paid': 'true', 'rate_multiplier': '2.00',
            'description': 'Independence Day',
        })
        log = AuditLog.objects.filter(table_name='calendar_app_calendar').first()
        self.assertIsNotNone(log)
        self.assertEqual(log.action, 'CREATE')

    def test_reconfigure_existing_day_writes_audit_log_as_update(self):
        Calendar.objects.create(
            date=date(2026, 6, 12), type='workday', is_paid=True,
            rate_multiplier=Decimal('1.00'), created_by=self.admin,
        )
        self.client.post(reverse('calendar_app:configure_day'), {
            'date': '2026-06-12', 'type': 'regular_holiday',
            'is_paid': 'true', 'rate_multiplier': '2.00',
            'description': 'Reclassified to holiday',
        })
        log = AuditLog.objects.filter(
            table_name='calendar_app_calendar', action='UPDATE'
        ).first()
        self.assertIsNotNone(log)


# ═══════════════════════════════════════════════════════════════════════════════
#  Add holiday — shortcut entry point (delegates to configure_day)
# ═══════════════════════════════════════════════════════════════════════════════

class AddHolidayTests(CalendarCRUDTestBase):

    def test_add_holiday_creates_calendar_entry(self):
        self.client.post(reverse('calendar_app:add_holiday'), {
            'date': '2026-08-21', 'type': 'special_holiday',
            'is_paid': 'false', 'rate_multiplier': '1.30',
            'description': 'Ninoy Aquino Day',
        })
        entry = Calendar.objects.get(date=date(2026, 8, 21))
        self.assertEqual(entry.description, 'Ninoy Aquino Day')


# ═══════════════════════════════════════════════════════════════════════════════
#  Remove day — delete (revert to system default)
# ═══════════════════════════════════════════════════════════════════════════════

class RemoveDayTests(CalendarCRUDTestBase):

    def test_remove_day_deletes_entry(self):
        Calendar.objects.create(
            date=date(2026, 6, 12), type='regular_holiday', is_paid=True,
            rate_multiplier=Decimal('2.00'), created_by=self.admin,
        )
        self.client.post(reverse('calendar_app:remove_day'), {'date': '2026-06-12'})
        self.assertFalse(Calendar.objects.filter(date=date(2026, 6, 12)).exists())

    def test_remove_nonexistent_day_does_not_crash(self):
        resp = self.client.post(reverse('calendar_app:remove_day'), {'date': '2026-01-01'})
        self.assertEqual(resp.status_code, 302)  # redirects gracefully, no 500

    def test_remove_day_writes_audit_log(self):
        entry = Calendar.objects.create(
            date=date(2026, 6, 12), type='regular_holiday', is_paid=True,
            rate_multiplier=Decimal('2.00'), created_by=self.admin,
        )
        self.client.post(reverse('calendar_app:remove_day'), {'date': '2026-06-12'})
        log = AuditLog.objects.filter(
            table_name='calendar_app_calendar', action='DELETE', record_id=entry.id
        ).first()
        self.assertIsNotNone(log)


# ═══════════════════════════════════════════════════════════════════════════════
#  Get day info — read
# ═══════════════════════════════════════════════════════════════════════════════

class GetDayInfoTests(CalendarCRUDTestBase):

    def test_get_configured_day_returns_its_data(self):
        Calendar.objects.create(
            date=date(2026, 6, 12), type='regular_holiday', is_paid=True,
            rate_multiplier=Decimal('2.00'), description='Holiday', created_by=self.admin,
        )
        resp = self.client.get(reverse('calendar_app:get_day_info'), {'date': '2026-06-12'})
        data = resp.json()
        self.assertEqual(data['type'], 'regular_holiday')
        self.assertEqual(float(data['rate_multiplier']), 2.00)

    def test_get_unconfigured_weekday_defaults_to_workday(self):
        # 2026-06-03 is a Wednesday
        resp = self.client.get(reverse('calendar_app:get_day_info'), {'date': '2026-06-03'})
        data = resp.json()
        self.assertEqual(data['type'], 'workday')
        self.assertEqual(float(data['rate_multiplier']), 1.0)

    def test_get_unconfigured_weekend_defaults_to_rest(self):
        # 2026-06-06 is a Saturday
        resp = self.client.get(reverse('calendar_app:get_day_info'), {'date': '2026-06-06'})
        data = resp.json()
        self.assertEqual(data['type'], 'rest')

    def test_get_day_info_requires_valid_date(self):
        resp = self.client.get(reverse('calendar_app:get_day_info'), {'date': 'garbage'})
        self.assertEqual(resp.status_code, 400)


# ═══════════════════════════════════════════════════════════════════════════════
#  Month API — used by the calendar admin page's month navigation
# ═══════════════════════════════════════════════════════════════════════════════

class CalendarApiMonthTests(CalendarCRUDTestBase):

    def test_returns_only_entries_within_the_requested_month(self):
        Calendar.objects.create(
            date=date(2026, 6, 12), type='regular_holiday', is_paid=True,
            rate_multiplier=Decimal('2.00'), created_by=self.admin,
        )
        Calendar.objects.create(
            date=date(2026, 7, 4), type='special_holiday', is_paid=False,
            rate_multiplier=Decimal('1.30'), created_by=self.admin,
        )
        resp = self.client.get(reverse('calendar_app:api_month'), {'year': 2026, 'month': 6})
        data = resp.json()
        self.assertIn('2026-06-12', data['entries'])
        self.assertNotIn('2026-07-04', data['entries'])

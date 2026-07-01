"""
apps/employees/management/commands/seed_demo_data.py
═══════════════════════════════════════════════════════════════════════════════
Seeds the database with realistic DEMO data so payroll can be run and
verified end-to-end without manually typing in employees and attendance
by hand.

This command creates:
    - 2 Departments (Human Resources, Field Operations)
    - Positions under each department
    - Salary Grades (hourly_rate-based, per the current SalaryGrade model)
    - 5 Employees spread across both departments/positions/salary grades
    - 1 open PayrollPeriod covering June 2026 (start/end only -- this
      command does NOT run payroll; that's left for you to trigger)
    - A full month of weekday Attendance records for June 2026 per
      employee, with realistic variation:
        * normal full-day attendance
        * occasional late arrivals (late_minutes > 0)
        * occasional early departures / undertime
        * occasional absences (status='absent', zero hours)
        * occasional overtime days (hours worked > 8)

Hours / lateness / undertime are computed with the SAME helper functions
the real system uses (attendance/utils.py), so the seeded numbers are
internally consistent with how attendance is calculated everywhere else
(payroll's hours_worked lookup, admin manual edits, OTP logging).

This command is idempotent: employees are matched on employee_code and
attendance is matched on (employee, date), so re-running it will not
create duplicates -- it will just update existing rows.

Run with:
    python manage.py seed_demo_data

To wipe just the demo employees (and their attendance/payroll-period
rows via cascade) before reseeding:
    python manage.py seed_demo_data --flush-demo
═══════════════════════════════════════════════════════════════════════════════
"""
import random
from datetime import date, timedelta, time as dtime
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from employees.models import Department, Position, SalaryGrade, Employee
from payroll.models import PayrollPeriod
from attendance.models import Attendance
from attendance.utils import (
    calculate_total_hours,
    calculate_late_minutes,
    calculate_undertime_minutes,
)

# ── Demo employee roster ───────────────────────────────────────────────────
# employee_code is the stable identifier used for idempotent seeding /
# flushing. Feel free to add more entries here -- the attendance generator
# below works for any number of employees.
EMPLOYEE_DATA = [
    {
        'employee_code': '2026-1001', 'first_name': 'Maria', 'last_name': 'Santos',
        'email': 'maria.santos@demo.local', 'phone': '0917-100-1001',
        'address': '12 Mabini St., Quezon City',
        'date_hired': date(2023, 3, 15), 'employment_type': 'regular', 'status': 'active',
        'department': 'Human Resources', 'position': 'HR Officer', 'salary_grade': 'Supervisory',
        'gender': 'female', 'civil_status': 'married', 'birthdate': date(1990, 4, 12),
        'sss_number': '34-1234567-8', 'philhealth_number': '12-345678901-2',
        'pagibig_number': '1234-5678-9012', 'tin_number': '123-456-789-000',
        'emergency_contact_name': 'Jose Santos', 'emergency_contact_phone': '0917-200-2001',
        'emergency_contact_relationship': 'Spouse',
    },
    {
        'employee_code': '2026-1002', 'first_name': 'Juan', 'last_name': 'Dela Cruz',
        'email': 'juan.delacruz@demo.local', 'phone': '0917-100-1002',
        'address': '45 Rizal Ave., Manila',
        'date_hired': date(2024, 1, 8), 'employment_type': 'regular', 'status': 'active',
        'department': 'Human Resources', 'position': 'HR Assistant', 'salary_grade': 'Rank and File',
        'gender': 'male', 'civil_status': 'single', 'birthdate': date(1996, 9, 2),
        'sss_number': '34-2234567-8', 'philhealth_number': '12-245678901-2',
        'pagibig_number': '1234-6678-9012', 'tin_number': '123-556-789-000',
        'emergency_contact_name': 'Ana Dela Cruz', 'emergency_contact_phone': '0917-200-2002',
        'emergency_contact_relationship': 'Mother',
    },
    {
        'employee_code': '2026-1003', 'first_name': 'Liza', 'last_name': 'Reyes',
        'email': 'liza.reyes@demo.local', 'phone': '0917-100-1003',
        'address': '78 Bonifacio St., Makati',
        'date_hired': date(2022, 6, 20), 'employment_type': 'regular', 'status': 'active',
        'department': 'Field Operations', 'position': 'Field Supervisor', 'salary_grade': 'Supervisory',
        'gender': 'female', 'civil_status': 'single', 'birthdate': date(1988, 11, 30),
        'sss_number': '34-3234567-8', 'philhealth_number': '12-345178901-2',
        'pagibig_number': '1234-5178-9012', 'tin_number': '123-456-189-000',
        'emergency_contact_name': 'Carmen Reyes', 'emergency_contact_phone': '0917-200-2003',
        'emergency_contact_relationship': 'Sister',
    },
    {
        'employee_code': '2026-1004', 'first_name': 'Mark', 'last_name': 'Villanueva',
        'email': 'mark.villanueva@demo.local', 'phone': '0917-100-1004',
        'address': '9 Aguinaldo Rd., Pasig',
        'date_hired': date(2024, 5, 2), 'employment_type': 'regular', 'status': 'active',
        'department': 'Field Operations', 'position': 'Operations Staff', 'salary_grade': 'Rank and File',
        'gender': 'male', 'civil_status': 'married', 'birthdate': date(1993, 2, 17),
        'sss_number': '34-4234567-8', 'philhealth_number': '12-345678111-2',
        'pagibig_number': '1234-5678-1112', 'tin_number': '123-456-781-100',
        'emergency_contact_name': 'Grace Villanueva', 'emergency_contact_phone': '0917-200-2004',
        'emergency_contact_relationship': 'Spouse',
    },
    {
        'employee_code': '2026-1005', 'first_name': 'Ella', 'last_name': 'Bautista',
        'email': 'ella.bautista@demo.local', 'phone': '0917-100-1005',
        'address': '3 Luna St., Taguig',
        'date_hired': date(2025, 9, 1), 'employment_type': 'contract', 'status': 'active',
        'department': 'Field Operations', 'position': 'Warehouse Clerk', 'salary_grade': 'Rank and File',
        'gender': 'female', 'civil_status': 'single', 'birthdate': date(1999, 7, 21),
        'sss_number': '34-5234567-8', 'philhealth_number': '12-345678901-9',
        'pagibig_number': '1234-5678-9019', 'tin_number': '123-456-789-199',
        'emergency_contact_name': 'Ramon Bautista', 'emergency_contact_phone': '0917-200-2005',
        'emergency_contact_relationship': 'Father',
        'contract_start': date(2025, 9, 1), 'contract_end': date(2026, 8, 31),
    },
]

DEPARTMENTS = {
    'Human Resources': 'Handles hiring, employee records, and company policy.',
    'Field Operations': 'Handles day-to-day operational and warehouse work.',
}

POSITIONS = {
    'HR Officer':        ('Human Resources', Decimal('35000.00')),
    'HR Assistant':      ('Human Resources', Decimal('22000.00')),
    'Field Supervisor':  ('Field Operations', Decimal('32000.00')),
    'Operations Staff':  ('Field Operations', Decimal('20000.00')),
    'Warehouse Clerk':   ('Field Operations', Decimal('18000.00')),
}

SALARY_GRADES = {
    # name: (hourly_rate, overtime_rate)
    'Supervisory':   (Decimal('180.0000'), Decimal('225.0000')),
    'Rank and File': (Decimal('110.0000'), Decimal('137.5000')),
}

ATTENDANCE_PERIOD_START = date(2026, 6, 1)
ATTENDANCE_PERIOD_END = date(2026, 6, 30)

STANDARD_TIME_IN_AM = dtime(8, 0, 0)
STANDARD_TIME_OUT_AM = dtime(12, 0, 0)
STANDARD_TIME_IN_PM = dtime(13, 0, 0)
STANDARD_TIME_OUT_PM = dtime(17, 0, 0)


class Command(BaseCommand):
    help = (
        "Seeds demo employees (with departments, positions, salary grades), "
        "an open June 2026 payroll period, and a full month of weekday "
        "attendance records with late/absent/overtime variation."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--flush-demo',
            action='store_true',
            help='Delete existing demo employees (matched by employee_code) '
                 'and their attendance before reseeding.',
        )

    def handle(self, *args, **options):
        if options['flush_demo']:
            self._flush_demo_data()

        with transaction.atomic():
            departments = self._seed_departments()
            positions = self._seed_positions(departments)
            salary_grades = self._seed_salary_grades()
            employees = self._seed_employees(departments, positions, salary_grades)
            period = self._seed_payroll_period()
            attendance_count = self._seed_attendance(employees)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(employees)} demo employees, "
            f"{attendance_count} attendance records "
            f"({ATTENDANCE_PERIOD_START} to {ATTENDANCE_PERIOD_END}), "
            f"and payroll period #{period.id} "
            f"({period.start_date} to {period.end_date}, status='{period.status}')."
        ))
        self.stdout.write(
            "Payroll itself was NOT run -- this data is ready for you to "
            "process through the normal payroll workflow."
        )

    # ── Flush ────────────────────────────────────────────────────────────
    def _flush_demo_data(self):
        codes = [e['employee_code'] for e in EMPLOYEE_DATA]
        qs = Employee.objects.filter(employee_code__in=codes)
        count = qs.count()
        # Attendance rows cascade-delete with the employee (on_delete=CASCADE).
        qs.delete()
        self.stdout.write(f"Flushed {count} previously seeded demo employee(s).")

    # ── Departments / Positions / Salary Grades ─────────────────────────
    def _seed_departments(self):
        departments = {}
        for name, description in DEPARTMENTS.items():
            dept, _ = Department.objects.get_or_create(
                name=name, defaults={'description': description},
            )
            departments[name] = dept
        return departments

    def _seed_positions(self, departments):
        positions = {}
        for name, (dept_name, base_salary) in POSITIONS.items():
            pos, _ = Position.objects.get_or_create(
                name=name,
                department=departments[dept_name],
                defaults={'base_salary': base_salary},
            )
            positions[name] = pos
        return positions

    def _seed_salary_grades(self):
        grades = {}
        for name, (hourly_rate, overtime_rate) in SALARY_GRADES.items():
            grade, created = SalaryGrade.objects.get_or_create(
                name=name,
                defaults={'hourly_rate': hourly_rate, 'overtime_rate': overtime_rate},
            )
            if not created and (grade.hourly_rate != hourly_rate or grade.overtime_rate != overtime_rate):
                grade.hourly_rate = hourly_rate
                grade.overtime_rate = overtime_rate
                grade.save()  # re-triggers base_salary auto-computation
            grades[name] = grade
        return grades

    # ── Employees ────────────────────────────────────────────────────────
    def _seed_employees(self, departments, positions, salary_grades):
        employees = []
        for data in EMPLOYEE_DATA:
            fields = dict(data)
            dept_name = fields.pop('department')
            pos_name = fields.pop('position')
            grade_name = fields.pop('salary_grade')
            code = fields.pop('employee_code')

            fields['department'] = departments[dept_name]
            fields['position'] = positions[pos_name]
            fields['salary_grade'] = salary_grades[grade_name]

            emp, _ = Employee.objects.update_or_create(
                employee_code=code, defaults=fields,
            )
            employees.append(emp)
        return employees

    # ── Payroll period (data only -- payroll is NOT run here) ──────────
    def _seed_payroll_period(self):
        period, _ = PayrollPeriod.objects.get_or_create(
            start_date=ATTENDANCE_PERIOD_START,
            end_date=ATTENDANCE_PERIOD_END,
            defaults={'status': 'open'},
        )
        return period

    # ── Attendance ───────────────────────────────────────────────────────
    def _seed_attendance(self, employees):
        weekdays = self._weekdays_in_range(ATTENDANCE_PERIOD_START, ATTENDANCE_PERIOD_END)
        total = 0
        for emp in employees:
            # Seeded per-employee so results are varied but reproducible
            # across re-runs of this command.
            rng = random.Random(emp.employee_code)
            for day in weekdays:
                pattern = self._pick_pattern(rng)
                self._create_attendance_record(emp, day, pattern)
                total += 1
        return total

    @staticmethod
    def _weekdays_in_range(start, end):
        days = []
        current = start
        while current <= end:
            if current.isoweekday() <= 5:  # Mon-Fri
                days.append(current)
            current += timedelta(days=1)
        return days

    @staticmethod
    def _pick_pattern(rng):
        """
        Picks a realistic daily attendance pattern.
        Roughly: 6% absent, 18% late, 10% undertime (left early),
        8% overtime, 58% normal full day.
        """
        roll = rng.random()
        if roll < 0.06:
            return 'absent'
        elif roll < 0.24:
            return 'late'
        elif roll < 0.34:
            return 'undertime'
        elif roll < 0.42:
            return 'overtime'
        return 'normal'

    def _create_attendance_record(self, employee, day, pattern, rng=None):
        rng = rng or random.Random(f'{employee.employee_code}-{day.isoformat()}')

        if pattern == 'absent':
            Attendance.objects.update_or_create(
                employee=employee, date=day,
                defaults=dict(
                    time_in_am=None, time_out_am=None,
                    time_in_pm=None, time_out_pm=None,
                    total_hours=Decimal('0.00'),
                    late_minutes=0, undertime_minutes=0,
                    status='absent',
                ),
            )
            return

        time_in_am = STANDARD_TIME_IN_AM
        time_out_am = STANDARD_TIME_OUT_AM
        time_in_pm = STANDARD_TIME_IN_PM
        time_out_pm = STANDARD_TIME_OUT_PM
        status = 'present'

        if pattern == 'late':
            minutes_late = rng.choice([5, 10, 15, 20, 30, 45])
            hour, minute = divmod(8 * 60 + minutes_late, 60)
            time_in_am = dtime(hour, minute, 0)
            status = 'late'
        elif pattern == 'undertime':
            minutes_early = rng.choice([15, 20, 30, 45, 60])
            hour, minute = divmod(17 * 60 - minutes_early, 60)
            time_out_pm = dtime(hour, minute, 0)
        elif pattern == 'overtime':
            extra_minutes = rng.choice([60, 90, 120, 150])
            hour, minute = divmod(17 * 60 + extra_minutes, 60)
            time_out_pm = dtime(hour % 24, minute, 0)

        total_hours = calculate_total_hours(time_in_am, time_out_am, time_in_pm, time_out_pm)
        late_minutes = calculate_late_minutes(time_in_am)
        undertime_minutes = calculate_undertime_minutes(time_out_pm)

        Attendance.objects.update_or_create(
            employee=employee, date=day,
            defaults=dict(
                time_in_am=time_in_am, time_out_am=time_out_am,
                time_in_pm=time_in_pm, time_out_pm=time_out_pm,
                total_hours=total_hours,
                late_minutes=late_minutes,
                undertime_minutes=undertime_minutes,
                status=status,
            ),
        )

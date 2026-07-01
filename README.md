# HRMS — Human Resource Management System

A Human Resource Management System built for **small companies (roughly
30–100 employees)**. It covers the core day-to-day HR loop: employee
records, attendance and time tracking, a configurable company calendar
(holidays/rest days), and payroll computation with per-employee
adjustments and payslip generation.

## Deployment model

This system is designed to be **deployed locally on a single machine or
local server within the company's own network** (e.g. an office LAN). It
is **not** intended to be exposed to the public internet. Only devices
connected to the same local network as the host machine can reach the
application — there is no built-in support for external/internet-facing
access, and none should be added without a proper review of
authentication, TLS, and hosting requirements first.

Typical setup: one PC or small server in the office runs the Django app;
HR staff and admins on the same office Wi-Fi/LAN open it in a browser
using the host machine's local IP address.

## Tech stack

- **Backend:** Django (Python)
- **Database:** relational (SQLite for local/small deployments, or
  PostgreSQL/MySQL if the company outgrows a single file)
- **Frontend:** server-rendered Django templates / forms, driven by the
  views behind each app below

## Modules (Django apps)

### `accounts` — Roles, permissions, leave policy, audit trail
- **Role / Permission / RolePermission** — a 3-tier admin matrix
  (`SuperAdmin`, `HRAdmin`, `StaffAdmin`), each mapped to specific
  permissions.
- **LeaveType** — configurable leave categories (Vacation, Sick, etc.),
  each with a paid flag and a max-days quota.
- **AuditLog** — records every create/update/delete on sensitive tables
  (who did what, old value vs. new value, timestamped), used for
  accountability and grading/traceability of admin actions.

### `employees` — Core HR records
- **Department / Position** — organizational structure; positions belong
  to a department and carry a reference base salary.
- **SalaryGrade** — pay structure keyed on `hourly_rate` (the primary
  input). `base_salary` is auto-computed (`hourly_rate × 8 hrs × 22
  days`) and kept only as a read-only reference value — actual payroll
  always multiplies `hourly_rate × hours_worked`.
- **Employee** — full 201-file style record: personal details
  (birthdate, gender, civil status, nationality), government IDs (TIN,
  SSS, PhilHealth, Pag-IBIG), emergency contact info, employment type
  (regular/contract) with contract start/end dates, and links to
  department/position/salary grade.
- **SystemUser** — login credentials + role, separate from Django's
  built-in `User`/`is_superuser`, so HR can grant app-level admin access
  without granting Django admin-site access.
- **LeaveBalance** — per-employee, per-leave-type running balance of
  remaining days.
- **CompanySettings** — single-row configuration (company name/logo,
  standard workday start/end, lunch window) used across attendance and
  payroll calculations.

### `attendance` — Time tracking
- **Attendance** — one row per employee per day, split into **AM and PM
  halves** (`time_in_am/time_out_am/time_in_pm/time_out_pm`) to support
  half-day/lunch-break UI beyond a single time-in/time-out pair.
  Computed fields: `total_hours`, `late_minutes`, `undertime_minutes`,
  and a `status` of present/absent/late.
- **OTP** — one-time codes used for verifying attendance actions (e.g.
  kiosk/terminal time-in).
- **AttendanceLog** — an audit trail of every time-in/time-out attempt
  (including failed attempts), with IP address and device info.
- **`attendance/utils.py`** — the single source of truth for computing
  hours worked, lateness, and undertime. These pure functions are
  overnight-shift-safe and accept both `time` objects and `"HH:MM"` /
  `"HH:MM:SS"` strings (matching what browser `<input type="time">`
  fields submit). Every other part of the system (views, payroll's
  hours lookup, admin manual edits) calls these same functions so hours
  are calculated consistently everywhere.

### `calendar_app` — Company calendar
- **Calendar** — per-date configuration of workday / regular holiday /
  special holiday / rest day, each with an `is_paid` flag and a
  `rate_multiplier` for holiday-pay computation. Unconfigured weekdays
  default to a normal workday; unconfigured weekends default to rest
  days.

### `payroll` — Pay computation
- **PayrollPeriod** — a pay cycle (start/end date) with a status of
  open/processing/closed.
- **Payroll** — one row per employee per payroll period: basic pay,
  gross pay, total deductions, net pay, plus a confirmation workflow
  (`is_confirmed`, `confirmed_by`, `confirmed_at`) separate from the
  draft/finalized status.
- **PayrollComponent / EmployeePayrollComponent** — reusable earning/
  deduction line items (fixed amount, percentage of a base, or custom
  formula), with optional per-employee overrides.
- **PayrollBreakdown** — the itemized components that made up a specific
  payroll run, for payslip display.
- **Adjustment** — manual one-off entries per payroll period: leave,
  overtime, flat deductions (e.g. cash advance), or flat allowances
  (e.g. transportation, bonus), each with an admin-editable display
  name.
- **Payslip** — the generated payslip file tied to a finalized payroll
  record.

## Seeding demo data

To try out attendance review and payroll computation without manually
entering records, a management command is included:

```
apps/employees/management/commands/seed_demo_data.py
```

It creates:
- 2 departments (Human Resources, Field Operations) with 5 positions
  between them
- 2 salary grades (Supervisory, Rank and File) with hourly/overtime
  rates
- **5 demo employees** spread across both departments (one on a
  contract with `contract_start`/`contract_end` set), with full
  personal-detail and government-ID fields filled in
- **A full month of weekday attendance for June 2026** per employee,
  with realistic variation: normal full days, occasional late arrivals,
  occasional early departures (undertime), occasional overtime days,
  and occasional absences — all computed with the same
  `attendance/utils.py` helpers the live system uses, so the numbers
  are consistent with how the app calculates hours everywhere else
- **One open PayrollPeriod** covering June 1–30, 2026

This command intentionally **does not run payroll** — it only prepares
the underlying employee/attendance data so payroll can be triggered and
verified manually afterward.

Run it with:

```bash
python manage.py seed_demo_data
```

Re-running it is safe (employees are matched on `employee_code`,
attendance on employee+date, so it updates rather than duplicates). To
wipe the demo employees and their attendance before reseeding:

```bash
python manage.py seed_demo_data --flush-demo
```

## Note on demo/sample data

Names, addresses, and government ID numbers seeded by the command above
are fictional placeholders for local testing only and do not correspond
to real people.

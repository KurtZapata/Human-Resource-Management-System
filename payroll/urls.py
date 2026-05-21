"""
payroll/urls.py  — Updated to include Webpage #7 Report routes
"""
from django.urls import path
from . import views
from . import views_report

app_name = 'payroll'

urlpatterns = [
    # Webpage #6 — Salary config
    path('components/',                     views.salary_components,          name='components'),
    path('components/create/',              views.create_component,           name='create_component'),
    path('components/<int:pk>/update/',     views.update_component,           name='update_component'),
    path('components/<int:pk>/delete/',     views.delete_component,           name='delete_component'),
    path('components/<int:pk>/json/',       views.get_component,              name='get_component'),
    path('components/reorder/',             views.reorder_components,         name='reorder_components'),
    path('components/toggle/',              views.toggle_component,           name='toggle_component'),

    # Payroll Period management
    path('periods/',                        views.payroll_periods,            name='periods'),
    path('periods/<int:pk>/close/',         views.close_period,               name='close_period'),

    # Payroll Run
    path('run/',                            views.run_payroll,                name='run'),

    # Payslips list
    path('payslips/',                       views.payslips_view,              name='payslips'),

    # Adjustments
    path('adjustments/',                    views.adjustments_view,           name='adjustments'),

    # Webpage #7 — Payroll Report & Payslip Printing
    path('report/',                         views_report.payroll_report,      name='report'),
    path('report/confirm/',                 views_report.confirm_payroll,     name='confirm_payroll'),
    path('report/<int:pk>/payslip-json/',   views_report.payslip_json,        name='payslip_json'),
    path('report/export/',                  views_report.export_payroll,      name='export_payroll'),
]

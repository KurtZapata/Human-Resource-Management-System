"""
attendance/urls.py
"""
from django.urls import path
from . import views

app_name = 'attendance'

urlpatterns = [
    # Webpage #2 — Public time-in/time-out station
    path('',                    views.timein_page,          name='timein'),
    path('log/',                views.log_attendance,       name='log_attendance'),
    path('otp/generate/',       views.generate_otp,         name='generate_otp'),

    # Webpage #4 — Admin attendance dashboard
    path('admin/',              views.attendance_dashboard, name='dashboard'),
    path('admin/records/',      views.attendance_records,   name='records'),
    path('admin/<int:pk>/update/', views.update_attendance, name='update'),
    path('admin/manual/',       views.manual_entry,         name='manual_entry'),
    path('admin/stats/',        views.employee_stats,       name='employee_stats'),
    path('admin/export/',       views.export_attendance,    name='export'),
    path('admin/audit/',        views.audit_log_view,       name='audit_log'),
    path('otp/',             views.otp_manager,      name='otp_manager'),
    path('otp/list-json/',   views.otp_list_json,    name='otp_list_json'),
    path('employee-login/',  views.employee_login,   name='employee_login'),
    path('employee-logout/', views.employee_logout,  name='employee_logout'),
    path('otp/stats-json/', views.otp_stats_json, name='otp_stats_json'),

]

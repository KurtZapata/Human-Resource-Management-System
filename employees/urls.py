from django.urls import path
from . import views

app_name = 'employees'

urlpatterns = [

    # ── Employee CRUD ──────────────────────────────────────────────────────────
    path('',
         views.employee_list,            name='list'),

    path('create/',
         views.employee_create,          name='create'),

    path('<int:pk>/',
         views.employee_detail,          name='detail'),

    path('<int:pk>/update/',
         views.employee_update,          name='update'),

    path('<int:pk>/delete/',
         views.employee_delete,          name='delete'),

    path('<int:pk>/json/',
         views.employee_get_json,        name='get_json'),

    path('export/',
         views.export_employees,         name='export'),

    # ── Dynamic dropdowns ─────────────────────────────────────────────────────
    path('positions-by-dept/',
         views.positions_by_department,  name='positions_by_dept'),

    # ── Auto-code AJAX ────────────────────────────────────────────────────────
    path('next-code/',
         views.next_employee_code,       name='next_code'),

    path('clear-credentials-session/',
         views.clear_credentials_session, name='clear_credentials_session'),

    # ── Departments ───────────────────────────────────────────────────────────
    path('departments/',
         views.departments_view,         name='departments'),

    # ── Positions ─────────────────────────────────────────────────────────────
    path('positions/',
         views.positions_view,           name='positions'),

    # ── Salary Grades ─────────────────────────────────────────────────────────
    path('salary-grades/',
         views.salary_grades_view,       name='salary_grades'),

    path('salary-grades/create/',
         views.create_grade,             name='create_grade'),

    path('salary-grades/<int:pk>/update/',
         views.update_grade,             name='update_grade'),

    path('salary-grades/<int:pk>/delete/',
         views.delete_grade,             name='delete_grade'),

    path('salary-grades/<int:pk>/json/',
         views.get_grade,                name='get_grade'),

    # ── System Users ──────────────────────────────────────────────────────────
    path('users/',
         views.users_view,              name='users'),

    # ── Roles & Permissions ───────────────────────────────────────────────────
    path('roles/',
         views.roles_view,              name='roles'),

    # ── Company Settings ──────────────────────────────────────────────────────
    path('settings/',
         views.company_settings,        name='company_settings'),
    
    path('<int:pk>/update-role/',
         views.update_employee_role,  name='update_employee_role'),

    path('<int:pk>/update-role/',
         views.update_employee_role, name='update_employee_role'),
]
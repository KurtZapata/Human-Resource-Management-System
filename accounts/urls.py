"""
accounts/urls.py
"""
from django.urls import path
from . import views

app_name = 'accounts'

urlpatterns = [
    path('',           views.login_view,            name='login'),
    path('login/',           views.login_view,            name='login'),
    path('logout/',          views.logout_view,            name='logout'),
    path('audit-log/',       views.audit_log_view,         name='audit_log'),
    path('password-reset/request/',  views.password_reset_request,  name='password_reset_request'),
    path('password-reset/verify/',   views.password_reset_verify,   name='password_reset_verify'),
    path('password-reset/confirm/',  views.password_reset_confirm,  name='password_reset_confirm'),
]

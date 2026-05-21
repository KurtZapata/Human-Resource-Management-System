"""
calendar_app/urls.py
"""
from django.urls import path
from . import views

app_name = 'calendar_app'

urlpatterns = [
    path('',             views.calendar_index,    name='index'),
    path('configure/',   views.configure_day,     name='configure_day'),
    path('remove/',      views.remove_day,        name='remove_day'),
    path('add-holiday/', views.add_holiday,       name='add_holiday'),
    path('day-info/',    views.get_day_info,       name='get_day_info'),
    path('api/month/',   views.calendar_api_month, name='api_month'),
]

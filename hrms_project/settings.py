"""
hrms/settings.py
Django project settings for the HRMS system.
"""

import os
import sys
from django.contrib.messages import constants as messages_constants
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# CRITICAL: Fixes project-wide bare cross-app imports (e.g., "from employees.models...")
# by placing the apps/ directory directly onto the Python system path.
sys.path.insert(0, os.path.join(BASE_DIR, 'apps'))

SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-change-this-in-production')

DEBUG = os.environ.get('DEBUG', 'True') == 'True'

ALLOWED_HOSTS = os.environ.get(
    'ALLOWED_HOSTS',
    'localhost,127.0.0.1,192.168.0.111'
).split(',')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # HRMS apps (Using bare names matching sys.path fix)
    'accounts',
    'employees',
    'attendance',
    'calendar_app',
    'payroll',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'accounts.access.AdminAccessMiddleware',      # Updated middleware path
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'hrms_project.urls'

TEMPLATES = [  
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'accounts.context_processors.user_role',   # Injected context processor
            ],
        },
    },
]

WSGI_APPLICATION = 'hrms_project.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME':   BASE_DIR / 'db.sqlite3',
        # For production, switch to PostgreSQL:
        # 'ENGINE': 'django.db.backends.postgresql',
        # 'NAME': os.environ.get('DB_NAME', 'hrms_db'),
        # 'USER': os.environ.get('DB_USER', 'hrms_user'),
        # 'PASSWORD': os.environ.get('DB_PASSWORD', ''),
        # 'HOST': os.environ.get('DB_HOST', 'localhost'),
        # 'PORT': os.environ.get('DB_PORT', '5432'),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE     = 'Asia/Manila'   # Philippines timezone
USE_I18N      = True
USE_TZ        = True

STATIC_URL  = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

MEDIA_URL  = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Auth redirects
LOGIN_URL          = '/login/'
LOGIN_REDIRECT_URL = '/employees/'
LOGOUT_REDIRECT_URL = '/login/'

# Email (configure for password reset)
EMAIL_BACKEND    = 'django.core.mail.backends.console.EmailBackend'  # dev: prints to console
DEFAULT_FROM_EMAIL = 'noreply@yourcompany.com'
# For production SMTP:
# EMAIL_BACKEND    = 'django.core.mail.backends.smtp.EmailBackend'
# EMAIL_HOST       = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
# EMAIL_PORT       = int(os.environ.get('EMAIL_PORT', 587))
# EMAIL_USE_TLS    = True
# EMAIL_HOST_USER  = os.environ.get('EMAIL_HOST_USER')
# EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD')

# Session
SESSION_COOKIE_AGE     = 28800   # 8 hours
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# Message tags (maps Django message levels to CSS toast classes)
MESSAGE_TAGS = {
    messages_constants.DEBUG:   'info',
    messages_constants.INFO:    'info',
    messages_constants.SUCCESS: 'success',
    messages_constants.WARNING: 'warning',
    messages_constants.ERROR:   'error',
}

# File upload size limit (5MB)
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
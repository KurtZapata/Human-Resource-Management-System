/* ============================================================
   HRMS Global JavaScript Utilities
   ============================================================ */

// ── Toast Notifications ──────────────────────────────────────
function showToast(message, type = 'info', duration = 3500) {
  let container = document.getElementById('toast-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'toast-container';
    container.className = 'toast-container';
    document.body.appendChild(container);
  }
  const icons = { success: '✓', error: '✕', info: 'ℹ', warning: '⚠' };
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `<span style="font-size:1.1rem">${icons[type] || icons.info}</span><span>${message}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.animation = 'toastIn .3s ease reverse';
    setTimeout(() => toast.remove(), 280);
  }, duration);
}

// ── Modal Helpers ─────────────────────────────────────────────
function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('open');
}
function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('open');
}
// Close modal on backdrop click
document.addEventListener('click', e => {
  if (e.target.classList.contains('modal-backdrop')) {
    e.target.classList.remove('open');
  }
});
// Close on Escape
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-backdrop.open').forEach(m => m.classList.remove('open'));
  }
});

// ── Logout Confirmation ───────────────────────────────────────
function confirmLogout() {
  openModal('logout-modal');
}
function doLogout() {
  // Django logout: submit the hidden logout form
  const form = document.getElementById('logout-form');
  if (form) form.submit();
  else window.location.href = '/accounts/logout/';
}

// ── Sidebar Toggle ────────────────────────────────────────────
function initSidebar() {
  const sidebar = document.getElementById('sidebar');
  const header = document.getElementById('admin-header');
  const content = document.getElementById('admin-content');
  const toggleBtn = document.getElementById('sidebar-toggle');
  if (!sidebar) return;

  const collapsed = localStorage.getItem('sidebar-collapsed') === 'true';
  if (collapsed) {
    sidebar.classList.add('collapsed');
    header?.classList.add('collapsed');
    content?.classList.add('collapsed');
  }

  toggleBtn?.addEventListener('click', () => {
    sidebar.classList.toggle('collapsed');
    header?.classList.toggle('collapsed');
    content?.classList.toggle('collapsed');
    localStorage.setItem('sidebar-collapsed', sidebar.classList.contains('collapsed'));
  });

  // Nav group toggles
  document.querySelectorAll('.nav-group-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const groupId = btn.dataset.group;
      const submenu = document.getElementById(groupId);
      if (!submenu) return;
      const isOpen = submenu.classList.contains('open');
      // Close all
      document.querySelectorAll('.nav-submenu.open').forEach(m => { m.classList.remove('open'); m.previousElementSibling?.classList.remove('open'); });
      document.querySelectorAll('.nav-group-toggle.open').forEach(b => b.classList.remove('open'));
      if (!isOpen) {
        submenu.classList.add('open');
        btn.classList.add('open');
      }
    });
  });

  // Mark active nav item
  const path = window.location.pathname;
  document.querySelectorAll('.nav-submenu a, .nav-link').forEach(link => {
    if (link.getAttribute('href') && path.startsWith(link.getAttribute('href')) && link.getAttribute('href') !== '/') {
      link.classList.add('active');
      const submenu = link.closest('.nav-submenu');
      if (submenu) {
        submenu.classList.add('open');
        submenu.previousElementSibling?.classList.add('open');
      }
    }
  });
}

// ── CSRF Token Helper (Django) ────────────────────────────────
function getCsrfToken() {
  const name = 'csrftoken';
  const cookies = document.cookie.split(';');
  for (let c of cookies) {
    const [k, v] = c.trim().split('=');
    if (k === name) return decodeURIComponent(v);
  }
  return '';
}

// ── Fetch wrapper with CSRF ───────────────────────────────────
async function apiPost(url, data) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() },
    body: JSON.stringify(data)
  });
  return res.json();
}

// ── Init on load ──────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initSidebar();
});

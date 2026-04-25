// feedback-button.js
//
// Drop-in floating feedback button. On submit, POSTs JSON to a configurable
// endpoint. The endpoint owns persistence — typically it appends to a
// project-local feedback.md.
//
// Usage:
//   import { initFeedback } from './feedback-button.js';
//   initFeedback({
//     endpoint: '/feedback',                   // required; relative or absolute URL
//     toolName: 'procurement-tracker',         // optional; sent in the body for tagging
//   });
//
// The POSTed body shape:
//   {
//     type: 'bug' | 'feature',
//     title: string,
//     description: string,
//     tool: string,           // toolName, or '' if not provided
//     url: string,            // window.location.href
//     userAgent: string,
//     timestamp: string,      // ISO 8601
//   }
//
// Returns { open, close, destroy }.

const STYLE = `
:host { all: initial; }

.launcher {
  position: fixed;
  bottom: 16px;
  right: 16px;
  z-index: 2147483647;
  background: #1f2937;
  color: #fff;
  border: none;
  border-radius: 999px;
  padding: 10px 16px;
  font: 500 13px/1 system-ui, -apple-system, Segoe UI, sans-serif;
  cursor: pointer;
  box-shadow: 0 2px 8px rgba(0,0,0,0.2);
}
.launcher:hover { background: #111827; }
.launcher:focus-visible { outline: 2px solid #2563eb; outline-offset: 2px; }

.overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.4);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 2147483647;
  font: 14px/1.4 system-ui, -apple-system, Segoe UI, sans-serif;
  color: #111827;
}
.overlay.open { display: flex; }

.modal {
  background: #fff;
  border-radius: 8px;
  padding: 20px;
  width: calc(100% - 32px);
  max-width: 480px;
  box-shadow: 0 8px 32px rgba(0,0,0,0.25);
}
.modal h2 {
  margin: 0 0 12px;
  font-size: 16px;
  font-weight: 600;
}

.row { margin-bottom: 12px; }
.row label {
  display: block;
  margin-bottom: 4px;
  font-size: 12px;
  font-weight: 500;
  color: #374151;
}
.row input,
.row textarea,
.row select {
  width: 100%;
  box-sizing: border-box;
  padding: 8px;
  border: 1px solid #d1d5db;
  border-radius: 4px;
  font: inherit;
  color: inherit;
  background: #fff;
}
.row textarea { min-height: 110px; resize: vertical; }
.row input:focus,
.row textarea:focus,
.row select:focus { outline: 2px solid #2563eb; outline-offset: -1px; border-color: #2563eb; }

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}
.actions button {
  padding: 8px 14px;
  border-radius: 4px;
  border: 1px solid #d1d5db;
  background: #fff;
  color: inherit;
  cursor: pointer;
  font: inherit;
}
.actions button.primary {
  background: #2563eb;
  color: #fff;
  border-color: #2563eb;
}
.actions button.primary:disabled { opacity: 0.6; cursor: not-allowed; }

.status {
  margin-top: 8px;
  font-size: 12px;
  color: #6b7280;
  min-height: 16px;
}
.status.error { color: #b91c1c; }
.status.success { color: #047857; }
`;

const HTML = `
<button class="launcher" type="button" aria-label="Send feedback">Feedback</button>
<div class="overlay" role="dialog" aria-modal="true" aria-labelledby="fb-heading">
  <form class="modal" novalidate>
    <h2 id="fb-heading">Send feedback</h2>
    <div class="row">
      <label for="fb-type">Type</label>
      <select id="fb-type" name="type">
        <option value="bug">Bug</option>
        <option value="feature">Feature request</option>
      </select>
    </div>
    <div class="row">
      <label for="fb-title">Title</label>
      <input id="fb-title" name="title" required maxlength="120" autocomplete="off" />
    </div>
    <div class="row">
      <label for="fb-desc">Description</label>
      <textarea id="fb-desc" name="description" required></textarea>
    </div>
    <div class="status" role="status" aria-live="polite"></div>
    <div class="actions">
      <button type="button" class="cancel">Cancel</button>
      <button type="submit" class="primary">Submit</button>
    </div>
  </form>
</div>
`;

export function initFeedback(config) {
  const { endpoint, toolName } = config || {};
  if (!endpoint) {
    throw new Error('initFeedback: endpoint is required');
  }

  const host = document.createElement('div');
  host.id = 'feedback-button-host';
  const root = host.attachShadow({ mode: 'open' });

  const styleEl = document.createElement('style');
  styleEl.textContent = STYLE;
  root.appendChild(styleEl);

  const wrap = document.createElement('div');
  wrap.innerHTML = HTML;
  root.appendChild(wrap);

  document.body.appendChild(host);

  const launcher = root.querySelector('.launcher');
  const overlay = root.querySelector('.overlay');
  const form = root.querySelector('.modal');
  const cancelBtn = root.querySelector('.cancel');
  const submitBtn = form.querySelector('button[type="submit"]');
  const status = root.querySelector('.status');
  const titleInput = root.getElementById('fb-title');

  const resetStatus = () => {
    status.textContent = '';
    status.className = 'status';
  };

  const open = () => {
    overlay.classList.add('open');
    setTimeout(() => titleInput.focus(), 0);
  };

  const close = () => {
    overlay.classList.remove('open');
    form.reset();
    resetStatus();
    submitBtn.disabled = false;
  };

  const onKeydown = (e) => {
    if (e.key === 'Escape' && overlay.classList.contains('open')) close();
  };

  launcher.addEventListener('click', open);
  cancelBtn.addEventListener('click', close);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  document.addEventListener('keydown', onKeydown);

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const data = new FormData(form);
    const type = String(data.get('type') || 'bug');
    const title = String(data.get('title') || '').trim();
    const description = String(data.get('description') || '').trim();
    if (!title || !description) {
      status.className = 'status error';
      status.textContent = 'Title and description are required.';
      return;
    }

    submitBtn.disabled = true;
    status.className = 'status';
    status.textContent = 'Submitting…';

    try {
      const payload = {
        type,
        title,
        description,
        tool: toolName || '',
        url: window.location.href,
        userAgent: navigator.userAgent,
        timestamp: new Date().toISOString(),
      };
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const text = await res.text().catch(() => '');
        throw new Error(`${res.status} ${text.slice(0, 200)}`.trim());
      }
      status.className = 'status success';
      status.textContent = 'Thanks — feedback saved.';
      setTimeout(close, 1500);
    } catch (err) {
      status.className = 'status error';
      status.textContent = err && err.message ? err.message : 'Failed to submit.';
      submitBtn.disabled = false;
    }
  });

  return {
    open,
    close,
    destroy: () => {
      document.removeEventListener('keydown', onKeydown);
      host.remove();
    },
  };
}

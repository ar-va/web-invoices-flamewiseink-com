// Copy buttons: <button data-copy="#inputId">
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('[data-copy]');
  if (!btn) return;
  const el = document.querySelector(btn.dataset.copy);
  if (!el) return;
  const text = el.value || el.textContent;
  try { await navigator.clipboard.writeText(text); } catch (_) { el.select && el.select(); document.execCommand('copy'); }
  const old = btn.textContent; btn.textContent = 'Copied ✓'; setTimeout(() => (btn.textContent = old), 1500);
});

function fmtMoney(cents) {
  const neg = cents < 0; cents = Math.abs(Math.round(cents));
  return (neg ? '-' : '') + '$' + (cents / 100).toLocaleString('en-CA', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function num(v) { const n = parseFloat(String(v || '').replace(/[$,\s]/g, '')); return isNaN(n) ? 0 : n; }

function initInvoiceForm() {
  const tbody = document.getElementById('items');
  const tpl = document.getElementById('row-template');
  const clientsEl = document.getElementById('clients-data');
  const clients = clientsEl ? JSON.parse(clientsEl.textContent || '{}') : {};

  function wireRow(tr) {
    const box = tr.querySelector('.tax-box'), hidden = tr.querySelector('input[name=item_taxable]');
    box.addEventListener('change', () => { hidden.value = box.checked ? '1' : '0'; recalc(); });
    tr.querySelector('.remove').addEventListener('click', () => {
      if (tbody.querySelectorAll('.item-row').length > 1) tr.remove();
      else tr.querySelectorAll('textarea,input[name=item_price]').forEach(i => (i.value = ''));
      recalc();
    });
    tr.querySelectorAll('input,textarea').forEach(i => i.addEventListener('input', recalc));
    const ta = tr.querySelector('textarea');
    const grow = () => { ta.style.height = 'auto'; ta.style.height = ta.scrollHeight + 'px'; };
    ta.addEventListener('input', grow); grow();
  }
  function addRow(item) {
    const tr = tpl.content.firstElementChild.cloneNode(true);
    tbody.appendChild(tr); wireRow(tr);
    if (item) {
      // reuse an empty row if there is one
      const empty = [...tbody.querySelectorAll('.item-row')].find(r => r !== tr &&
        !r.querySelector('textarea').value && !r.querySelector('input[name=item_price]').value);
      const target = empty || tr;
      if (empty) tr.remove();
      target.querySelector('textarea').value = item.desc;
      target.querySelector('input[name=item_qty]').value = item.qty || '1';
      target.querySelector('input[name=item_price]').value = item.price || '';
      const box = target.querySelector('.tax-box');
      box.checked = item.taxable !== '0';
      target.querySelector('input[name=item_taxable]').value = box.checked ? '1' : '0';
      target.querySelector('textarea').dispatchEvent(new Event('input'));
    } else {
      tr.querySelector('textarea').focus();
    }
    recalc();
  }
  function recalc() {
    let sub = 0, taxable = 0;
    tbody.querySelectorAll('.item-row').forEach(tr => {
      const q = tr.querySelector('input[name=item_qty]').value.trim() === '' ? 1 : num(tr.querySelector('input[name=item_qty]').value);
      const amt = Math.round(q * Math.round(num(tr.querySelector('input[name=item_price]').value) * 100));
      tr.querySelector('.amt').textContent = fmtMoney(amt);
      sub += amt; if (tr.querySelector('.tax-box').checked) taxable += amt;
    });
    const disc = Math.min(Math.max(0, Math.round(num(document.getElementById('discount').value) * 100)), sub);
    const rate = num(document.getElementById('tax_rate').value);
    const taxableAfter = sub ? taxable - disc * taxable / sub : 0;
    const tax = Math.round(taxableAfter * rate / 100);
    document.getElementById('t-sub').textContent = fmtMoney(sub);
    document.getElementById('t-tax').textContent = fmtMoney(tax);
    document.getElementById('t-tax-label').textContent = (document.getElementById('tax_name').value || 'Tax') + ' (' + rate + '%)';
    document.getElementById('t-total').textContent = fmtMoney(sub - disc + tax);
  }

  tbody.querySelectorAll('.item-row').forEach(wireRow);
  document.getElementById('add-item').addEventListener('click', () => addRow());
  const saved = document.getElementById('add-saved');
  if (saved) saved.addEventListener('change', () => {
    const o = saved.selectedOptions[0];
    if (o && o.dataset.desc !== undefined) addRow({ desc: o.dataset.desc, qty: o.dataset.qty, price: o.dataset.price, taxable: o.dataset.taxable });
    saved.value = '';
  });
  ['discount', 'tax_rate', 'tax_name'].forEach(id => document.getElementById(id).addEventListener('input', recalc));

  const name = document.getElementById('client_name');
  if (name) initClientPicker(name, clients);
  recalc();
}

function initHelcimPay() {
  const btn = document.getElementById('pay-card');
  if (!btn) return;
  const status = document.getElementById('pay-status');
  const label = btn.textContent;
  const say = (msg, cls) => { status.textContent = msg; status.className = 'small ' + (cls || ''); };
  const reset = () => { btn.disabled = false; btn.textContent = label; };

  btn.addEventListener('click', async () => {
    if (typeof appendHelcimPayIframe !== 'function') { say('Card checkout failed to load. Disable ad blockers or use e-Transfer.', 'danger'); return; }
    btn.disabled = true; btn.textContent = 'Opening secure checkout…'; say('');
    let checkoutToken;
    try {
      const r = await fetch(btn.dataset.start, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' }, body: '{}',
      });
      const j = await readJson(r);
      if (!r.ok) throw new Error(j.error || 'Could not start card payment.');
      checkoutToken = j.checkoutToken;
    } catch (err) { say(err.message, 'danger'); reset(); return; }

    const key = 'helcim-pay-js-' + checkoutToken;
    const onMessage = async (event) => {
      if (!event.data || event.data.eventName !== key) return;
      const st = event.data.eventStatus;
      if (st === 'ABORTED') {
        say('The card payment was declined. You can try another card or use e-Transfer.', 'danger');
      } else if (st === 'HIDE') {
        window.removeEventListener('message', onMessage); reset();
      } else if (st === 'SUCCESS') {
        window.removeEventListener('message', onMessage);
        try { removeHelcimPayIframe(); } catch (_) {}
        btn.textContent = 'Confirming payment…'; say('Please keep this page open.');
        try {
          const r = await fetch(btn.dataset.confirm, {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify({ checkoutToken, eventMessage: event.data.eventMessage }),
          });
          const j = await readJson(r);
          if (!r.ok) throw new Error(j.error || 'Could not confirm payment.');
          location.href = location.pathname + (j.pending ? '?pending=1' : '?paid=1');
        } catch (err) { say(err.message, 'danger'); btn.textContent = 'Payment sent'; }
      }
    };
    window.addEventListener('message', onMessage);
    appendHelcimPayIframe(checkoutToken, true);
  });
}

function initScheduleHint(locked) {
  const input = document.getElementById('start_date'), hint = document.getElementById('schedule-hint');
  if (!input || !hint || locked) return;
  const ord = n => n + ((n % 100 >= 11 && n % 100 <= 13) ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' }[n % 10] || 'th'));
  const show = () => {
    const d = new Date(input.value + 'T12:00:00');
    if (isNaN(d)) { hint.textContent = ''; return; }
    const day = d.getDate();
    const period = day === 1 ? 'the 1st to the end of each month' : `the ${ord(day)} to the ${ord(day - 1)} of each month`;
    hint.textContent = `Billing periods run from ${period}. Each invoice is created and emailed on the first day of its period.`;
  };
  input.addEventListener('input', show); show();
}

// Header: phone menu toggle; dropdowns close on outside click, Escape, or when another opens
(function () {
  const toggle = document.querySelector('.nav-toggle'), nav = document.getElementById('main-nav');
  if (toggle && nav) toggle.addEventListener('click', () => {
    const open = nav.classList.toggle('open');
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  const menus = [...document.querySelectorAll('details.menu')];
  menus.forEach(m => m.addEventListener('toggle', () => {
    if (m.open) menus.forEach(o => { if (o !== m) o.open = false; });
  }));
  document.addEventListener('click', e => menus.forEach(m => { if (m.open && !m.contains(e.target)) m.open = false; }));
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    menus.forEach(m => { if (m.open) { m.open = false; m.querySelector('summary').focus(); } });
  });
  // "+ New → Client / Saved item" jump to the add form and focus its first field
  if (location.hash === '#add') {
    const first = document.querySelector('#add input:not([type=hidden])');
    if (first) first.focus();
  }
})();

// e-Transfer dropdown: "+ Add a new email…" reveals a box to type it
document.querySelectorAll('.et-picker').forEach(p => {
  const sel = p.querySelector('.et-select'), box = p.querySelector('.et-new'), hint = p.querySelector('.et-new-hint');
  const input = box.querySelector('input');
  const sync = (focus) => {
    const adding = sel.value === '__new__';
    box.hidden = hint.hidden = !adding;
    input.required = adding;
    if (adding && focus) input.focus();
  };
  sel.addEventListener('change', () => sync(true));
  sync(false);
});

// Client picker: searchable list of saved clients (name, email, phone); fills the contact fields
function initClientPicker(input, clients) {
  const list = document.getElementById('client-options');
  const status = document.getElementById('client-status');
  const saveRow = document.getElementById('save-client-row');
  const all = Object.entries(clients).map(([n, c]) => ({ name: n, ...c }))
    .sort((a, b) => a.name.localeCompare(b.name));
  let shown = [], active = -1;
  const esc = t => String(t).replace(/[&<>"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
  const exact = () => all.find(c => c.name.toLowerCase() === input.value.trim().toLowerCase());

  function updateStatus() {
    const typed = input.value.trim(), match = exact();
    if (saveRow) saveRow.style.display = match || !typed ? 'none' : '';
    status.className = 'small combo-status' + (match ? ' saved' : '');
    status.textContent = match ? '✓ Saved client: details filled in from the client list'
      : typed ? 'New client' : '';
  }
  function fill(c) {
    input.value = c.name;
    [['client_email', c.email], ['client_phone', c.phone], ['client_address', c.address]].forEach(([id, v]) => {
      document.getElementById(id).value = v || '';
    });
    close(); updateStatus();
  }
  function render() {
    const q = input.value.trim().toLowerCase();
    const digits = q.replace(/\D/g, '');
    shown = all.filter(c => !q || [c.name, c.email].some(v => (v || '').toLowerCase().includes(q))
      || (digits.length >= 3 && (c.phone || '').replace(/\D/g, '').includes(digits))).slice(0, 8);
    const rows = shown.map((c, i) => `<li role="option" id="client-opt-${i}" data-i="${i}" aria-selected="${i === active}">
        <span class="name">${esc(c.name)}</span><span class="meta">${esc([c.email, c.phone].filter(Boolean).join(' · ') || 'No contact details yet')}</span></li>`);
    if (q && /\p{L}/u.test(q) && !exact()) rows.push(`<li role="option" class="new" data-new="1" id="client-opt-new" aria-selected="${active === shown.length}">+ Add “${esc(input.value.trim())}” as a new client</li>`);
    if (!rows.length) rows.push('<li class="empty">No saved clients yet. Type a name to add one.</li>');
    list.innerHTML = rows.join('');
    list.hidden = false; input.setAttribute('aria-expanded', 'true');
    const cur = list.querySelector('[aria-selected="true"]');
    input.setAttribute('aria-activedescendant', cur ? cur.id : '');
    if (cur) cur.scrollIntoView({ block: 'nearest' });
  }
  function close() { list.hidden = true; active = -1; input.setAttribute('aria-expanded', 'false'); }
  function choose(li) {
    if (!li) return;
    if (li.dataset.new) { close(); updateStatus(); document.getElementById('client_email').focus(); return; }
    if (li.dataset.i !== undefined) fill(shown[+li.dataset.i]);
  }

  input.addEventListener('focus', () => { active = -1; render(); });
  input.addEventListener('input', () => { active = -1; render(); updateStatus(); });
  input.addEventListener('keydown', e => {
    const count = list.querySelectorAll('li[role=option]').length;
    if (e.key === 'ArrowDown') { e.preventDefault(); if (list.hidden) render(); active = Math.min(active + 1, count - 1); render(); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(active - 1, 0); render(); }
    else if (e.key === 'Enter' && !list.hidden && active >= 0) { e.preventDefault(); choose(list.querySelectorAll('li[role=option]')[active]); }
    else if (e.key === 'Escape') close();
    else if (e.key === 'Tab') { const m = exact(); if (m && !document.getElementById('client_email').value) fill(m); close(); }
  });
  list.addEventListener('mousedown', e => { e.preventDefault(); choose(e.target.closest('li')); });
  input.addEventListener('blur', () => setTimeout(close, 100));
  updateStatus();
}

// Read a JSON reply; if something in between (e.g. Cloudflare) answered with a web page, explain that instead
async function readJson(r) {
  const text = await r.text();
  try { return JSON.parse(text); } catch (_) {}
  const title = (text.match(/<title>([^<]*)<\/title>/i) || [])[1] || '';
  const viaCloudflare = /cloudflare|cf-ray|challenge-platform/i.test(text);
  console.error('Card payment: non-JSON reply', r.status, title, text.slice(0, 500));
  throw new Error(viaCloudflare
    ? `Cloudflare stopped the card payment request (HTTP ${r.status}${title ? ': ' + title.trim() : ''}). Please use e-Transfer for now.`
    : `The server answered with a web page instead of the payment details (HTTP ${r.status}${title ? ': ' + title.trim() : ''}). Please use e-Transfer for now.`);
}

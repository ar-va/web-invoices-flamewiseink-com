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
  const saveRow = document.getElementById('save-client-row');
  if (name) {
    const sync = (fill) => {
      const c = clients[name.value.trim()] ||
        Object.entries(clients).find(([k]) => k.toLowerCase() === name.value.trim().toLowerCase())?.[1];
      if (saveRow) saveRow.style.display = c || !name.value.trim() ? 'none' : '';
      if (!c || !fill) return;
      [['client_email', c.email], ['client_phone', c.phone], ['client_address', c.address]].forEach(([id, v]) => {
        document.getElementById(id).value = v || '';
      });
    };
    name.addEventListener('change', () => sync(true));
    name.addEventListener('input', () => sync(false));
    sync(false);
  }
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
      const r = await fetch(btn.dataset.start, { method: 'POST' });
      const j = await r.json();
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
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ checkoutToken, eventMessage: event.data.eventMessage }),
          });
          const j = await r.json();
          if (!r.ok) throw new Error(j.error || 'Could not confirm payment.');
          location.href = location.pathname + '?paid=1';
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

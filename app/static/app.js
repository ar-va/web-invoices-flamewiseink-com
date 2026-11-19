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
  const clients = JSON.parse(document.getElementById('clients-data').textContent || '{}');

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
  function addRow(desc, price) {
    const tr = tpl.content.firstElementChild.cloneNode(true);
    tbody.appendChild(tr); wireRow(tr);
    if (desc !== undefined) {
      const empty = [...tbody.querySelectorAll('.item-row')].find(r => !r.querySelector('textarea').value && !r.querySelector('input[name=item_price]').value && r !== tr);
      const target = empty || tr;
      if (empty) tr.remove();
      target.querySelector('textarea').value = desc;
      target.querySelector('input[name=item_price]').value = price || '';
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
  document.querySelectorAll('.chip[data-desc]').forEach(c => c.addEventListener('click', () => addRow(c.dataset.desc, c.dataset.price)));
  ['discount', 'tax_rate', 'tax_name'].forEach(id => document.getElementById(id).addEventListener('input', recalc));

  const name = document.getElementById('client_name');
  name.addEventListener('change', () => {
    const c = clients[name.value]; if (!c) return;
    [['client_email', c.email], ['client_phone', c.phone], ['client_address', c.address]].forEach(([id, v]) => {
      const el = document.getElementById(id); if (!el.value) el.value = v;
    });
  });
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

import json, os, re, sys, tempfile, hashlib
from unittest import mock

tmp = tempfile.mkdtemp()
os.environ.update(DATA_DIR=tmp, ADMIN_EMAIL="admin@x.ca", ADMIN_PASSWORD="supersecret1",
                  HELCIM_API_TOKEN="test-token", BASE_URL="https://invoices.example.ca", TZ="America/Toronto")
sys.path.insert(0, sys.argv[1])
import app as A  # noqa

c = A.app.test_client()


def csrf(html):
    m = re.search(r'name="_csrf" value="([^"]+)"', html)
    return m.group(1)


def get(path, code=200):
    r = c.get(path)
    assert r.status_code == code, (path, r.status_code, r.data[:500])
    return r.get_data(as_text=True)


def post(path, data, code=302, page="/"):
    data = dict(data)
    data.setdefault("_csrf", TOK)
    r = c.post(path, data=data)
    assert r.status_code == code, (path, r.status_code, r.get_data(as_text=True)[:1500])
    return r


# login
h = get("/login")
TOK = csrf(h)
r = c.post("/login", data={"_csrf": TOK, "email": "admin@x.ca", "password": "nope"})
assert "Wrong email" in r.get_data(as_text=True)
r = c.post("/login", data={"_csrf": TOK, "email": "admin@x.ca", "password": "supersecret1"})
assert r.status_code == 302
TOK = csrf(get("/"))

# CSRF enforced
assert c.post("/invoices/new", data={}).status_code == 400

# settings
s = get("/settings")
post("/settings", {"business_name": "Flame Wise Tattoo Collective", "business_address": "219 Dufferin St",
                   "business_email": "ads@flamewiseink.ca", "accent_color": "#d9480f", "invoice_title": "Invoice",
                   "invoice_prefix": "FW-", "next_number": "1001", "default_due_days": "14", "tax_name": "HST",
                   "tax_rate": "13", "currency": "CAD", "etransfer_enabled": "on", "etransfer_email": "ads@flamewiseink.ca",
                   "etransfer_instructions": A.DEFAULTS["etransfer_instructions"], "helcim_enabled": "on",
                   "quick_items": "Tattoo session (hourly) | 150.00\nDeposit | 100", "email_subject": A.DEFAULTS["email_subject"],
                   "email_body": A.DEFAULTS["email_body"], "show_artist": "on", "tax_number_label": "HST #", "tax_number": "123"})

# new invoice form
post("/items", {"name": "Tattoo session (hourly)", "description": "Tattoo session", "qty": "1", "price": "150", "taxable": "on"})
h = get("/invoices/new")
assert "Tattoo session (hourly)" in h and 'data-price="150.00"' in h
# validation error
r = post("/invoices/new", {"client_name": "", "issue_date": "2026-11-19", "due_date": "2026-12-03", "tax_rate": "13"}, code=400)
# create
r = post("/invoices/new", {"client_name": "Jane Doe", "client_email": "jane@example.com", "issue_date": "2026-11-19",
                            "due_date": "2026-12-03", "tax_name": "HST", "tax_rate": "13", "discount": "10",
                            "item_desc": ["Tattoo session", "Aftercare kit"], "item_qty": ["2.5", "1"],
                            "item_price": ["150", "20.00"], "item_taxable": ["1", "0"], "notes": "Thanks!"})
loc = r.headers["Location"]
iid = int(loc.rstrip("/").split("/")[-1])
h = get(loc)
# subtotal 375+20=395; disc 10; taxable after = 375 - 10*375/395 = 365.5063; tax=47.52; total 395-10+47.52=432.52
assert "FW-1001" in h and "$432.52" in h, h[:3000]
with A.app.app_context():
    row = A.q("SELECT * FROM invoices WHERE id=?", (iid,), one=True)
    assert row["total_cents"] == 43252 and row["tax_cents"] == 4752, dict(row)
    token = row["token"]

# public draft hidden from anonymous
anon = A.app.test_client()
_d = anon.get(f"/i/{token}")
assert _d.status_code == 200 and "being updated" in _d.get_data(as_text=True)
assert "Jane Doe" not in _d.get_data(as_text=True) and "432.52" not in _d.get_data(as_text=True)
# preview works for logged in
assert "Preview" in get(f"/i/{token}")

# mark sent
post(f"/invoices/{iid}/status", {"action": "sent"})
ph = anon.get(f"/i/{token}").get_data(as_text=True)
assert "ads@flamewiseink.ca" in ph and "Pay $432.52 by card" in ph and "FW-1001" in ph, ph[:2000]
assert "secure.helcim.app/helcim-pay/services/start.js" in ph

# partial e-transfer payment
post(f"/invoices/{iid}/payments", {"amount": "100", "method": "e-Transfer", "paid_on": "2026-11-19", "reference": "CAabc"})
with A.app.app_context():
    row = A.q("SELECT * FROM invoices WHERE id=?", (iid,), one=True)
    assert row["status"] == "partial" and row["paid_cents"] == 10000

# Helcim flow (mocked)
class R:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.ok = code < 300
        self.content = json.dumps(body).encode()
        self.text = json.dumps(body)
    def json(self):
        return self._b

calls = {}
def fake_post(url, json=None, headers=None, timeout=None):
    calls["init"] = (url, json, headers)
    return R(200, {"checkoutToken": "ct123", "secretToken": "st456"})

def fake_get(url, headers=None, timeout=None):
    calls["get"] = url
    return R(200, {"transactionId": 999, "status": "APPROVED", "amount": 332.52, "type": "purchase", "approvalCode": "T1"})

with mock.patch.object(A.requests, "post", fake_post), mock.patch.object(A.requests, "get", fake_get):
    r = anon.post(f"/i/{token}/helcim/start")
    assert r.status_code == 200 and r.get_json()["checkoutToken"] == "ct123", r.data
    assert calls["init"][1]["amount"] == 332.52 and calls["init"][2]["api-token"] == "test-token"
    msg = json.dumps({"data": {"data": {"transactionId": "999", "status": "APPROVED", "amount": "332.52"}, "hash": "x"}})
    r = anon.post(f"/i/{token}/helcim/confirm", json={"checkoutToken": "ct123", "eventMessage": msg})
    assert r.status_code == 200, r.data
    assert calls["get"].endswith("/card-transactions/999")
    # replay is harmless
    r = anon.post(f"/i/{token}/helcim/confirm", json={"checkoutToken": "ct123", "eventMessage": msg})
    assert r.status_code == 200

with A.app.app_context():
    row = A.q("SELECT * FROM invoices WHERE id=?", (iid,), one=True)
    assert row["status"] == "paid" and row["paid_cents"] == 43252, dict(row)
    assert len(A.q("SELECT * FROM payments WHERE invoice_id=?", (iid,))) == 2
ph = anon.get(f"/i/{token}?paid=1").get_data(as_text=True)
assert "Payment received" in ph and "Paid in full" in ph and "Pay $" not in ph

# hash-only fallback when API lookup fails
txn = {"transactionId": "1001", "status": "APPROVED", "amount": "5.00"}
post("/invoices/new", {"client_name": "Bob", "issue_date": "2026-11-19", "due_date": "2026-12-03", "tax_rate": "0",
                       "item_desc": ["Touch-up"], "item_qty": ["1"], "item_price": ["5"], "item_taxable": ["1"]})
with A.app.app_context():
    b = A.q("SELECT * FROM invoices WHERE client_name='Bob'", one=True)
post(f"/invoices/{b['id']}/status", {"action": "sent"})
def fake_post2(url, json=None, headers=None, timeout=None):
    return R(200, {"checkoutToken": "ct2", "secretToken": "sec2"})
def fake_get_fail(url, headers=None, timeout=None):
    return R(401, {"errors": "nope"})
h_ = hashlib.sha256((json.dumps(txn, separators=(",", ":")) + "sec2").encode()).hexdigest()
with mock.patch.object(A.requests, "post", fake_post2), mock.patch.object(A.requests, "get", fake_get_fail):
    anon.post(f"/i/{b['token']}/helcim/start")
    bad = anon.post(f"/i/{b['token']}/helcim/confirm", json={"checkoutToken": "ct2", "eventMessage": {"data": txn, "hash": "wrong"}})
    assert bad.status_code == 400
    good = anon.post(f"/i/{b['token']}/helcim/confirm", json={"checkoutToken": "ct2", "eventMessage": json.dumps({"data": txn, "hash": h_})})
    assert good.status_code == 200, good.data
with A.app.app_context():
    assert A.q("SELECT status FROM invoices WHERE id=?", (b["id"],), one=True)["status"] == "paid"

# advertising account sees only own
post("/users", {"name": "Artie", "email": "artie@x.ca", "role": "advertising", "password": "artistpass1"})
ac = A.app.test_client()
t2 = csrf(ac.get("/login").get_data(as_text=True))
assert ac.post("/login", data={"_csrf": t2, "email": "artie@x.ca", "password": "artistpass1"}).status_code == 302
d = ac.get("/").get_data(as_text=True)
assert "FW-1001" not in d
assert ac.get(f"/invoices/{iid}").status_code == 403
assert ac.get("/settings").status_code == 403
t2 = csrf(ac.get("/invoices/new").get_data(as_text=True))
r = ac.post("/invoices/new", data={"_csrf": t2, "client_name": "Jane Doe", "issue_date": "2026-11-19", "due_date": "2026-12-03",
                                   "tax_rate": "13", "item_desc": ["Flash piece"], "item_qty": ["1"], "item_price": ["200"],
                                   "item_taxable": ["1"]})
assert r.status_code == 302
assert "Flash piece" in ac.get(r.headers["Location"]).get_data(as_text=True)

# other pages render
for p in ["/", "/?f=all", "/?f=overdue", "/?q=Jane&f=all", "/users", "/account", "/export.csv", f"/invoices/{iid}/edit"]:
    get(p)
post(f"/invoices/{iid}/duplicate", {})
print("ALL TESTS PASSED")
open(os.path.join(sys.argv[2], "view.html"), "w").write(get(f"/invoices/{iid}"))
open(os.path.join(sys.argv[2], "public.html"), "w").write(anon.get(f"/i/{b['token']}").get_data(as_text=True))
open(os.path.join(sys.argv[2], "form.html"), "w").write(get("/invoices/new"))
open(os.path.join(sys.argv[2], "dash.html"), "w").write(get("/"))

# --- per-invoice e-Transfer email ---
post("/settings", {"business_name": "Flame Wise Tattoo Collective", "accent_color": "#d9480f", "invoice_title": "Invoice",
                   "invoice_prefix": "FW-", "next_number": "2000", "default_due_days": "14", "tax_name": "HST",
                   "tax_rate": "13", "currency": "CAD", "etransfer_enabled": "on", "etransfer_email": "ads@flamewiseink.ca",
                   "etransfer_emails": "artie@flamewiseink.ca\nads@flamewiseink.ca",
                   "etransfer_instructions": A.DEFAULTS["etransfer_instructions"], "helcim_enabled": "on",
                   "email_subject": A.DEFAULTS["email_subject"], "email_body": A.DEFAULTS["email_body"]})
f = get("/invoices/new")
import re as _re
_sel = f[f.index('<select name="etransfer_email" class="et-select">'):]
_sel = _sel[:_sel.index('</select>')]
assert _sel.count('value="artie@flamewiseink.ca"') == 1 and _sel.count('value="ads@flamewiseink.ca"') == 1, _sel
assert _re.search(r'value="ads@flamewiseink.ca"\s+selected>ads@flamewiseink.ca \(default\)', _sel), _sel
post("/invoices/new", {"client_name": "Carla", "issue_date": "2026-11-19", "due_date": "2026-12-03", "tax_rate": "0",
                       "etransfer_email": "bad-email", "item_desc": ["X"], "item_qty": ["1"], "item_price": ["5"], "item_taxable": ["1"]}, code=400)
r = post("/invoices/new", {"client_name": "Carla", "issue_date": "2026-11-19", "due_date": "2026-12-03", "tax_rate": "0",
                           "etransfer_email": "artie@flamewiseink.ca", "item_desc": ["X"], "item_qty": ["1"], "item_price": ["5"], "item_taxable": ["1"]})
cid = int(r.headers["Location"].split("/")[-1])
post(f"/invoices/{cid}/status", {"action": "sent"})
with A.app.app_context():
    ct = A.q("SELECT token FROM invoices WHERE id=?", (cid,), one=True)["token"]
pub = anon.get(f"/i/{ct}").get_data(as_text=True)
assert 'value="artie@flamewiseink.ca" id="et-email"' in pub and "to artie@flamewiseink.ca" in pub, pub[:3000]
assert "artie@flamewiseink.ca" in get(f"/invoices/{cid}")
assert _re.search(r'value="artie@flamewiseink.ca"\s+selected>', get(f"/invoices/{cid}/edit"))
# duplicate keeps it
r = post(f"/invoices/{cid}/duplicate", {})
assert _re.search(r'value="artie@flamewiseink.ca"\s+selected>', get(r.headers["Location"]))
# blank -> default
post(f"/invoices/{cid}/edit", {"client_name": "Carla", "issue_date": "2026-11-19", "due_date": "2026-12-03", "tax_rate": "0",
                               "etransfer_email": "", "item_desc": ["X"], "item_qty": ["1"], "item_price": ["5"], "item_taxable": ["1"]})
pub = anon.get(f"/i/{ct}").get_data(as_text=True)
assert 'value="ads@flamewiseink.ca" id="et-email"' in pub
print("ETRANSFER TESTS PASSED")

# --- Studio Manager: all invoices, no settings/accounts ---
post("/users", {"name": "Mona", "email": "mona@x.ca", "role": "manager", "password": "managerpass1"})
mc = A.app.test_client()
t3 = csrf(mc.get("/login").get_data(as_text=True))
assert mc.post("/login", data={"_csrf": t3, "email": "mona@x.ca", "password": "managerpass1"}).status_code == 302
md = mc.get("/?f=all").get_data(as_text=True)
assert "FW-1001" in md and "Export CSV" in md and ">By<" in md
assert mc.get(f"/invoices/{iid}").status_code == 200
assert mc.get("/settings").status_code == 403 and mc.get("/users").status_code == 403
assert mc.get("/export.csv").status_code == 200
assert "Settings</a>" not in md and "Accounts</a>" not in md
# advertising: no CSV, no By column
ad = ac.get("/").get_data(as_text=True)
assert "Export CSV" not in ad and ">By<" not in ad and ac.get("/export.csv").status_code == 403
# role labels on accounts page
up = get("/users")
assert "Studio Manager" in up and "Advertising" in up and ">Staff<" not in up
# invoice shows creator label
with A.app.app_context():
    A.save_settings({"show_artist": "1"})
assert "Prepared by" in get(f"/invoices/{iid}")
# old role names are migrated on start
with A.app.app_context():
    A.x("UPDATE users SET role='staff' WHERE email='mona@x.ca'")
    A.x("UPDATE users SET role='artist' WHERE email='artie@x.ca'")
A.bootstrap()
with A.app.app_context():
    roles = {r["email"]: r["role"] for r in A.q("SELECT email, role FROM users")}
assert roles["mona@x.ca"] == "manager" and roles["artie@x.ca"] == "advertising", roles
print("ROLE TESTS PASSED")

# --- Invoice lifecycle rules, end to end ---
def new_invoice(client, tok, name, price="100", tax="0"):
    r = client.post("/invoices/new", data={"_csrf": tok, "client_name": name, "client_email": f"{name.lower()}@x.ca",
                                           "issue_date": "2026-11-19", "due_date": "2026-12-03", "tax_rate": tax,
                                           "item_desc": ["Session"], "item_qty": ["1"], "item_price": [price],
                                           "item_taxable": ["1"]})
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    return int(r.headers["Location"].split("/")[-1])

def row(iid):
    with A.app.app_context():
        return dict(A.q("SELECT * FROM invoices WHERE id=?", (iid,), one=True))

def events(iid):
    with A.app.app_context():
        return [e["text"] for e in A.q("SELECT text FROM events WHERE invoice_id=? ORDER BY id", (iid,))]

def flashed(client, iid):
    import html as _html
    return _html.unescape(client.get(f"/invoices/{iid}").get_data(as_text=True))

TOK = csrf(get("/"))
L1 = new_invoice(c, TOK, "Lena")
post(f"/invoices/{L1}/status", {"action": "sent"})
# Q3 overpayment refused
post(f"/invoices/{L1}/payments", {"amount": "150", "method": "e-Transfer"})
assert row(L1)["paid_cents"] == 0 and "more than the $100.00 still owed" in flashed(c, L1)
post(f"/invoices/{L1}/payments", {"amount": "40", "method": "e-Transfer"})
assert row(L1)["status"] == "partial" and row(L1)["paid_cents"] == 4000
# Q1 void with payments refused
post(f"/invoices/{L1}/status", {"action": "void"})
assert row(L1)["status"] == "partial" and "can't be voided" in flashed(c, L1)
# Q6 reopen refused once paid
post(f"/invoices/{L1}/status", {"action": "reopen"})
assert row(L1)["status"] == "partial"
# Q7 edit below money received refused; edit up keeps partial
post(f"/invoices/{L1}/edit", {"client_name": "Lena", "issue_date": "2026-11-19", "due_date": "2026-12-03", "tax_rate": "0",
                              "item_desc": ["Session"], "item_qty": ["1"], "item_price": ["30"], "item_taxable": ["1"]}, code=400)
post(f"/invoices/{L1}/edit", {"client_name": "Lena", "issue_date": "2026-11-19", "due_date": "2026-12-03", "tax_rate": "0",
                              "item_desc": ["Session"], "item_qty": ["1"], "item_price": ["40"], "item_taxable": ["1"]})
assert row(L1)["status"] == "paid" and row(L1)["paid_at"]
assert any("total $100.00 → $40.00" in e for e in events(L1)), events(L1)
assert "Paid in full" in events(L1)
# Q8 Advertising can't remove payments (manager can)
with A.app.app_context():
    pid = A.q("SELECT id FROM payments WHERE invoice_id=?", (L1,), one=True)["id"]
# Q10 admin can't delete a paid invoice
post(f"/invoices/{L1}/delete", {})
assert row(L1)["status"] == "paid"
# Manager removes the payment -> sent
t3 = csrf(mc.get("/").get_data(as_text=True))
assert mc.post(f"/invoices/{L1}/payments/{pid}/delete", data={"_csrf": t3}).status_code == 302
assert row(L1)["status"] == "sent" and row(L1)["paid_cents"] == 0 and row(L1)["paid_at"] is None
# now void is allowed, then payments refused on void
post(f"/invoices/{L1}/status", {"action": "void"})
assert row(L1)["status"] == "void"
post(f"/invoices/{L1}/payments", {"amount": "10", "method": "Cash"})
assert row(L1)["paid_cents"] == 0
# void -> back to draft -> admin may delete draft
post(f"/invoices/{L1}/status", {"action": "reopen"})
assert row(L1)["status"] == "draft"

# Advertising: own invoice, can record payment but not remove it, can't delete
t2 = csrf(ac.get("/").get_data(as_text=True))
A1 = new_invoice(ac, t2, "Adele")
assert ac.post(f"/invoices/{A1}/payments", data={"_csrf": t2, "amount": "25", "method": "e-Transfer"}).status_code == 302
assert row(A1)["status"] == "partial" and row(A1)["sent_at"]              # Q5 draft + payment -> sent
with A.app.app_context():
    apid = A.q("SELECT id FROM payments WHERE invoice_id=?", (A1,), one=True)["id"]
ac.post(f"/invoices/{A1}/payments/{apid}/delete", data={"_csrf": t2})
assert row(A1)["paid_cents"] == 2500
_ap = ac.get(f"/invoices/{A1}").get_data(as_text=True)
assert f"/payments/{apid}/delete" not in _ap and "Delete permanently" not in _ap and "Void invoice" not in _ap
ac.post(f"/invoices/{A1}/delete", data={"_csrf": t2})
assert row(A1)["status"] == "partial"
# shared client list: a client added by the admin is offered to Advertising too
post("/clients", {"name": "Lena", "email": "lena@x.ca", "phone": "416-555-0100"})
form = ac.get("/invoices/new").get_data(as_text=True)
assert '"Lena": {"address": "", "email": "lena@x.ca"' in form or '"lena@x.ca"' in form, form[-800:]

# Q4 $0 invoice becomes paid when sent
Z = new_invoice(c, TOK, "Zero", price="0")
post(f"/invoices/{Z}/status", {"action": "sent"})
assert row(Z)["status"] == "paid"

# Admin deletes a draft
D = new_invoice(c, TOK, "Dora")
post(f"/invoices/{D}/delete", {})
with A.app.app_context():
    assert A.q("SELECT 1 FROM invoices WHERE id=?", (D,), one=True) is None

# Q9 overdue starts the day after the due date
with A.app.app_context():
    import datetime as _dt
    r_ = A.q("SELECT * FROM invoices WHERE id=?", (A1,), one=True)
    d = dict(r_); d["due_date"] = A.today().isoformat()
    assert A.L.display_status(A.L.Invoice.from_row(d), A.today()) == "partial"
    d["due_date"] = (A.today() - _dt.timedelta(days=1)).isoformat()
    assert A.L.display_status(A.L.Invoice.from_row(d), A.today()) == "overdue"

# Helcim: charge smaller than checkout is rejected; voided-while-paying is recorded and flagged
H = new_invoice(c, TOK, "Hana", price="80")
post(f"/invoices/{H}/status", {"action": "sent"})
Htok = row(H)["token"]
def hpost(path, **kw):
    return anon.post(path, **kw)
def init_ok(url, json=None, headers=None, timeout=None):
    return R(200, {"checkoutToken": "ctH" + str(len(calls)), "secretToken": "s"})
with mock.patch.object(A.requests, "post", init_ok), \
     mock.patch.object(A.requests, "get", lambda url, headers=None, timeout=None: R(200, {"transactionId": 7001, "status": "APPROVED", "amount": "5.00", "dateCreated": "2026-11-19 10:00:00"})):
    ct = hpost(f"/i/{Htok}/helcim/start").get_json()["checkoutToken"]
    r = hpost(f"/i/{Htok}/helcim/confirm", json={"checkoutToken": ct, "eventMessage": {"data": {"transactionId": 7001}}})
    assert r.status_code == 400 and row(H)["paid_cents"] == 0
    assert any("claim rejected" in e for e in events(H))
calls["n"] = 1
with mock.patch.object(A.requests, "post", init_ok), \
     mock.patch.object(A.requests, "get", lambda url, headers=None, timeout=None: R(200, {"transactionId": 7002, "status": "APPROVED", "amount": "80.00"})):
    ct = hpost(f"/i/{Htok}/helcim/start").get_json()["checkoutToken"]
    post(f"/invoices/{H}/status", {"action": "void"})                       # staff voids while client pays
    assert row(H)["status"] == "void"
    r = hpost(f"/i/{Htok}/helcim/confirm", json={"checkoutToken": ct, "eventMessage": {"data": {"transactionId": 7002}}})
    assert r.status_code == 200, r.data
assert row(H)["status"] == "paid" and row(H)["paid_cents"] == 8000
assert any("may need a refund (invoice was void)" in e for e in events(H)), events(H)
print("LIFECYCLE E2E TESTS PASSED")

# --- contact email shown to clients ---
assert A.DEFAULTS["business_email"] == "team@flamewise.ca"
with A.app.app_context():
    A.save_settings({"business_email": "team@flamewise.ca"})
_pp = anon.get(f"/i/{Htok}").get_data(as_text=True)
assert 'Questions? <a href="mailto:team@flamewise.ca">' in _pp
print("CONTACT EMAIL TESTS PASSED")

# ===================== Clients, saved items, e-Transfer dropdown, monthly billing =====================
import datetime as _dt
TOK = csrf(get("/"))

# -- client list: new name on an invoice is added when "save_client" is ticked
r = c.post("/invoices/new", data={"_csrf": TOK, "client_name": "Mira Studio", "client_email": "mira@x.ca",
                                  "issue_date": "2026-11-19", "due_date": "2026-12-03", "tax_rate": "13",
                                  "save_client": "1", "item_desc": ["Ad"], "item_qty": ["1"], "item_price": ["50"],
                                  "item_taxable": ["1"]})
assert r.status_code == 302
with A.app.app_context():
    mira = A.q("SELECT * FROM clients WHERE name='Mira Studio'", one=True)
    assert mira and mira["email"] == "mira@x.ca"
    assert A.q("SELECT client_id FROM invoices WHERE client_name='Mira Studio'", one=True)["client_id"] == mira["id"]
# prefill from client page link
assert 'value="mira@x.ca"' in get(f"/invoices/new?client_id={mira['id']}")
# duplicates refused, Advertising can add but not edit
post("/clients", {"name": "mira studio"})
with A.app.app_context():
    assert A.q("SELECT COUNT(*) n FROM clients WHERE name LIKE 'mira studio'", one=True)["n"] == 1
t2 = csrf(ac.get("/clients").get_data(as_text=True))
assert ac.post("/clients", data={"_csrf": t2, "name": "Carrie Lead", "email": "lead@x.ca"}).status_code == 302
assert ac.get(f"/clients/{mira['id']}/edit").status_code == 403
assert "Mira Studio" in ac.get("/clients").get_data(as_text=True)          # shared list
post(f"/clients/{mira['id']}/edit", {"name": "Mira Studio", "email": "billing@mira.ca", "phone": "", "address": "", "notes": "VIP"})
with A.app.app_context():
    assert A.q("SELECT email FROM clients WHERE id=?", (mira["id"],), one=True)["email"] == "billing@mira.ca"
    assert A.q("SELECT client_email FROM invoices WHERE client_name='Mira Studio'", one=True)["client_email"] == "mira@x.ca"

# -- saved items: Advertising can add/edit/remove
assert ac.post("/items", data={"_csrf": t2, "name": "Ad package", "description": "Advertising – monthly package",
                               "qty": "1", "price": "300", "taxable": "on"}).status_code == 302
with A.app.app_context():
    item = A.q("SELECT * FROM catalog WHERE name='Ad package'", one=True)
    assert item["unit_cents"] == 30000 and item["taxable"] == 1
ac.post(f"/items/{item['id']}", data={"_csrf": t2, "name": "Ad package", "description": "Advertising – monthly package",
                                      "qty": "1", "price": "350"})
with A.app.app_context():
    item = A.q("SELECT * FROM catalog WHERE id=?", (item["id"],), one=True)
    assert item["unit_cents"] == 35000 and item["taxable"] == 0
assert 'data-desc="Advertising – monthly package"' in ac.get("/invoices/new").get_data(as_text=True)

# -- monthly billing (Carrie = Advertising account "Artie")
sent = []
def fake_send(to, subject, body, reply_to=None, **kw):
    sent.append((to, subject, body))
TODAY = A.today()
start = TODAY.isoformat()
with mock.patch.object(A, "send_email", fake_send), mock.patch.object(A, "smtp_ready", lambda: True):
    r = ac.post("/billing/new", data={"_csrf": t2, "client_id": str(mira["id"]), "start_date": start,
                                      "tax_name": "HST", "tax_rate": "13", "etransfer_email": "artie@flamewiseink.ca",
                                      "item_desc": ["Advertising package"], "item_qty": ["1"], "item_price": ["300"],
                                      "item_taxable": ["1"], "notes": "Thanks!"})
    assert r.status_code == 302, r.get_data(as_text=True)[:600]
    rid = int(r.headers["Location"].rstrip("/").split("/")[-1])
    with A.app.app_context():
        invs = A.q("SELECT * FROM invoices WHERE recurring_id=?", (rid,))
        assert len(invs) == 1, len(invs)
        inv1 = invs[0]
        label = A.B.period_label(TODAY.day, TODAY)
        it = A.q("SELECT * FROM items WHERE invoice_id=?", (inv1["id"],), one=True)
        assert it["description"] == f"Advertising package ({label})", it["description"]
        assert inv1["status"] == "sent" and inv1["total_cents"] == 33900 and inv1["client_email"] == "billing@mira.ca"
        assert inv1["created_by"] == A.q("SELECT id FROM users WHERE email='artie@x.ca'", one=True)["id"]
        assert inv1["etransfer_email"] == "artie@flamewiseink.ca" and inv1["issue_date"] == start
    assert sent and sent[0][0] == "billing@mira.ca" and inv1["number"] in sent[0][1]
    assert any("Emailed automatically to billing@mira.ca" in e for e in events(inv1["id"]))
    # running again the same day creates nothing (no duplicates, no second email)
    with A.app.test_request_context():
        assert A.run_billing() == []
    assert len(sent) == 1
    # next period: created exactly on its first day, catch-up after downtime creates each missed period once
    nxt = A.B.next_period_start(TODAY.day, TODAY)
    with A.app.test_request_context():
        assert A.run_billing(nxt - _dt.timedelta(days=1)) == []
        made = A.run_billing(A.B.next_period_start(TODAY.day, nxt))
    assert len(made) == 2 and len(sent) == 3
    with A.app.app_context():
        periods = [r_["period_start"] for r_ in A.q("SELECT period_start FROM invoices WHERE recurring_id=? ORDER BY period_start", (rid,))]
    assert periods == [TODAY.isoformat(), nxt.isoformat(), A.B.next_period_start(TODAY.day, nxt).isoformat()], periods

# Advertising owner sees the plan and its invoices; another Advertising account doesn't
assert "Mira Studio" in ac.get("/billing").get_data(as_text=True)
assert ac.get(f"/invoices/{inv1['id']}").status_code == 200
other = A.app.test_client()
post("/users", {"name": "Otto", "email": "otto@x.ca", "role": "advertising", "password": "ottopass123"})
to = csrf(other.get("/login").get_data(as_text=True))
other.post("/login", data={"_csrf": to, "email": "otto@x.ca", "password": "ottopass123"})
assert other.get(f"/billing/{rid}").status_code == 403 and "Mira Studio" not in other.get("/billing").get_data(as_text=True)
# Studio Manager sees all plans
assert "Mira Studio" in mc.get("/billing").get_data(as_text=True)

# client with active monthly billing can't be removed
post(f"/clients/{mira['id']}/delete", {})
with A.app.app_context():
    assert A.q("SELECT 1 FROM clients WHERE id=?", (mira["id"],), one=True)

# pause -> nothing created; stop -> ended
ac.post(f"/billing/{rid}/status", data={"_csrf": t2, "action": "pause"})
with A.app.test_request_context():
    far = TODAY + _dt.timedelta(days=200)
    assert A.run_billing(far) == []
ac.post(f"/billing/{rid}/status", data={"_csrf": t2, "action": "stop"})
with A.app.app_context():
    assert A.q("SELECT status FROM recurring WHERE id=?", (rid,), one=True)["status"] == "ended"

# future start: nothing yet; stop_after ends the plan; no client email -> created, not emailed
lead = None
with A.app.app_context():
    lead = A.q("SELECT id FROM clients WHERE name='Carrie Lead'", one=True)["id"]
    A.x("UPDATE clients SET email='' WHERE id=?", (lead,))
fut = (TODAY + _dt.timedelta(days=10))
r = ac.post("/billing/new", data={"_csrf": t2, "client_id": str(lead), "start_date": fut.isoformat(),
                                  "stop_after": (fut + _dt.timedelta(days=40)).isoformat(), "tax_rate": "0",
                                  "etransfer_email": "pay@flamewiseink.ca", "item_desc": ["Ad"], "item_qty": ["1"],
                                  "item_price": ["100"], "item_taxable": ["1"]})
rid2 = int(r.headers["Location"].rstrip("/").split("/")[-1])
with A.app.app_context():
    assert not A.q("SELECT 1 FROM invoices WHERE recurring_id=?", (rid2,), one=True)
with mock.patch.object(A, "smtp_ready", lambda: True), A.app.test_request_context():
    made = A.run_billing(fut + _dt.timedelta(days=120))
assert len(made) == 2, made            # fut and fut+1 month; third period starts after stop date
with A.app.app_context():
    assert A.q("SELECT status FROM recurring WHERE id=?", (rid2,), one=True)["status"] == "ended"
assert any("client has no email" in e for e in events(made[0]))

# pages render
for path in ["/clients", "/items", "/billing", f"/billing/{rid}", "/billing/new", f"/clients/{lead}/edit"]:
    get(path)
assert "billing has ended" in get(f"/billing/{rid2}")
print("CLIENTS / ITEMS / MONTHLY BILLING TESTS PASSED")

# --- Sharing invoices with Advertising ---
TOK = csrf(get("/"))
t3 = csrf(mc.get("/").get_data(as_text=True))
r = mc.post("/invoices/new", data={"_csrf": t3, "client_name": "Shared Co", "issue_date": "2026-11-19",
                                   "due_date": "2026-12-03", "tax_rate": "0", "item_desc": ["Ad"], "item_qty": ["1"],
                                   "item_price": ["120"], "item_taxable": ["1"]})
S = int(r.headers["Location"].split("/")[-1])
with A.app.app_context():
    artie_id = A.q("SELECT id FROM users WHERE email='artie@x.ca'", one=True)["id"]
    otto_id = A.q("SELECT id FROM users WHERE email='otto@x.ca'", one=True)["id"]
t2 = csrf(ac.get("/").get_data(as_text=True))
assert ac.get(f"/invoices/{S}").status_code == 403 and "Shared Co" not in ac.get("/?f=all").get_data(as_text=True)
page = mc.get(f"/invoices/{S}").get_data(as_text=True)
assert "Share with Advertising" in page and f'value="{artie_id}"' in page
mc.post(f"/invoices/{S}/share", data={"_csrf": t3, "user_id": [str(artie_id)]})
assert any("Shared with Artie by Mona" in e for e in events(S)), events(S)
# Artie: sees it, marked shared, works on it like his own
d = ac.get("/?f=all").get_data(as_text=True)
assert "Shared Co" in d and ">shared<" in d
v = ac.get(f"/invoices/{S}").get_data(as_text=True)
assert v.count("shared with you") == 1 and "Share with Advertising" not in v and "Record a payment" in v
assert ac.post(f"/invoices/{S}/status", data={"_csrf": t2, "action": "sent"}).status_code == 302
assert ac.post(f"/invoices/{S}/payments", data={"_csrf": t2, "amount": "20", "method": "e-Transfer"}).status_code == 302
assert row(S)["paid_cents"] == 2000 and row(S)["status"] == "partial"
# Artie can't share it onward, can't remove payments
ac.post(f"/invoices/{S}/share", data={"_csrf": t2, "user_id": [str(otto_id)]})
with A.app.app_context():
    assert A.q("SELECT COUNT(*) n FROM invoice_shares WHERE invoice_id=?", (S,), one=True)["n"] == 1
# Otto still can't see it
assert other.get(f"/invoices/{S}").status_code == 403
# stop sharing
mc.post(f"/invoices/{S}/share", data={"_csrf": t3})
assert ac.get(f"/invoices/{S}").status_code == 403
assert any("Stopped sharing with Artie by Mona" in e for e in events(S))
print("SHARING TESTS PASSED")

# --- Helcim token and email settings saved in the app (survive redeploys) ---
os.environ.pop("HELCIM_API_TOKEN", None)
with A.app.app_context():
    assert A.helcim_token() == ""
    assert not A.smtp_ready()
base = {"business_name": "Flame Wise Tattoo Collective", "accent_color": "#df620c", "invoice_title": "Invoice",
        "invoice_prefix": "FW-", "next_number": "3000", "default_due_days": "14", "tax_name": "HST", "tax_rate": "13",
        "currency": "CAD", "etransfer_enabled": "on", "etransfer_email": "pay@flamewiseink.ca", "helcim_enabled": "on",
        "etransfer_instructions": A.DEFAULTS["etransfer_instructions"], "email_subject": A.DEFAULTS["email_subject"],
        "email_body": A.DEFAULTS["email_body"], "business_email": "team@flamewise.ca",
        "smtp_host": "giowm1301.siteground.biz", "smtp_port": "465", "smtp_security": "ssl",
        "smtp_user": "team@flamewise.ca", "smtp_from": "Flame Wise Tattoo Collective <team@flamewise.ca>"}
post("/settings", dict(base, helcim_token="tok-123", smtp_password="pw-1"))
with A.app.app_context():
    assert A.helcim_token() == "tok-123" and A.helcim_ready()
    cfg = A.smtp_config()
    assert cfg["host"] == "giowm1301.siteground.biz" and cfg["password"] == "pw-1" and cfg["source"] == "app" and A.smtp_ready()
    assert not any(k.startswith("secret_") for k in A.settings())           # secrets never reach templates
sp = get("/settings")
assert "tok-123" not in sp and "pw-1" not in sp and "Saved. Paste a new one" in sp and "Saved. Type a new one" in sp
# saving again with blank secret fields keeps them
post("/settings", base)
with A.app.app_context():
    assert A.helcim_token() == "tok-123" and A.smtp_config()["password"] == "pw-1"
# Helcim script loads on the client page once a token is saved
assert "secure.helcim.app/helcim-pay/services/start.js" in anon.get(f"/i/{Htok}").get_data(as_text=True)
# test email uses saved settings via smtplib
class FakeSMTP:
    sent = []
    def __init__(self, host, port, timeout=None): self.host, self.port = host, port
    def login(self, u, p): FakeSMTP.login_with = (u, p)
    def send_message(self, m): FakeSMTP.sent.append((self.host, self.port, m["To"], m["From"]))
    def quit(self): pass
with mock.patch.object(A.smtplib, "SMTP_SSL", FakeSMTP):
    post("/settings/test-email", {"to": "me@x.ca"})
assert FakeSMTP.sent == [("giowm1301.siteground.biz", 465, "me@x.ca", "Flame Wise Tattoo Collective <team@flamewise.ca>")], FakeSMTP.sent
assert FakeSMTP.login_with == ("team@flamewise.ca", "pw-1")
# Portainer values still override when present
os.environ["HELCIM_API_TOKEN"] = "env-tok"
with A.app.app_context():
    assert A.helcim_token() == "env-tok"
os.environ.pop("HELCIM_API_TOKEN")
# remove saved token
post("/settings", dict(base, remove_helcim_token="on"))
with A.app.app_context():
    assert A.helcim_token() == ""
print("SAVED INTEGRATION SETTINGS TESTS PASSED")

# --- "+ Add a new email…" in the e-Transfer dropdown ---
TOK = csrf(get("/"))
assert '<option value="__new__">+ Add a new email…</option>' in get("/invoices/new")
def inv_with(et, new=""):
    return c.post("/invoices/new", data={"_csrf": TOK, "client_name": "Nina", "issue_date": "2026-11-19",
                                         "due_date": "2026-12-03", "tax_rate": "0", "etransfer_email": et,
                                         "etransfer_email_new": new, "item_desc": ["Ad"], "item_qty": ["1"],
                                         "item_price": ["10"], "item_taxable": ["1"]})
assert inv_with("__new__", "").status_code == 400                        # picked "new" but typed nothing
assert inv_with("__new__", "not-an-email").status_code == 400
r = inv_with("__new__", "nina.pay@flamewiseink.ca")
assert r.status_code == 302
nid = int(r.headers["Location"].split("/")[-1])
assert row(nid)["etransfer_email"] == "nina.pay@flamewiseink.ca"
with A.app.app_context():
    assert A.settings()["etransfer_emails"].splitlines()[-1] == "nina.pay@flamewiseink.ca"
f = get("/invoices/new")
assert 'value="nina.pay@flamewiseink.ca"' in f and "added to the e-Transfer email list" in f
# typing an email that's already listed doesn't add a duplicate
inv_with("__new__", "NINA.PAY@flamewiseink.ca")
with A.app.app_context():
    assert A.settings()["etransfer_emails"].lower().count("nina.pay@flamewiseink.ca") == 1
# monthly billing form does the same
with A.app.app_context():
    any_client = A.q("SELECT id FROM clients LIMIT 1", one=True)["id"]
r = c.post("/billing/new", data={"_csrf": TOK, "client_id": str(any_client), "start_date": "2030-01-10", "tax_rate": "0",
                                 "etransfer_email": "__new__", "etransfer_email_new": "plans@flamewiseink.ca",
                                 "item_desc": ["Ad"], "item_qty": ["1"], "item_price": ["10"], "item_taxable": ["1"]})
assert r.status_code == 302
with A.app.app_context():
    assert "plans@flamewiseink.ca" in A.settings()["etransfer_emails"]
print("NEW E-TRANSFER EMAIL TESTS PASSED")

# --- card payment routes always answer with JSON (never a web page) ---
os.environ["HELCIM_API_TOKEN"] = "t"
r = anon.get(f"/i/{Htok}/helcim/start")
assert r.status_code == 405 and r.is_json, (r.status_code, r.data[:80])
with mock.patch.object(A.L, "client_can_pay_online", side_effect=RuntimeError("boom")):
    r = anon.post(f"/i/{Htok}/helcim/start", json={})
assert r.status_code == 400 and r.is_json and "e-Transfer" in r.get_json()["error"], r.data[:120]
r = anon.post("/i/no-such-token/helcim/start", json={})
assert r.status_code == 404 and r.is_json
# ordinary pages still get the friendly HTML error page
assert "Not found" in anon.get("/i/no-such-token").get_data(as_text=True)
os.environ.pop("HELCIM_API_TOKEN")
print("CARD ROUTE JSON TESTS PASSED")

# --- Helcim refuses the convenience fee: checkout opens without it (and errors never use 502) ---
os.environ["HELCIM_API_TOKEN"] = "t"
with A.app.app_context():
    A.save_settings({"helcim_fee": "1"})
seen = []
def init_fee(url, json=None, headers=None, timeout=None):
    seen.append(dict(json))
    if json.get("hasConvenienceFee"):
        return R(400, {"errors": "Convenience fee can only be applied when both payment methods exist"})
    return R(200, {"checkoutToken": "ct-fee", "secretToken": "s-fee"})
F = new_invoice(c, csrf(get("/")), "Fiona", price="25")
post(f"/invoices/{F}/status", {"action": "sent"})
Ftok = row(F)["token"]
with mock.patch.object(A.requests, "post", init_fee):
    r = anon.post(f"/i/{Ftok}/helcim/start", json={})
assert r.status_code == 200 and r.get_json()["checkoutToken"] == "ct-fee", r.data
assert seen[0].get("hasConvenienceFee") == 1 and seen[0].get("paymentMethod") == "cc-ach"
assert "hasConvenienceFee" not in seen[1] and "paymentMethod" not in seen[1]
assert any("without the convenience fee" in e for e in events(F))
def init_down(url, json=None, headers=None, timeout=None):
    return R(500, {"errors": "down"})
with mock.patch.object(A.requests, "post", init_down):
    r = anon.post(f"/i/{Ftok}/helcim/start", json={})
assert r.status_code == 400 and r.is_json
with A.app.app_context():
    A.save_settings({"helcim_fee": "0"})
os.environ.pop("HELCIM_API_TOKEN")
print("CONVENIENCE FEE FALLBACK TESTS PASSED")

# --- Fee Saver accepted; bank (ACH) payments ---
os.environ["HELCIM_API_TOKEN"] = "t"
with A.app.app_context():
    A.save_settings({"helcim_fee": "1"})
assert "Pay $" in anon.get(f"/i/{Ftok}").get_data(as_text=True) and "Bank payment and e-Transfer have no fee" in anon.get(f"/i/{Ftok}").get_data(as_text=True)
ach_calls = []
import itertools as _it
_ct_counter = _it.count()
def init_ok2(url, json=None, headers=None, timeout=None):
    return R(200, {"checkoutToken": f"ach-{next(_ct_counter)}", "secretToken": "s"})
def ach_get(status_auth, amount="25.00"):
    def g(url, headers=None, timeout=None):
        ach_calls.append(url)
        return R(200, {"transactionId": 8801, "amount": amount, "statusAuth": status_auth, "statusClearing": "OPENED",
                       "bankToken": "bt", "dateCreated": "2026-11-19 12:00:00"})
    return g
msg = {"data": {"data": {"transactionId": 8801, "amount": "25.00", "bankToken": "bt", "statusAuth": "PENDING"}, "hash": "x"}}
# pending bank payment: not recorded, flagged for staff, client told it's on its way
with mock.patch.object(A.requests, "post", init_ok2), mock.patch.object(A.requests, "get", ach_get("PENDING")):
    ct = anon.post(f"/i/{Ftok}/helcim/start", json={}).get_json()["checkoutToken"]
    r = anon.post(f"/i/{Ftok}/helcim/confirm", json={"checkoutToken": ct, "eventMessage": msg})
assert r.status_code == 200 and r.get_json().get("pending") is True
assert ach_calls[-1].endswith("/ach/transactions/8801") and row(F)["paid_cents"] == 0
assert any("bank payment" in e and "once it clears" in e for e in events(F))
assert "bank payment was sent" in anon.get(f"/i/{Ftok}?pending=1").get_data(as_text=True)
# approved bank payment: recorded as Helcim (bank), invoice paid, no fee
msg["data"]["data"]["transactionId"] = 8802
with mock.patch.object(A.requests, "post", init_ok2), mock.patch.object(A.requests, "get", lambda url, headers=None, timeout=None: R(200, {"transactionId": 8802, "amount": "25.00", "statusAuth": "APPROVED", "statusClearing": "OPENED", "bankToken": "bt"})):
    ct = anon.post(f"/i/{Ftok}/helcim/start", json={}).get_json()["checkoutToken"]
    r = anon.post(f"/i/{Ftok}/helcim/confirm", json={"checkoutToken": ct, "eventMessage": msg})
assert r.status_code == 200 and not r.get_json().get("pending")
with A.app.app_context():
    p_ = A.q("SELECT * FROM payments WHERE invoice_id=?", (F,), one=True)
assert p_["method"] == "Helcim (bank)" and p_["amount_cents"] == 2500 and "few business days" in p_["note"]
assert row(F)["status"] == "paid"
# declined bank payment
G = new_invoice(c, csrf(get("/")), "Gus", price="30"); post(f"/invoices/{G}/status", {"action": "sent"}); Gtok = row(G)["token"]
msg["data"]["data"]["transactionId"] = 8803
with mock.patch.object(A.requests, "post", init_ok2), mock.patch.object(A.requests, "get", lambda url, headers=None, timeout=None: R(200, {"transactionId": 8803, "amount": "30.00", "statusAuth": "DECLINED", "bankToken": "bt"})):
    ct = anon.post(f"/i/{Gtok}/helcim/start", json={}).get_json()["checkoutToken"]
    r = anon.post(f"/i/{Gtok}/helcim/confirm", json={"checkoutToken": ct, "eventMessage": msg})
assert r.status_code == 400 and row(G)["paid_cents"] == 0
# card payment with fee: fee noted, invoice credited the invoice amount
H2 = new_invoice(c, csrf(get("/")), "Hank", price="100"); post(f"/invoices/{H2}/status", {"action": "sent"}); H2tok = row(H2)["token"]
with mock.patch.object(A.requests, "post", init_ok2), mock.patch.object(A.requests, "get", lambda url, headers=None, timeout=None: R(200, {"transactionId": 9901, "amount": "103.00", "status": "APPROVED", "cardToken": "c"})):
    ct = anon.post(f"/i/{H2tok}/helcim/start", json={}).get_json()["checkoutToken"]
    r = anon.post(f"/i/{H2tok}/helcim/confirm", json={"checkoutToken": ct, "eventMessage": {"data": {"transactionId": 9901, "cardToken": "c"}}})
assert r.status_code == 200
with A.app.app_context():
    p_ = A.q("SELECT * FROM payments WHERE invoice_id=?", (H2,), one=True)
assert p_["amount_cents"] == 10000 and "incl. $3.00 convenience fee" in p_["note"] and row(H2)["status"] == "paid"
with A.app.app_context():
    A.save_settings({"helcim_fee": "0"})
os.environ.pop("HELCIM_API_TOKEN")
print("FEE SAVER + BANK PAYMENT TESTS PASSED")

# --- invoice emails carry the full summary (HTML + text), proper headers, embedded logo ---
TOK = csrf(get("/"))
E = new_invoice(c, TOK, "Erin", price="150", tax="13")
post(f"/invoices/{E}/status", {"action": "sent"})
pv = get(f"/invoices/{E}/email-preview")
assert row(E)["number"] in pv and "$169.50" in pv and "View and pay invoice" in pv and "Session" in pv
assert "Pay by Interac e-Transfer" in pv and "<title>Invoice" in pv
vw = get(f"/invoices/{E}")
assert "Preview email" in vw and "Amount%20due%3A%20%24169.50" in vw            # mailto body includes the summary
with A.app.app_context():
    A.save_settings({"logo": "logo-email-test.png", "smtp_host": "smtp.example.com", "smtp_port": "465",
                     "smtp_security": "ssl", "smtp_user": "team@flamewise.ca",
                     "smtp_from": "Flame Wise Tattoo Collective <team@flamewise.ca>"})
open(os.path.join(A.UPLOAD_DIR, "logo-email-test.png"), "wb").write(b"\x89PNG\r\n\x1a\nfake")
class CaptureSMTP:
    msgs = []
    def __init__(self, host, port, timeout=None): pass
    def login(self, u, p): pass
    def send_message(self, m): CaptureSMTP.msgs.append(m)
    def quit(self): pass
with mock.patch.object(A.smtplib, "SMTP_SSL", CaptureSMTP):
    post(f"/invoices/{E}/email", {"to": "erin@x.ca"})
m = CaptureSMTP.msgs[-1]
assert m["Date"] and m["Message-ID"].endswith("@flamewise.ca>"), (m["Date"], m["Message-ID"])
plain = m.get_body(("plain",)).get_content()
html = m.get_body(("html",)).get_content()
assert row(E)["number"] in plain and "Amount due: $169.50" in plain and "/i/" in plain
assert "$169.50" in html and 'src="cid:logo"' in html
assert any(part.get("Content-ID") == "<logo>" for part in m.walk())
with A.app.app_context():
    A.save_settings({"logo": "", "smtp_host": ""})
print("INVOICE EMAIL TESTS PASSED")

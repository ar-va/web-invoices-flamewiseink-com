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
h = get("/invoices/new")
assert "Tattoo session (hourly)" in h
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
assert f.count('<option value="artie@flamewiseink.ca">') == 1 and f.count('<option value="ads@flamewiseink.ca">') == 1
assert 'name="etransfer_email" value="ads@flamewiseink.ca"' in f
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
assert 'value="artie@flamewiseink.ca"' in get(f"/invoices/{cid}/edit")
# duplicate keeps it
r = post(f"/invoices/{cid}/duplicate", {})
assert 'value="artie@flamewiseink.ca"' in get(r.headers["Location"])
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
# client autocomplete: Advertising only sees own clients (Adele, Jane Doe from earlier), not Lena/Carla
form = ac.get("/invoices/new").get_data(as_text=True)
assert "adele@x.ca" in form and "lena@x.ca" not in form and "carla" not in form.lower(), form[-800:]
assert "\"Lena\"" in get("/invoices/new") and "\"Lena\"" not in form   # admin sees all

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

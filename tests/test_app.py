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
assert anon.get(f"/i/{token}").status_code == 404
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

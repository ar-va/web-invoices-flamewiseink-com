"""Flame Wise Tattoo Collective - self-hosted invoice generator.

Flask + SQLite. Multi-user (admin / studio manager / advertising), customizable branding and
templates, Interac e-Transfer instructions, optional Helcim card payments via
HelcimPay.js.
"""
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from email.message import EmailMessage
from functools import wraps
from zoneinfo import ZoneInfo

import requests
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import billing as B
import lifecycle as L

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
DATA_DIR = os.environ.get("DATA_DIR", "/data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "invoices.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)

HELCIM_API = os.environ.get("HELCIM_API_URL", "https://api.helcim.com/v2").rstrip("/")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

ROLES = {
    "admin": ("Admin", "everything, incl. settings and accounts"),
    "manager": ("Studio Manager", "all invoices, no settings or accounts"),
    "advertising": ("Advertising", "only invoices they created"),
}
SEE_ALL_ROLES = L.SEES_ALL_ROLES
OLD_ROLE_NAMES = {"staff": "manager", "artist": "advertising"}
STATUSES = [L.DRAFT, L.SENT, L.PARTIAL, L.PAID, L.VOID]
PAY_METHODS = ["e-Transfer", "Helcim (card)", "Helcim (bank)", "Cash", "Debit/Credit (terminal)", "Other"]

log = logging.getLogger("invoices")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _secret_key():
    env = os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env
    path = os.path.join(DATA_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
        os.chmod(path, 0o600)
    with open(path) as f:
        return f.read().strip()


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SECURE_COOKIES", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
)

DEFAULTS = {
    "business_name": "Flame Wise Tattoo Collective",
    "business_address": "219 Dufferin St, Unit 206B\nToronto, ON M6V 0B2",
    "business_email": "team@flamewise.ca",
    "business_phone": "",
    "business_website": "",
    "tax_number_label": "HST #",
    "tax_number": "",
    "logo": "",
    "accent_color": "#df620c",
    "invoice_title": "Invoice",
    "invoice_prefix": "FW-",
    "next_number": "1001",
    "default_due_days": "14",
    "tax_name": "HST",
    "tax_rate": "13",
    "currency": "CAD",
    "show_artist": "1",
    "creator_label": "Prepared by",
    "default_notes": "Thank you for choosing Flame Wise Tattoo Collective!",
    "default_terms": "",
    "quick_items": "",
    "etransfer_enabled": "1",
    "etransfer_email": "pay@flamewiseink.ca",
    "smtp_host": "",
    "smtp_port": "465",
    "smtp_security": "ssl",
    "smtp_user": "",
    "smtp_from": "",
    "etransfer_emails": "",
    "etransfer_instructions": (
        "Send an Interac e-Transfer for {amount} to {email}.\n"
        "Put invoice number {number} in the message so we can match your payment."
    ),
    "helcim_enabled": "1",
    "helcim_fee": "0",
    "email_subject": "Invoice {number} from {business}",
    "email_body": (
        "Hi {client},\n\n"
        "Here is invoice {number} for {amount}, due {due_date}.\n\n"
        "View and pay online: {link}\n\n"
        "Prefer e-Transfer? Send it to {email} with {number} in the message.\n\n"
        "Thank you,\n{business}"
    ),
}

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL COLLATE NOCASE, name TEXT NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'advertising', active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS invoices(
  id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL, token TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  client_name TEXT NOT NULL, client_email TEXT, client_phone TEXT, client_address TEXT,
  issue_date TEXT NOT NULL, due_date TEXT NOT NULL,
  tax_name TEXT, tax_rate TEXT NOT NULL DEFAULT '0', discount_cents INTEGER NOT NULL DEFAULT 0,
  notes TEXT, terms TEXT,
  subtotal_cents INTEGER NOT NULL DEFAULT 0, tax_cents INTEGER NOT NULL DEFAULT 0,
  total_cents INTEGER NOT NULL DEFAULT 0, paid_cents INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  sent_at TEXT, viewed_at TEXT, paid_at TEXT);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  position INTEGER NOT NULL, description TEXT NOT NULL, qty TEXT NOT NULL,
  unit_cents INTEGER NOT NULL, taxable INTEGER NOT NULL DEFAULT 1, amount_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  method TEXT NOT NULL, amount_cents INTEGER NOT NULL, paid_on TEXT NOT NULL, reference TEXT,
  helcim_transaction_id TEXT UNIQUE, note TEXT, created_by INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS helcim_sessions(
  id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  checkout_token TEXT UNIQUE NOT NULL, secret_token TEXT NOT NULL, amount_cents INTEGER NOT NULL,
  created_at TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  at TEXT NOT NULL, text TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clients(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT, phone TEXT, address TEXT, notes TEXT,
  created_by INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_name ON clients(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS catalog(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, qty TEXT NOT NULL DEFAULT '1',
  unit_cents INTEGER NOT NULL DEFAULT 0, taxable INTEGER NOT NULL DEFAULT 1,
  created_by INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recurring(
  id INTEGER PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES clients(id),
  start_date TEXT NOT NULL, anchor_day INTEGER NOT NULL, next_period_start TEXT NOT NULL, stop_after TEXT,
  status TEXT NOT NULL DEFAULT 'active',
  tax_name TEXT, tax_rate TEXT NOT NULL DEFAULT '0', discount_cents INTEGER NOT NULL DEFAULT 0,
  notes TEXT, terms TEXT, etransfer_email TEXT,
  created_by INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recurring_items(
  id INTEGER PRIMARY KEY, recurring_id INTEGER NOT NULL REFERENCES recurring(id) ON DELETE CASCADE,
  position INTEGER NOT NULL, description TEXT NOT NULL, qty TEXT NOT NULL,
  unit_cents INTEGER NOT NULL, taxable INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS invoice_shares(
  invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
  user_id INTEGER NOT NULL REFERENCES users(id), shared_by INTEGER, created_at TEXT NOT NULL,
  PRIMARY KEY(invoice_id, user_id));
CREATE INDEX IF NOT EXISTS idx_shares_user ON invoice_shares(user_id);
CREATE INDEX IF NOT EXISTS idx_items_inv ON items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_pay_inv ON payments(invoice_id);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _seed_once(con):
    """One-time moves: past invoice clients -> client list, Settings quick items -> saved items."""
    done = {r[0] for r in con.execute("SELECT key FROM settings WHERE key LIKE 'seeded_%'")}
    ts = now_iso()
    if "seeded_clients" not in done:
        con.execute("""INSERT OR IGNORE INTO clients(name,email,phone,address,created_by,created_at,updated_at)
                       SELECT client_name, client_email, client_phone, client_address, created_by, ?, ?
                       FROM invoices WHERE id IN (SELECT MAX(id) FROM invoices GROUP BY lower(client_name))""",
                    (ts, ts))
        con.execute("UPDATE invoices SET client_id=(SELECT id FROM clients c WHERE c.name=invoices.client_name "
                    "COLLATE NOCASE) WHERE client_id IS NULL")
        con.execute("INSERT INTO settings(key,value) VALUES('seeded_clients','1')")
    if "seeded_catalog" not in done:
        row = con.execute("SELECT value FROM settings WHERE key='quick_items'").fetchone()
        for line in (row[0] if row else "").splitlines():
            if "|" in line:
                d, price = (part.strip() for part in line.rsplit("|", 1))
                try:
                    cents = to_cents(price)
                except ValueError:
                    cents = 0
                if d:
                    con.execute("INSERT INTO catalog(name,description,unit_cents,created_at,updated_at) "
                                "VALUES(?,?,?,?,?)", (d, d, cents, ts, ts))
        con.execute("INSERT INTO settings(key,value) VALUES('seeded_catalog','1')")


def bootstrap():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(invoices)")}
    for col in ("etransfer_email TEXT", "client_id INTEGER", "recurring_id INTEGER", "period_start TEXT"):
        if col.split()[0] not in cols:
            con.execute(f"ALTER TABLE invoices ADD COLUMN {col}")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_inv_period ON invoices(recurring_id, period_start) "
                "WHERE recurring_id IS NOT NULL")
    _seed_once(con)
    for old, new in OLD_ROLE_NAMES.items():
        con.execute("UPDATE users SET role=? WHERE role=?", (new, old))
    count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count == 0:
        email = os.environ.get("ADMIN_EMAIL", "").strip()
        pw = os.environ.get("ADMIN_PASSWORD", "")
        if email and pw:
            con.execute(
                "INSERT INTO users(email,name,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                (email, os.environ.get("ADMIN_NAME", "Admin"), generate_password_hash(pw), "admin", now_iso()),
            )
            log.info("Created first admin account %s", email)
        else:
            log.warning("No accounts exist. Set ADMIN_EMAIL and ADMIN_PASSWORD and restart.")
    con.commit()
    con.close()


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------


def db():
    if "db" not in g:
        con = sqlite3.connect(DB_PATH, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        g.db = con
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def q(sql, args=(), one=False):
    rows = db().execute(sql, args).fetchall()
    if one:
        return rows[0] if rows else None
    return rows


def x(sql, args=()):
    cur = db().execute(sql, args)
    db().commit()
    return cur.lastrowid


def settings():
    if "settings" not in g:
        s = dict(DEFAULTS)
        s.update({r["key"]: r["value"] for r in q("SELECT key, value FROM settings") if not r["key"].startswith("secret_")})
        g.settings = s
    return g.settings


def secret(key):
    """Passwords/tokens saved in Settings. Kept out of settings() so no page can print them."""
    row = q("SELECT value FROM settings WHERE key=?", (f"secret_{key}",), one=True)
    return (row["value"] if row else "") or ""


def helcim_token():
    """Portainer's HELCIM_API_TOKEN wins if set; otherwise the token saved in Settings."""
    return os.environ.get("HELCIM_API_TOKEN", "").strip() or secret("helcim_token").strip()


def smtp_config():
    """Email sending settings: from Portainer (SMTP_HOST set) or from the Settings page."""
    if os.environ.get("SMTP_HOST", "").strip():
        e = os.environ.get
        return {"host": e("SMTP_HOST", "").strip(), "port": e("SMTP_PORT", "587"), "security": e("SMTP_SECURITY", "starttls"),
                "user": e("SMTP_USER", ""), "password": e("SMTP_PASSWORD", ""), "from": e("SMTP_FROM", ""),
                "source": "portainer"}
    st = settings()
    return {"host": st["smtp_host"], "port": st["smtp_port"] or "465", "security": st["smtp_security"] or "ssl",
            "user": st["smtp_user"], "password": secret("smtp_password"), "from": st["smtp_from"], "source": "app"}


def save_settings(values):
    con = db()
    with con:
        for k, v in values.items():
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
    g.pop("settings", None)


def add_event(invoice_id, text, con=None):
    """Log to the invoice's activity. Pass con to join the caller's transaction."""
    sql, args = "INSERT INTO events(invoice_id,at,text) VALUES(?,?,?)", (invoice_id, now_iso(), text)
    if con is not None:
        con.execute(sql, args)
    else:
        x(sql, args)


# --------------------------------------------------------------------------
# Money & formatting
# --------------------------------------------------------------------------


def to_cents(value):
    s = str(value if value is not None else "").replace("$", "").replace(",", "").strip()
    if not s:
        return 0
    try:
        return int((Decimal(s) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        raise ValueError(f"'{value}' is not a valid amount")


def to_decimal(value, what="number"):
    s = str(value or "").replace(",", "").strip()
    if not s:
        raise ValueError(f"Missing {what}")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"'{value}' is not a valid {what}")
    if not d.is_finite():
        raise ValueError(f"'{value}' is not a valid {what}")
    return d


bootstrap()   # needs to_cents for one-time moves


@app.template_filter("money")
def money(cents):
    cents = int(cents or 0)
    d = (Decimal(abs(cents)) / 100).quantize(Decimal("0.01"))
    return f"{'-' if cents < 0 else ''}${d:,}"


@app.template_filter("plain_money")
def plain_money(cents):
    return f"{(Decimal(int(cents or 0)) / 100).quantize(Decimal('0.01'))}"


@app.template_filter("qty")
def fmt_qty(q_):
    d = Decimal(str(q_))
    return f"{d.normalize():f}" if d == d.to_integral() else f"{d.normalize()}"


@app.template_filter("datefmt")
def datefmt(s):
    if not s:
        return ""
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%b %-d, %Y")
    except ValueError:
        return s


@app.template_filter("localtime")
def localtime(s):
    if not s:
        return ""
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        tzname = os.environ.get("TZ")
        if tzname:
            from zoneinfo import ZoneInfo
            dt = dt.astimezone(ZoneInfo(tzname))
        return dt.strftime("%b %-d, %Y %-I:%M %p")
    except Exception:
        return s


def compute_totals(items, tax_rate, discount_cents):
    subtotal = sum(i["amount_cents"] for i in items)
    taxable = sum(i["amount_cents"] for i in items if i["taxable"])
    disc = max(0, min(discount_cents, subtotal))
    taxable_after = Decimal(taxable)
    if subtotal and disc:
        taxable_after -= Decimal(disc) * Decimal(taxable) / Decimal(subtotal)
    tax = int((taxable_after * Decimal(str(tax_rate)) / 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return {"subtotal": subtotal, "discount": disc, "tax": tax, "total": subtotal - disc + tax}


def today():
    """Today's date in the studio's time zone (overdue and payment dates use it)."""
    try:
        return datetime.now(ZoneInfo(os.environ.get("TZ") or "America/Toronto")).date()
    except Exception:  # noqa: BLE001 - bad TZ value
        return datetime.now(ZoneInfo("America/Toronto")).date()


def shared_with(iid):
    """Advertising accounts an invoice is shared with (cached per request)."""
    cache = g.setdefault("_shares", {})
    if iid not in cache:
        cache[iid] = frozenset(r["user_id"] for r in q("SELECT user_id FROM invoice_shares WHERE invoice_id=?", (iid,)))
    return cache[iid]


def state(inv):
    return L.Invoice.from_row(inv, shared_with(inv["id"]))


def display_status(inv):
    return L.display_status(L.Invoice.from_row(inv), today())


def balance_of(inv):
    return L.balance(L.Invoice.from_row(inv))


app.jinja_env.globals.update(display_status=display_status, balance_of=balance_of, ROLES=ROLES,
                             PAY_METHODS=PAY_METHODS)


def etransfer_email_for(inv):
    """The invoice's own e-Transfer email, or the default from Settings."""
    try:
        own = inv["etransfer_email"]
    except (KeyError, IndexError):
        own = None
    return own or settings()["etransfer_email"]


NEW_ETRANSFER = "__new__"


def chosen_etransfer(form):
    """The e-Transfer email picked in the dropdown, or the new one typed after '+ Add a new email…'."""
    value = form.get("etransfer_email", "").strip()
    if value == NEW_ETRANSFER:
        return form.get("etransfer_email_new", "").strip(), True
    return value, False


def remember_etransfer(email):
    """Add a newly typed e-Transfer email to Settings → Other e-Transfer emails (once)."""
    if not email or email.lower() in (e.lower() for e in etransfer_email_options()):
        return False
    current = settings()["etransfer_emails"].strip()
    save_settings({"etransfer_emails": (current + "\n" if current else "") + email})
    return True


def etransfer_email_options():
    s = settings()
    opts = [s["etransfer_email"]] + [l.strip() for l in s["etransfer_emails"].splitlines()]
    return [e for i, e in enumerate(opts) if e and e not in opts[:i]]


app.jinja_env.globals.update(etransfer_email_for=etransfer_email_for, etransfer_email_options=etransfer_email_options)


def fill_template(template, inv):
    s = settings()
    due = inv["total_cents"] - inv["paid_cents"]
    tokens = {
        "number": inv["number"],
        "business": s["business_name"],
        "client": inv["client_name"],
        "amount": money(due if due > 0 else inv["total_cents"]),
        "total": money(inv["total_cents"]),
        "balance": money(due),
        "due_date": datefmt(inv["due_date"]),
        "link": public_url(inv),
        "email": etransfer_email_for(inv),
    }
    return re.sub(r"\{(\w+)\}", lambda m: tokens.get(m.group(1), m.group(0)), template or "")


app.jinja_env.globals["fill_template"] = fill_template


def public_url(inv):
    path = url_for("public_invoice", token=inv["token"])
    if BASE_URL:
        return BASE_URL + path
    try:
        return url_for("public_invoice", token=inv["token"], _external=True)
    except RuntimeError:
        return path


app.jinja_env.globals["public_url"] = public_url

# --------------------------------------------------------------------------
# Auth, CSRF, permissions
# --------------------------------------------------------------------------


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def _csrf_protect():
    if request.method == "POST" and not request.path.startswith("/i/"):
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
        if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
            abort(400, "This form expired. Go back, reload the page and try again.")


def current_user():
    if "user" not in g:
        uid = session.get("uid")
        g.user = q("SELECT * FROM users WHERE id=? AND active=1", (uid,), one=True) if uid else None
    return g.user


app.jinja_env.globals["current_user"] = current_user


def login_required(*roles):
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                return redirect(url_for("login", next=request.full_path))
            if roles and u["role"] not in roles:
                abort(403)
            return f(*a, **kw)
        return wrapper
    return deco


def actor():
    u = current_user()
    return L.Actor(u["id"], u["role"]) if u else None


def can_see(inv):
    return L.can_view(state(inv), actor())


def sees_all():
    return L.sees_all(actor())


def allowed(inv):
    return L.allowed_actions(state(inv), actor())


app.jinja_env.globals.update(sees_all=sees_all, allowed=allowed)


def get_invoice(iid):
    inv = q("SELECT i.*, u.name AS artist_name FROM invoices i LEFT JOIN users u ON u.id=i.created_by WHERE i.id=?",
            (iid,), one=True)
    if not inv:
        abort(404)
    if not can_see(inv):
        abort(403)
    return inv


_login_failures = defaultdict(list)


def _too_many_failures(key):
    cutoff = time.time() - 900
    _login_failures[key] = [t for t in _login_failures[key] if t > cutoff]
    return len(_login_failures[key]) >= 8


@app.route("/login", methods=["GET", "POST"])
def login():
    no_users = q("SELECT COUNT(*) c FROM users", one=True)["c"] == 0
    if request.method == "POST":
        ip = request.remote_addr or "?"
        email = request.form.get("email", "").strip()
        if _too_many_failures(ip) or _too_many_failures(email.lower()):
            flash("Too many failed attempts. Wait 15 minutes and try again.", "error")
            return render_template("login.html", no_users=no_users), 429
        u = q("SELECT * FROM users WHERE email=? AND active=1", (email,), one=True)
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["uid"] = u["id"]
            nxt = request.args.get("next", "")
            if not nxt.startswith("/") or nxt.startswith("//"):
                nxt = url_for("dashboard")
            return redirect(nxt)
        _login_failures[ip].append(time.time())
        _login_failures[email.lower()].append(time.time())
        flash("Wrong email or password.", "error")
    return render_template("login.html", no_users=no_users)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------


@app.route("/")
@login_required()
def dashboard():
    u = current_user()
    where, args = ["1=1"], []
    if not sees_all():
        where.append("(i.created_by=? OR i.id IN (SELECT invoice_id FROM invoice_shares WHERE user_id=?))")
        args += [u["id"], u["id"]]
    base_where, base_args = list(where), list(args)

    f = request.args.get("f", "open")
    day = today()
    open_in = ",".join(f"'{st}'" for st in L.OPEN_STATUSES)
    if f == "open":
        where.append(f"i.status IN ('{L.DRAFT}',{open_in})")
    elif f == "overdue":
        where.append(f"i.status IN ({open_in}) AND i.due_date < ?")
        args.append(day.isoformat())
    elif f in STATUSES:
        where.append("i.status=?")
        args.append(f)
    search = request.args.get("q", "").strip()
    if search:
        where.append("(i.number LIKE ? OR i.client_name LIKE ? OR i.client_email LIKE ?)")
        args += [f"%{search}%"] * 3

    invoices = q(f"""SELECT i.*, u.name AS artist_name FROM invoices i LEFT JOIN users u ON u.id=i.created_by
                    WHERE {' AND '.join(where)} ORDER BY i.id DESC LIMIT 500""", args)
    bw = " AND ".join(base_where)
    stats = {
        "outstanding": q(f"SELECT COALESCE(SUM(MAX(total_cents-paid_cents,0)),0) v FROM invoices i WHERE {bw} AND status IN ({open_in})", base_args, one=True)["v"],
        "overdue": q(f"SELECT COUNT(*) v FROM invoices i WHERE {bw} AND status IN ({open_in}) AND due_date < ?", base_args + [day.isoformat()], one=True)["v"],
        "drafts": q(f"SELECT COUNT(*) v FROM invoices i WHERE {bw} AND status='draft'", base_args, one=True)["v"],
        "paid_month": q(f"""SELECT COALESCE(SUM(p.amount_cents),0) v FROM payments p JOIN invoices i ON i.id=p.invoice_id
                           WHERE {bw} AND p.paid_on >= ?""", base_args + [day.replace(day=1).isoformat()], one=True)["v"],
    }
    return render_template("dashboard.html", invoices=invoices, stats=stats, f=f, search=search)


# --------------------------------------------------------------------------
# Invoice create / edit
# --------------------------------------------------------------------------


def parse_invoice_form(form):
    errors = []
    data = {
        "client_name": form.get("client_name", "").strip(),
        "client_email": form.get("client_email", "").strip(),
        "client_phone": form.get("client_phone", "").strip(),
        "client_address": form.get("client_address", "").strip(),
        "issue_date": form.get("issue_date", "").strip(),
        "due_date": form.get("due_date", "").strip(),
        "tax_name": form.get("tax_name", "").strip(),
        "tax_rate": form.get("tax_rate", "0").strip() or "0",
        "notes": form.get("notes", "").strip(),
        "terms": form.get("terms", "").strip(),
        "number": form.get("number", "").strip(),
        "discount": form.get("discount", "").strip(),
        "etransfer_email": "",
    }
    data["etransfer_email"], data["etransfer_is_new"] = chosen_etransfer(form)
    if data["etransfer_is_new"] and not data["etransfer_email"]:
        errors.append("Type the new e-Transfer email, or pick one from the list.")
    if data["etransfer_email"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", data["etransfer_email"]):
        errors.append("e-Transfer email doesn't look right.")
    if not data["client_name"]:
        errors.append("Client name is required.")
    if data["client_email"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", data["client_email"]):
        errors.append("Client email doesn't look right.")
    for k in ("issue_date", "due_date"):
        try:
            datetime.strptime(data[k], "%Y-%m-%d")
        except ValueError:
            errors.append(f"{'Issue' if k == 'issue_date' else 'Due'} date is missing or invalid.")
    try:
        rate = to_decimal(data["tax_rate"], "tax rate")
        if rate < 0 or rate > 100:
            raise ValueError("Tax rate must be between 0 and 100.")
        data["tax_rate"] = str(rate)
    except ValueError as e:
        errors.append(str(e))
    try:
        data["discount_cents"] = max(0, to_cents(data["discount"]))
    except ValueError as e:
        errors.append(f"Discount: {e}")
        data["discount_cents"] = 0

    items, item_errors = parse_items(form)
    errors += item_errors
    data["save_client"] = form.get("save_client") == "1"
    return data, items, errors


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_items(form):
    """Line items from the shared editor. Returns (items, errors)."""
    items, errors = [], []
    descs = form.getlist("item_desc")
    qtys = form.getlist("item_qty")
    prices = form.getlist("item_price")
    taxables = form.getlist("item_taxable")
    for idx, desc in enumerate(descs):
        desc = desc.strip()
        qty_s = qtys[idx] if idx < len(qtys) else ""
        price_s = prices[idx] if idx < len(prices) else ""
        taxable = (taxables[idx] if idx < len(taxables) else "1") == "1"
        if not desc and not price_s.strip():
            continue
        row = {"description": desc, "qty": qty_s, "price": price_s, "taxable": taxable}
        try:
            if not desc:
                raise ValueError("needs a description")
            qty = to_decimal(qty_s or "1", "quantity")
            unit = to_cents(price_s)
            row.update(qty=str(qty), unit_cents=unit,
                       amount_cents=int((qty * unit).quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
        except ValueError as e:
            errors.append(f"Line {idx + 1}: {e}")
            row.update(unit_cents=0, amount_cents=0)
        items.append(row)
    if not items:
        errors.append("Add at least one line item.")
    return items, errors


def client_map():
    """Shared client list for prefilling: name -> contact details."""
    return {r["name"]: {"id": r["id"], "email": r["email"] or "", "phone": r["phone"] or "",
                        "address": r["address"] or ""}
            for r in q("SELECT * FROM clients ORDER BY name COLLATE NOCASE LIMIT 5000")}


def catalog_items():
    return q("SELECT * FROM catalog ORDER BY name COLLATE NOCASE")


def _form_context(inv, items, is_new):
    return dict(inv=inv, items=items, is_new=is_new, clients=client_map(), catalog=catalog_items())


@app.route("/invoices/new", methods=["GET", "POST"])
@login_required()
def invoice_new():
    s = settings()
    if request.method == "POST":
        data, items, errors = parse_invoice_form(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("invoice_form.html", **_form_context(data, items, True)), 400
        iid = save_invoice(None, data, items)
        _remember_new_etransfer(data)
        flash("Invoice created.", "ok")
        return redirect(url_for("invoice_view", iid=iid))
    day = today()
    c = q("SELECT * FROM clients WHERE id=?", (request.args.get("client_id", type=int),), one=True)
    inv = {
        "client_name": c["name"] if c else "", "client_email": (c["email"] if c else "") or "",
        "client_phone": (c["phone"] if c else "") or "", "client_address": (c["address"] if c else "") or "",
        "issue_date": day.isoformat(),
        "due_date": (day + timedelta(days=int(s["default_due_days"] or 0))).isoformat(),
        "tax_name": s["tax_name"], "tax_rate": s["tax_rate"], "notes": s["default_notes"], "terms": s["default_terms"],
        "etransfer_email": s["etransfer_email"],
        "number": "", "discount": "",
    }
    items = [{"description": "", "qty": "1", "price": "", "taxable": True}]
    return render_template("invoice_form.html", **_form_context(inv, items, True))


def _remember_new_etransfer(data):
    if data.get("etransfer_is_new") and remember_etransfer(data["etransfer_email"]):
        flash(f"{data['etransfer_email']} was added to the e-Transfer email list.", "ok")


def refuse(iid, err):
    flash(str(err), "error")
    return redirect(url_for("invoice_view", iid=iid))


@app.route("/invoices/<int:iid>/edit", methods=["GET", "POST"])
@login_required()
def invoice_edit(iid):
    inv = get_invoice(iid)
    try:
        L.require(state(inv), actor(), L.EDIT)
    except L.LifecycleError as e:
        return refuse(iid, e)
    if request.method == "POST":
        data, items, errors = parse_invoice_form(request.form)
        if data["number"] and data["number"] != inv["number"]:
            if q("SELECT 1 FROM invoices WHERE number=? AND id<>?", (data["number"], iid), one=True):
                errors.append(f"Invoice number {data['number']} is already used.")
        new_status = inv["status"]
        if not errors:
            try:
                total = compute_totals(items, data["tax_rate"], data["discount_cents"])["total"]
                new_status = L.after_edit(state(inv), total)
            except L.LifecycleError as e:
                errors.append(str(e))
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("invoice_form.html", **_form_context(dict(data, id=iid), items, False)), 400
        save_invoice(iid, data, items, old=inv, new_status=new_status)
        _remember_new_etransfer(data)
        flash("Invoice saved.", "ok")
        return redirect(url_for("invoice_view", iid=iid))
    items = [{"description": r["description"], "qty": fmt_qty(r["qty"]), "price": plain_money(r["unit_cents"]),
              "taxable": bool(r["taxable"])} for r in q("SELECT * FROM items WHERE invoice_id=? ORDER BY position", (iid,))]
    data = dict(inv)
    data["discount"] = plain_money(inv["discount_cents"]) if inv["discount_cents"] else ""
    return render_template("invoice_form.html", **_form_context(data, items, False))


def _next_number(con):
    row = con.execute("SELECT value FROM settings WHERE key='next_number'").fetchone()
    nxt = int(row[0]) if row else int(DEFAULTS["next_number"])
    prefix = settings()["invoice_prefix"]
    number = f"{prefix}{nxt}"
    while con.execute("SELECT 1 FROM invoices WHERE number=?", (number,)).fetchone():
        nxt += 1
        number = f"{prefix}{nxt}"
    con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('next_number',?)", (str(nxt + 1),))
    return number


def _client_id_for(con, data, user_id):
    """Link to a saved client by name; add a new one if asked."""
    row = con.execute("SELECT id FROM clients WHERE name=? COLLATE NOCASE", (data["client_name"],)).fetchone()
    if row:
        return row[0]
    if data.get("save_client"):
        ts = now_iso()
        cur = con.execute("INSERT INTO clients(name,email,phone,address,created_by,created_at,updated_at) "
                          "VALUES(?,?,?,?,?,?,?)", (data["client_name"], data["client_email"], data["client_phone"],
                                                    data["client_address"], user_id, ts, ts))
        return cur.lastrowid
    return None


def _insert_invoice(con, data, items, owner_id, status, created_event, recurring_id=None, period_start=None):
    """Insert a new invoice and its line items inside the caller's transaction."""
    totals = compute_totals(items, data["tax_rate"], data["discount_cents"])
    ts = now_iso()
    sent_at = ts if status in (L.SENT, L.PARTIAL, L.PAID) else None
    cur = con.execute(
        """INSERT INTO invoices(number,token,status,client_name,client_email,client_phone,client_address,client_id,
           issue_date,due_date,tax_name,tax_rate,discount_cents,notes,terms,subtotal_cents,tax_cents,total_cents,
           etransfer_email,created_by,created_at,updated_at,sent_at,paid_at,recurring_id,period_start)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_next_number(con), secrets.token_urlsafe(18), status, data["client_name"], data["client_email"],
         data["client_phone"], data["client_address"], data.get("client_id"), data["issue_date"], data["due_date"],
         data["tax_name"], data["tax_rate"], totals["discount"], data["notes"], data["terms"], totals["subtotal"],
         totals["tax"], totals["total"], data.get("etransfer_email") or None, owner_id, ts, ts, sent_at,
         ts if status == L.PAID else None, recurring_id, period_start))
    iid = cur.lastrowid
    _insert_items(con, "items", "invoice_id", iid, items, with_amount=True)
    add_event(iid, created_event, con)
    if status == L.PAID:
        add_event(iid, "Paid in full", con)
    return iid


def _insert_items(con, table, key, owner_id, items, with_amount):
    for pos, it in enumerate(items):
        cols = f"{key},position,description,qty,unit_cents,taxable" + (",amount_cents" if with_amount else "")
        vals = [owner_id, pos, it["description"], it["qty"], it["unit_cents"], 1 if it["taxable"] else 0]
        if with_amount:
            vals.append(it["amount_cents"])
        con.execute(f"INSERT INTO {table}({cols}) VALUES({','.join('?' * len(vals))})", vals)


def save_invoice(iid, data, items, old=None, new_status=L.DRAFT):
    """Create (iid=None) or update an invoice and its line items in one transaction."""
    totals = compute_totals(items, data["tax_rate"], data["discount_cents"])
    con = db()
    ts = now_iso()
    user = current_user()
    with con:
        data["client_id"] = _client_id_for(con, data, user["id"])
        if iid is None:
            iid = _insert_invoice(con, data, items, user["id"], L.DRAFT, f"Created by {user['name']}")
        else:
            number = data["number"] or old["number"]
            con.execute(
                """UPDATE invoices SET number=?,client_name=?,client_email=?,client_phone=?,client_address=?,client_id=?,
                   issue_date=?,due_date=?,tax_name=?,tax_rate=?,discount_cents=?,notes=?,terms=?,subtotal_cents=?,
                   tax_cents=?,total_cents=?,etransfer_email=?,updated_at=? WHERE id=?""",
                (number, data["client_name"], data["client_email"], data["client_phone"], data["client_address"],
                 data["client_id"], data["issue_date"], data["due_date"], data["tax_name"], data["tax_rate"],
                 totals["discount"], data["notes"], data["terms"], totals["subtotal"], totals["tax"], totals["total"],
                 data.get("etransfer_email") or None, ts, iid))
            con.execute("DELETE FROM items WHERE invoice_id=?", (iid,))
            _insert_items(con, "items", "invoice_id", iid, items, with_amount=True)
            change = (f" (total {money(old['total_cents'])} → {money(totals['total'])})"
                      if old["total_cents"] != totals["total"] else "")
            add_event(iid, f"Edited by {user['name']}{change}", con)
            set_status(con, old, new_status)
    g.pop("settings", None)
    return iid


def set_status(con, inv, new_status, event=None, paid_delta=0):
    """Write a status decided by the lifecycle module, inside the caller's transaction."""
    ts = now_iso()
    con.execute(
        """UPDATE invoices SET status=?, paid_cents=paid_cents+?,
             sent_at=CASE WHEN ? IN ('sent','partial','paid') THEN COALESCE(sent_at, ?) ELSE sent_at END,
             paid_at=CASE WHEN ?='paid' THEN COALESCE(paid_at, ?) ELSE NULL END
           WHERE id=?""",
        (new_status, paid_delta, new_status, ts, new_status, ts, inv["id"]))
    if event:
        add_event(inv["id"], event, con)
    if new_status == L.PAID and inv["status"] != L.PAID:
        add_event(inv["id"], "Paid in full", con)


# --------------------------------------------------------------------------
# Invoice view & actions
# --------------------------------------------------------------------------


def invoice_bundle(iid):
    items = q("SELECT * FROM items WHERE invoice_id=? ORDER BY position", (iid,))
    payments = q("SELECT p.*, u.name AS by_name FROM payments p LEFT JOIN users u ON u.id=p.created_by "
                 "WHERE invoice_id=? ORDER BY paid_on, id", (iid,))
    return items, payments


@app.route("/invoices/<int:iid>")
@login_required()
def invoice_view(iid):
    inv = get_invoice(iid)
    items, payments = invoice_bundle(iid)
    events = q("SELECT * FROM events WHERE invoice_id=? ORDER BY id DESC LIMIT 50", (iid,))
    share_ids = shared_with(iid)
    shared_people = [r["name"] for r in q("SELECT name FROM users WHERE id IN (%s) ORDER BY name"
                                          % ",".join("?" * len(share_ids)), tuple(share_ids))] if share_ids else []
    ad_users = q("SELECT id, name FROM users WHERE role=? AND active=1 AND id<>? ORDER BY name",
                 (L.ADVERTISING, inv["created_by"] or 0))
    return render_template("invoice_view.html", inv=inv, items=items, payments=payments, events=events,
                           smtp=smtp_ready(), today=today().isoformat(), acts=allowed(inv),
                           share_ids=share_ids, shared_people=shared_people, ad_users=ad_users)


@app.post("/invoices/<int:iid>/share")
@login_required()
def invoice_share(iid):
    inv = get_invoice(iid)
    try:
        L.require(state(inv), actor(), L.SHARE)
    except L.LifecycleError as e:
        return refuse(iid, e)
    allowed_ids = {r["id"]: r["name"] for r in q("SELECT id, name FROM users WHERE role=? AND active=1",
                                                  (L.ADVERTISING,))}
    wanted = {int(v) for v in request.form.getlist("user_id") if v.isdigit() and int(v) in allowed_ids}
    wanted.discard(inv["created_by"])
    current = set(shared_with(iid))
    added, removed = wanted - current, current - wanted
    who = current_user()["name"]
    with db() as con:
        for uid in added:
            con.execute("INSERT OR IGNORE INTO invoice_shares(invoice_id,user_id,shared_by,created_at) VALUES(?,?,?,?)",
                        (iid, uid, current_user()["id"], now_iso()))
        for uid in removed:
            con.execute("DELETE FROM invoice_shares WHERE invoice_id=? AND user_id=?", (iid, uid))
        if added:
            add_event(iid, f"Shared with {', '.join(sorted(allowed_ids[u] for u in added))} by {who}", con)
        if removed:
            names = sorted(allowed_ids.get(u) or q("SELECT name FROM users WHERE id=?", (u,), one=True)["name"]
                           for u in removed)
            add_event(iid, f"Stopped sharing with {', '.join(names)} by {who}", con)
    flash("Sharing updated." if added or removed else "No changes to sharing.", "ok")
    return redirect(url_for("invoice_view", iid=iid))


STATUS_ACTIONS = {
    "sent": (L.SEND, L.after_send, "Marked as sent"),
    "void": (L.VOID_IT, L.after_void, "Voided"),
    "reopen": (L.REOPEN, L.after_reopen, "Moved back to draft"),
}


@app.post("/invoices/<int:iid>/status")
@login_required()
def invoice_status(iid):
    inv = get_invoice(iid)
    if request.form.get("action") not in STATUS_ACTIONS:
        abort(400)
    action, after, label = STATUS_ACTIONS[request.form["action"]]
    try:
        L.require(state(inv), actor(), action)
        new_status = after(state(inv))
    except L.LifecycleError as e:
        return refuse(iid, e)
    with db() as con:
        set_status(con, inv, new_status, f"{label} by {current_user()['name']}")
    flash(f"{label}.", "ok")
    return redirect(url_for("invoice_view", iid=iid))


@app.post("/invoices/<int:iid>/payments")
@login_required()
def payment_add(iid):
    inv = get_invoice(iid)
    try:
        L.require(state(inv), actor(), L.RECORD_PAYMENT)
        amount = to_cents(request.form.get("amount"))
        new_status = L.after_payment(state(inv), amount)
        paid_on = request.form.get("paid_on") or today().isoformat()
        datetime.strptime(paid_on, "%Y-%m-%d")
    except (L.LifecycleError, ValueError) as e:
        return refuse(iid, f"Payment not saved: {e}")
    method = request.form.get("method") if request.form.get("method") in PAY_METHODS else "Other"
    with db() as con:
        con.execute("""INSERT INTO payments(invoice_id,method,amount_cents,paid_on,reference,note,created_by,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (iid, method, amount, paid_on, request.form.get("reference", "").strip(),
                     request.form.get("note", "").strip(), current_user()["id"], now_iso()))
        set_status(con, inv, new_status, f"{money(amount)} {method} payment recorded by {current_user()['name']}",
                   paid_delta=amount)
    flash(f"{money(amount)} payment recorded.", "ok")
    return redirect(url_for("invoice_view", iid=iid))


@app.post("/invoices/<int:iid>/payments/<int:pid>/delete")
@login_required()
def payment_delete(iid, pid):
    inv = get_invoice(iid)
    p = q("SELECT * FROM payments WHERE id=? AND invoice_id=?", (pid, iid), one=True)
    if not p:
        abort(404)
    try:
        L.require(state(inv), actor(), L.REMOVE_PAYMENT)
    except L.LifecycleError as e:
        return refuse(iid, e)
    new_status = L.after_payment_removed(state(inv), p["amount_cents"])
    with db() as con:
        con.execute("DELETE FROM payments WHERE id=?", (pid,))
        set_status(con, inv, new_status,
                   f"{money(p['amount_cents'])} {p['method']} payment removed by {current_user()['name']}",
                   paid_delta=-p["amount_cents"])
    flash("Payment removed.", "ok")
    return redirect(url_for("invoice_view", iid=iid))


@app.post("/invoices/<int:iid>/duplicate")
@login_required()
def invoice_duplicate(iid):
    inv = get_invoice(iid)
    s = settings()
    day = today()
    data = {k: inv[k] for k in ("client_name", "client_email", "client_phone", "client_address", "tax_name",
                                 "tax_rate", "notes", "terms", "etransfer_email")}
    data.update(issue_date=day.isoformat(),
                due_date=(day + timedelta(days=int(s["default_due_days"] or 0))).isoformat(),
                discount_cents=inv["discount_cents"], number="", save_client=False)
    items = [{"description": r["description"], "qty": r["qty"], "unit_cents": r["unit_cents"],
              "taxable": bool(r["taxable"]), "amount_cents": r["amount_cents"]}
             for r in q("SELECT * FROM items WHERE invoice_id=? ORDER BY position", (iid,))]
    new_id = save_invoice(None, data, items)
    flash(f"Copied {inv['number']} into a new draft.", "ok")
    return redirect(url_for("invoice_edit", iid=new_id))


@app.post("/invoices/<int:iid>/delete")
@login_required()
def invoice_delete(iid):
    inv = get_invoice(iid)
    try:
        L.require(state(inv), actor(), L.DELETE)
    except L.LifecycleError as e:
        return refuse(iid, e)
    x("DELETE FROM invoices WHERE id=?", (iid,))
    flash(f"Invoice {inv['number']} deleted.", "ok")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------


def smtp_ready():
    c = smtp_config()
    return bool(c["host"] and c["from"])


def send_email(to, subject, body, reply_to=None):
    msg = EmailMessage()
    cfg = smtp_config()
    msg["From"] = cfg["from"]
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    host = cfg["host"]
    port = int(cfg["port"] or 587)
    security = (cfg["security"] or "starttls").lower()
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        if security == "starttls":
            server.starttls()
    try:
        if cfg["user"]:
            server.login(cfg["user"], cfg["password"])
        server.send_message(msg)
    finally:
        server.quit()


@app.post("/invoices/<int:iid>/email")
@login_required()
def invoice_email(iid):
    inv = get_invoice(iid)
    try:
        L.require(state(inv), actor(), L.EMAIL)
    except L.LifecycleError as e:
        return refuse(iid, e)
    to = request.form.get("to", "").strip() or inv["client_email"]
    if not to:
        flash("Add a client email first.", "error")
        return redirect(url_for("invoice_view", iid=iid))
    if not smtp_ready():
        flash("Email sending isn't set up (SMTP settings). Use 'Open in email app' instead.", "error")
        return redirect(url_for("invoice_view", iid=iid))
    s = settings()
    try:
        send_email(to, fill_template(s["email_subject"], inv), fill_template(s["email_body"], inv),
                   reply_to=s["business_email"] or None)
    except Exception as e:  # noqa: BLE001
        log.exception("Email failed")
        flash(f"Email failed: {e}", "error")
        return redirect(url_for("invoice_view", iid=iid))
    with db() as con:
        new_status = L.after_send(state(inv)) if inv["status"] == L.DRAFT else inv["status"]
        set_status(con, inv, new_status, f"Emailed to {to} by {current_user()['name']}")
    flash(f"Invoice emailed to {to}.", "ok")
    return redirect(url_for("invoice_view", iid=iid))


# --------------------------------------------------------------------------
# Public (client-facing) invoice page + Helcim
# --------------------------------------------------------------------------


def public_invoice_or_404(token):
    inv = q("SELECT i.*, u.name AS artist_name FROM invoices i LEFT JOIN users u ON u.id=i.created_by WHERE token=?",
            (token,), one=True)
    if not inv:
        abort(404)
    return inv


def helcim_ready():
    return bool(helcim_token()) and settings()["helcim_enabled"] == "1"


app.jinja_env.globals["helcim_ready"] = helcim_ready


@app.route("/i/<token>")
def public_invoice(token):
    inv = public_invoice_or_404(token)
    preview = can_see(inv)
    if inv["status"] == L.DRAFT and not preview:
        return render_template("public_invoice.html", inv=inv, updating=True, public=True)
    if not current_user() and not inv["viewed_at"]:
        x("UPDATE invoices SET viewed_at=? WHERE id=?", (now_iso(), inv["id"]))
        add_event(inv["id"], "Client opened the invoice link")
    items, payments = invoice_bundle(inv["id"])
    return render_template("public_invoice.html", inv=inv, items=items, payments=payments, preview=preview,
                           just_paid=request.args.get("paid") == "1", bank_pending=request.args.get("pending") == "1",
                           public=True)


def helcim_headers():
    return {"accept": "application/json", "content-type": "application/json", "api-token": helcim_token()}


@app.post("/i/<token>/helcim/start")
def helcim_start(token):
    inv = public_invoice_or_404(token)
    if not helcim_ready():
        return jsonify(error="Card payments are not available."), 404
    if not L.client_can_pay_online(state(inv)):
        return jsonify(error="This invoice can't be paid online right now."), 400
    due = L.balance(state(inv))
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    recent = q("SELECT COUNT(*) c FROM helcim_sessions WHERE invoice_id=? AND created_at>?", (inv["id"], hour_ago),
               one=True)["c"]
    if recent >= 20:
        return jsonify(error="Too many attempts. Please try again later."), 429
    payload = {
        "paymentType": "purchase",
        "amount": float(Decimal(due) / 100),
        "currency": settings()["currency"] or "CAD",
        "language": "en",
        "confirmationScreen": False,
    }
    if settings()["helcim_fee"] == "1":
        # Helcim Fee Saver: card pays the fee; the bank (ACH) option is the required no-fee choice
        payload["hasConvenienceFee"] = 1
        payload["paymentMethod"] = "cc-ach"
    unavailable = "Card payments are unavailable right now. Please use e-Transfer or try again later."
    # Errors use 400, not 502/504: Cloudflare replaces those with its own web page.
    try:
        r, body = _helcim_initialize(payload)
        if r.status_code == 400 and payload.get("hasConvenienceFee") and "convenience fee" in str(body).lower():
            # Helcim only allows the fee when bank (ACH) payments are also on. Take the card without it.
            log.warning("Helcim refused the convenience fee (%s); opening checkout without it", str(body)[:200])
            add_event(inv["id"], "Card checkout opened without the convenience fee: turn on Online Fee Saver and "
                                 "ACH bank payments for invoicing in Helcim's settings")
            payload.pop("hasConvenienceFee")
            payload.pop("paymentMethod", None)
            r, body = _helcim_initialize(payload)
    except Exception:  # noqa: BLE001
        log.exception("Helcim initialize failed")
        return jsonify(error=unavailable), 400
    if r.status_code >= 300 or "checkoutToken" not in body:
        log.error("Helcim initialize HTTP %s: %s", r.status_code, str(body)[:500])
        return jsonify(error=unavailable), 400
    x("INSERT INTO helcim_sessions(invoice_id,checkout_token,secret_token,amount_cents,created_at) VALUES(?,?,?,?,?)",
      (inv["id"], body["checkoutToken"], body["secretToken"], due, now_iso()))
    return jsonify(checkoutToken=body["checkoutToken"])


def _helcim_initialize(payload):
    r = requests.post(f"{HELCIM_API}/helcim-pay/initialize", json=payload, headers=helcim_headers(), timeout=20)
    try:
        body = r.json() if r.content else {}
    except ValueError:
        body = {"raw": r.text[:300]}
    return r, body


def _find_txn(obj, depth=0):
    """Find the transaction dict + hash in a HelcimPay.js eventMessage (shape varies)."""
    if depth > 4:
        return None, None
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            return None, None
    if isinstance(obj, dict):
        d = obj.get("data")
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except ValueError:
                d = None
        if isinstance(d, dict):
            if "transactionId" in d:
                return d, obj.get("hash")
            found = _find_txn(d, depth + 1)
            if found[0]:
                return found
        if "transactionId" in obj:
            return obj, None
    return None, None


def _hash_ok(txn, given_hash, secret):
    if not given_hash or not txn:
        return False
    compact = json.dumps(txn, separators=(",", ":"))
    candidates = {compact, compact.replace("/", "\\/"), json.dumps(txn, separators=(",", ":"), ensure_ascii=False),
                  json.dumps(txn, separators=(",", ":"), ensure_ascii=False).replace("/", "\\/")}
    for c in candidates:
        if hmac.compare_digest(hashlib.sha256((c + secret).encode("utf-8")).hexdigest(), str(given_hash)):
            return True
    return False


def helcim_get_transaction(tid, ach=False):
    try:
        path = "ach/transactions" if ach else "card-transactions"
        r = requests.get(f"{HELCIM_API}/{path}/{int(tid)}", headers=helcim_headers(), timeout=20)
        if r.ok:
            j = r.json()
            if isinstance(j, list):
                j = j[0] if j else None
            return j if isinstance(j, dict) else None
        log.warning("Helcim lookup %s -> HTTP %s %s", tid, r.status_code, r.text[:300])
    except Exception:  # noqa: BLE001
        log.exception("Helcim transaction lookup failed")
    return None


@app.post("/i/<token>/helcim/confirm")
def helcim_confirm(token):
    inv = public_invoice_or_404(token)
    body = request.get_json(silent=True) or {}
    sess = q("SELECT * FROM helcim_sessions WHERE checkout_token=? AND invoice_id=?",
             (str(body.get("checkoutToken", "")), inv["id"]), one=True)
    fail_msg = ("We couldn't confirm the payment automatically. If your card was charged, don't pay again - "
                "we'll confirm it on our side.")
    if not sess:
        return jsonify(error="Payment session not found. Please reload and try again."), 400
    if sess["used"]:
        return jsonify(ok=True)
    txn, given_hash = _find_txn(body.get("eventMessage"))
    if not txn or not txn.get("transactionId"):
        log.error("Helcim confirm: no transaction in message for %s", inv["number"])
        return jsonify(error=fail_msg), 400
    tid = str(txn["transactionId"])
    if q("SELECT 1 FROM payments WHERE helcim_transaction_id=?", (tid,), one=True):
        x("UPDATE helcim_sessions SET used=1 WHERE id=?", (sess["id"],))
        return jsonify(ok=True)

    hash_ok = _hash_ok(txn, given_hash, sess["secret_token"])
    is_bank = _is_bank_txn(txn)
    api_txn = helcim_get_transaction(tid, ach=is_bank)
    source = api_txn or (txn if hash_ok else None)
    if not source:
        add_event(inv["id"], f"Card payment attempt (Helcim transaction {tid}) could not be verified - check Helcim")
        return jsonify(error=fail_msg), 400
    try:
        charged = to_cents(source.get("amount"))
    except ValueError:
        charged = 0
    if is_bank:
        return _record_bank_payment(inv, sess, tid, source, charged, verified=bool(api_txn))
    status = str(source.get("status", "")).upper()
    if status != "APPROVED" or charged <= 0:
        add_event(inv["id"], f"Helcim transaction {tid} not approved (status {status or 'unknown'})")
        return jsonify(error="The payment was not approved."), 400
    mismatch = _txn_mismatch(source, charged, sess)
    if mismatch:
        log.warning("Helcim txn %s rejected for %s: %s", tid, inv["number"], mismatch)
        add_event(inv["id"], f"Card payment claim rejected (Helcim #{tid}): {mismatch} - check Helcim")
        return jsonify(error=fail_msg), 400

    fee_on = settings()["helcim_fee"] == "1"
    out = L.after_card_payment(state(inv), charged, fee_expected=fee_on)
    notes = []
    if out.extra_cents:
        if fee_on and not out.needs_review:
            notes.append(f"Card charged {money(charged)} incl. {money(out.extra_cents)} convenience fee")
        else:
            notes.append(f"Card charged {money(charged)}: {money(out.extra_cents)} above the balance")
    if not api_txn:
        notes.append("verified by hash only")
    event = f"Client paid {money(out.applied_cents)} by card (Helcim #{tid})"
    if out.needs_review:
        reason = "invoice was void" if inv["status"] == L.VOID else f"{money(out.extra_cents)} above the balance"
        event += f" - check: may need a refund ({reason})"
    try:
        with db() as con:
            con.execute("""INSERT INTO payments(invoice_id,method,amount_cents,paid_on,reference,helcim_transaction_id,
                           note,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (inv["id"], "Helcim (card)", out.applied_cents, today().isoformat(),
                         f"Helcim #{tid}" + (f" / approval {source.get('approvalCode')}"
                                             if source.get("approvalCode") else ""),
                         tid, "; ".join(notes), now_iso()))
            con.execute("UPDATE helcim_sessions SET used=1 WHERE id=?", (sess["id"],))
            set_status(con, inv, out.status, event, paid_delta=out.applied_cents)
    except sqlite3.IntegrityError:
        return jsonify(ok=True)
    return jsonify(ok=True)


BANK_FAILED = ("DECLINED", "FAILED", "RETURNED", "REJECTED", "CANCELLED", "CANCELED", "VOID", "VOIDED", "REFUNDED")


def _is_bank_txn(txn):
    """HelcimPay.js bank (ACH) responses carry bank fields instead of card fields."""
    return "cardToken" not in txn and any(k in txn for k in ("bankToken", "bankAccountNumber", "statusAuth",
                                                                 "statusClearing"))


def _record_bank_payment(inv, sess, tid, source, charged, verified):
    """Bank (ACH) payments clear over a few business days. Record them only when Helcim says approved;
    otherwise log them for staff and tell the client it's on its way."""
    auth = str(source.get("statusAuth") or source.get("status") or "").upper()
    clearing = str(source.get("statusClearing") or "").upper()
    if charged <= 0 or auth in BANK_FAILED or clearing in BANK_FAILED:
        add_event(inv["id"], f"Bank payment (Helcim #{tid}) was not approved (status {auth or clearing or 'unknown'})")
        return jsonify(error="The bank payment was not approved."), 400
    mismatch = _txn_mismatch(source, charged, sess)
    if mismatch:
        add_event(inv["id"], f"Bank payment claim rejected (Helcim #{tid}): {mismatch} - check Helcim")
        return jsonify(error="We couldn't confirm the payment automatically. We'll check it on our side."), 400
    if auth != "APPROVED":
        x("UPDATE helcim_sessions SET used=1 WHERE id=?", (sess["id"],))
        add_event(inv["id"], f"Client started a {money(charged)} bank payment (Helcim #{tid}, status "
                             f"{auth or 'unknown'}). Record it with 'Helcim (bank)' once it clears in Helcim")
        return jsonify(ok=True, pending=True)
    out = L.after_card_payment(state(inv), charged, fee_expected=False)
    note = "Bank payment: can take a few business days to clear. If Helcim shows it returned, remove this payment"
    if not verified:
        note += "; verified by hash only"
    event = f"Client paid {money(out.applied_cents)} by bank payment (Helcim #{tid})"
    if out.needs_review:
        event += " - check: may need a refund"
    try:
        with db() as con:
            con.execute("""INSERT INTO payments(invoice_id,method,amount_cents,paid_on,reference,helcim_transaction_id,
                           note,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (inv["id"], "Helcim (bank)", out.applied_cents, today().isoformat(), f"Helcim #{tid}",
                         tid, note, now_iso()))
            con.execute("UPDATE helcim_sessions SET used=1 WHERE id=?", (sess["id"],))
            set_status(con, inv, out.status, event, paid_delta=out.applied_cents)
    except sqlite3.IntegrityError:
        pass
    return jsonify(ok=True)


def _txn_mismatch(txn, charged_cents, sess):
    """Why a Helcim transaction can't belong to this checkout session, or '' if it can."""
    if charged_cents < sess["amount_cents"]:
        return f"charged {money(charged_cents)}, checkout was for {money(sess['amount_cents'])}"
    created = str(txn.get("dateCreated") or "")[:19]
    if created:
        try:
            txn_at = datetime.strptime(created, "%Y-%m-%d %H:%M:%S")
            opened = datetime.strptime(sess["created_at"], "%Y-%m-%d %H:%M:%S")
            # Helcim's timestamp time zone isn't documented: allow a day of slack
            if txn_at < opened - timedelta(days=1):
                return f"transaction from {created}, before this checkout started"
        except ValueError:
            pass
    return ""


# --------------------------------------------------------------------------
# Clients (shared list: everyone adds, Admin + Studio Manager edit/delete)
# --------------------------------------------------------------------------


def _client_form(form):
    data = {k: form.get(k, "").strip() for k in ("name", "email", "phone", "address", "notes")}
    errors = []
    if not data["name"]:
        errors.append("Client name is required.")
    if data["email"] and not EMAIL_RE.match(data["email"]):
        errors.append("Client email doesn't look right.")
    return data, errors


@app.route("/clients", methods=["GET", "POST"])
@login_required()
def clients_page():
    if request.method == "POST":
        data, errors = _client_form(request.form)
        if not errors and q("SELECT 1 FROM clients WHERE name=? COLLATE NOCASE", (data["name"],), one=True):
            errors.append(f"{data['name']} is already in the client list.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            ts = now_iso()
            x("INSERT INTO clients(name,email,phone,address,notes,created_by,created_at,updated_at) "
              "VALUES(?,?,?,?,?,?,?,?)", (data["name"], data["email"], data["phone"], data["address"], data["notes"],
                                          current_user()["id"], ts, ts))
            flash(f"{data['name']} added to clients.", "ok")
        return redirect(url_for("clients_page"))
    search = request.args.get("q", "").strip()
    where, args = "", []
    if search:
        where = "WHERE c.name LIKE ? OR c.email LIKE ? OR c.phone LIKE ?"
        args = [f"%{search}%"] * 3
    rows = q(f"""SELECT c.*, (SELECT COUNT(*) FROM invoices i WHERE i.client_id=c.id) AS invoice_count,
                        (SELECT COUNT(*) FROM recurring r WHERE r.client_id=c.id AND r.status!='ended') AS plan_count
                 FROM clients c {where} ORDER BY c.name COLLATE NOCASE LIMIT 1000""", args)
    return render_template("clients.html", clients=rows, search=search)


@app.route("/clients/<int:cid>/edit", methods=["GET", "POST"])
@login_required(*SEE_ALL_ROLES)
def client_edit(cid):
    c = q("SELECT * FROM clients WHERE id=?", (cid,), one=True)
    if not c:
        abort(404)
    if request.method == "POST":
        data, errors = _client_form(request.form)
        if not errors and q("SELECT 1 FROM clients WHERE name=? COLLATE NOCASE AND id<>?", (data["name"], cid), one=True):
            errors.append(f"{data['name']} is already in the client list.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("client_edit.html", c=dict(c, **data)), 400
        x("UPDATE clients SET name=?,email=?,phone=?,address=?,notes=?,updated_at=? WHERE id=?",
          (data["name"], data["email"], data["phone"], data["address"], data["notes"], now_iso(), cid))
        flash("Client saved. Existing invoices keep the details they were sent with.", "ok")
        return redirect(url_for("clients_page"))
    return render_template("client_edit.html", c=c)


@app.post("/clients/<int:cid>/delete")
@login_required(*SEE_ALL_ROLES)
def client_delete(cid):
    c = q("SELECT * FROM clients WHERE id=?", (cid,), one=True)
    if not c:
        abort(404)
    if q("SELECT 1 FROM recurring WHERE client_id=? AND status!='ended'", (cid,), one=True):
        flash(f"{c['name']} has active monthly billing. Stop it first.", "error")
        return redirect(url_for("client_edit", cid=cid))
    with db() as con:
        con.execute("UPDATE invoices SET client_id=NULL WHERE client_id=?", (cid,))
        con.execute("DELETE FROM recurring_items WHERE recurring_id IN (SELECT id FROM recurring WHERE client_id=?)", (cid,))
        con.execute("DELETE FROM recurring WHERE client_id=?", (cid,))
        con.execute("DELETE FROM clients WHERE id=?", (cid,))
    flash(f"{c['name']} removed from clients. Their invoices are kept.", "ok")
    return redirect(url_for("clients_page"))


# --------------------------------------------------------------------------
# Saved line items (everyone can manage)
# --------------------------------------------------------------------------


def _catalog_form(form):
    errors = []
    data = {"name": form.get("name", "").strip(), "description": form.get("description", "").strip(),
            "taxable": 1 if form.get("taxable") else 0}
    if not data["name"]:
        errors.append("Give the saved item a name.")
    data["description"] = data["description"] or data["name"]
    try:
        data["qty"] = str(to_decimal(form.get("qty") or "1", "quantity"))
        data["unit_cents"] = max(0, to_cents(form.get("price")))
    except ValueError as e:
        errors.append(str(e))
    return data, errors


@app.route("/items", methods=["GET", "POST"])
@login_required()
def catalog_page():
    if request.method == "POST":
        data, errors = _catalog_form(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            ts = now_iso()
            x("INSERT INTO catalog(name,description,qty,unit_cents,taxable,created_by,created_at,updated_at) "
              "VALUES(?,?,?,?,?,?,?,?)", (data["name"], data["description"], data["qty"], data["unit_cents"],
                                          data["taxable"], current_user()["id"], ts, ts))
            flash(f"Saved item '{data['name']}' added.", "ok")
        return redirect(url_for("catalog_page"))
    return render_template("catalog.html", catalog=catalog_items())


@app.post("/items/<int:item_id>")
@login_required()
def catalog_update(item_id):
    if not q("SELECT 1 FROM catalog WHERE id=?", (item_id,), one=True):
        abort(404)
    if request.form.get("delete"):
        x("DELETE FROM catalog WHERE id=?", (item_id,))
        flash("Saved item removed.", "ok")
        return redirect(url_for("catalog_page"))
    data, errors = _catalog_form(request.form)
    if errors:
        for e in errors:
            flash(e, "error")
    else:
        x("UPDATE catalog SET name=?,description=?,qty=?,unit_cents=?,taxable=?,updated_at=? WHERE id=?",
          (data["name"], data["description"], data["qty"], data["unit_cents"], data["taxable"], now_iso(), item_id))
        flash(f"Saved item '{data['name']}' updated.", "ok")
    return redirect(url_for("catalog_page"))


# --------------------------------------------------------------------------
# Monthly billing (recurring invoices)
# --------------------------------------------------------------------------


def plan_or_404(rid):
    r = q("""SELECT r.*, c.name AS client_name, c.email AS client_email, u.name AS owner_name
             FROM recurring r JOIN clients c ON c.id=r.client_id LEFT JOIN users u ON u.id=r.created_by
             WHERE r.id=?""", (rid,), one=True)
    if not r:
        abort(404)
    if not (sees_all() or r["created_by"] == current_user()["id"]):
        abort(403)
    return r


def plan_items(rid):
    rows = q("SELECT * FROM recurring_items WHERE recurring_id=? ORDER BY position", (rid,))
    return [{"description": r["description"], "qty": r["qty"], "unit_cents": r["unit_cents"],
             "taxable": bool(r["taxable"]),
             "amount_cents": int((Decimal(r["qty"]) * r["unit_cents"]).quantize(Decimal("1"), rounding=ROUND_HALF_UP))}
            for r in rows]


def plan_total(r):
    return compute_totals(plan_items(r["id"]), r["tax_rate"], r["discount_cents"])["total"]


def plan_next_label(r):
    if r["status"] != "active":
        return ""
    nxt = date.fromisoformat(r["next_period_start"])
    if r["stop_after"] and nxt > date.fromisoformat(r["stop_after"]):
        return ""
    return f"{datefmt(nxt.isoformat())} ({B.period_label(r['anchor_day'], nxt)})"


app.jinja_env.globals.update(plan_total=plan_total, plan_next_label=plan_next_label,
                             schedule_label=B.schedule_label)


def _parse_plan_form(form):
    errors = []
    data = {k: form.get(k, "").strip() for k in ("tax_name", "notes", "terms",
                                                  "start_date", "stop_after", "discount")}
    data["client_id"] = form.get("client_id", type=int)
    data["etransfer_email"], data["etransfer_is_new"] = chosen_etransfer(form)
    if data["etransfer_is_new"] and not data["etransfer_email"]:
        errors.append("Type the new e-Transfer email, or pick one from the list.")
    data["tax_rate"] = form.get("tax_rate", "0").strip() or "0"
    if not data["client_id"] or not q("SELECT 1 FROM clients WHERE id=?", (data["client_id"],), one=True):
        errors.append("Choose a client from the client list.")
    try:
        data["start"] = date.fromisoformat(data["start_date"])
    except ValueError:
        errors.append("First billing date is missing or invalid.")
        data["start"] = None
    data["stop"] = None
    if data["stop_after"]:
        try:
            data["stop"] = date.fromisoformat(data["stop_after"])
        except ValueError:
            errors.append("Stop date is invalid.")
    if data["stop"] and data["start"] and data["stop"] < data["start"]:
        errors.append("The stop date is before the first billing date.")
    if data["etransfer_email"] and not EMAIL_RE.match(data["etransfer_email"]):
        errors.append("e-Transfer email doesn't look right.")
    try:
        rate = to_decimal(data["tax_rate"], "tax rate")
        if rate < 0 or rate > 100:
            raise ValueError("Tax rate must be between 0 and 100.")
        data["tax_rate"] = str(rate)
    except ValueError as e:
        errors.append(str(e))
    try:
        data["discount_cents"] = max(0, to_cents(data["discount"]))
    except ValueError as e:
        errors.append(f"Discount: {e}")
        data["discount_cents"] = 0
    items, item_errors = parse_items(form)
    return data, items, errors + item_errors


def _plan_form_context(plan, items, is_new, locked_start=False):
    return dict(plan=plan, items=items, is_new=is_new, locked_start=locked_start,
                clients=q("SELECT id, name, email FROM clients ORDER BY name COLLATE NOCASE"),
                catalog=catalog_items())


@app.route("/billing")
@login_required()
def plans_page():
    where, args = "", []
    if not sees_all():
        where, args = "WHERE r.created_by=?", [current_user()["id"]]
    plans = q(f"""SELECT r.*, c.name AS client_name, u.name AS owner_name FROM recurring r
                  JOIN clients c ON c.id=r.client_id LEFT JOIN users u ON u.id=r.created_by {where}
                  ORDER BY CASE r.status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END,
                           r.next_period_start""", args)
    return render_template("plans.html", plans=plans)


@app.route("/billing/new", methods=["GET", "POST"])
@login_required()
def plan_new():
    if request.method == "POST":
        data, items, errors = _parse_plan_form(request.form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("plan_form.html", **_plan_form_context(data, items, True)), 400
        ts = now_iso()
        first = B.first_period_start(data["start"], today())
        with db() as con:
            cur = con.execute(
                """INSERT INTO recurring(client_id,start_date,anchor_day,next_period_start,stop_after,status,tax_name,
                   tax_rate,discount_cents,notes,terms,etransfer_email,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,'active',?,?,?,?,?,?,?,?,?)""",
                (data["client_id"], data["start"].isoformat(), data["start"].day, first.isoformat(),
                 data["stop"].isoformat() if data["stop"] else None, data["tax_name"], data["tax_rate"],
                 data["discount_cents"], data["notes"], data["terms"], data["etransfer_email"] or None,
                 current_user()["id"], ts, ts))
            rid = cur.lastrowid
            _insert_items(con, "recurring_items", "recurring_id", rid, items, with_amount=False)
        _remember_new_etransfer(data)
        made = run_billing()
        msg = "Monthly billing set up."
        if made:
            msg += f" The first invoice ({len(made)}) was created and sent now."
        flash(msg, "ok")
        return redirect(url_for("plan_view", rid=rid))
    s = settings()
    plan = {"client_id": request.args.get("client_id", type=int), "start_date": today().isoformat(),
            "stop_after": "", "tax_name": s["tax_name"], "tax_rate": s["tax_rate"], "discount": "",
            "notes": s["default_notes"], "terms": s["default_terms"], "etransfer_email": s["etransfer_email"]}
    return render_template("plan_form.html", **_plan_form_context(
        plan, [{"description": "", "qty": "1", "price": "", "taxable": True}], True))


@app.route("/billing/<int:rid>")
@login_required()
def plan_view(rid):
    r = plan_or_404(rid)
    invoices = q("SELECT * FROM invoices WHERE recurring_id=? ORDER BY period_start DESC", (rid,))
    return render_template("plan_view.html", r=r, items=plan_items(rid), invoices=invoices)


@app.route("/billing/<int:rid>/edit", methods=["GET", "POST"])
@login_required()
def plan_edit(rid):
    r = plan_or_404(rid)
    if r["status"] == "ended":
        flash("This monthly billing has ended. Set up a new one instead.", "error")
        return redirect(url_for("plan_view", rid=rid))
    has_invoices = bool(q("SELECT 1 FROM invoices WHERE recurring_id=?", (rid,), one=True))
    if request.method == "POST":
        form = request.form.copy()
        if has_invoices:
            form["start_date"] = r["start_date"]          # schedule is fixed once billing started
        data, items, errors = _parse_plan_form(form)
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("plan_form.html", **_plan_form_context(dict(data, id=rid), items, False,
                                                                          has_invoices)), 400
        nxt = r["next_period_start"]
        if not has_invoices:
            nxt = B.first_period_start(data["start"], today()).isoformat()
        with db() as con:
            con.execute("""UPDATE recurring SET client_id=?,start_date=?,anchor_day=?,next_period_start=?,stop_after=?,
                           tax_name=?,tax_rate=?,discount_cents=?,notes=?,terms=?,etransfer_email=?,updated_at=?
                           WHERE id=?""",
                        (data["client_id"], data["start"].isoformat(), data["start"].day, nxt,
                         data["stop"].isoformat() if data["stop"] else None, data["tax_name"], data["tax_rate"],
                         data["discount_cents"], data["notes"], data["terms"], data["etransfer_email"] or None,
                         now_iso(), rid))
            con.execute("DELETE FROM recurring_items WHERE recurring_id=?", (rid,))
            _insert_items(con, "recurring_items", "recurring_id", rid, items, with_amount=False)
        _remember_new_etransfer(data)
        run_billing()
        flash("Saved. Invoices already created keep their original details.", "ok")
        return redirect(url_for("plan_view", rid=rid))
    plan = dict(r)
    plan["discount"] = plain_money(r["discount_cents"]) if r["discount_cents"] else ""
    items = [{"description": i["description"], "qty": fmt_qty(i["qty"]), "price": plain_money(i["unit_cents"]),
              "taxable": i["taxable"]} for i in plan_items(rid)]
    return render_template("plan_form.html", **_plan_form_context(plan, items, False, has_invoices))


@app.post("/billing/<int:rid>/status")
@login_required()
def plan_status(rid):
    r = plan_or_404(rid)
    action = request.form.get("action")
    if action == "pause" and r["status"] == "active":
        x("UPDATE recurring SET status='paused', updated_at=? WHERE id=?", (now_iso(), rid))
        flash("Paused. No invoices will be created until you resume.", "ok")
    elif action == "resume" and r["status"] == "paused":
        # skip periods that started while paused
        nxt = max(date.fromisoformat(r["next_period_start"]),
                  B.first_period_start(date.fromisoformat(r["start_date"]), today()))
        x("UPDATE recurring SET status='active', next_period_start=?, updated_at=? WHERE id=?",
          (nxt.isoformat(), now_iso(), rid))
        made = run_billing()
        flash("Resumed." + (" This month's invoice was created and sent." if made else ""), "ok")
    elif action == "stop" and r["status"] != "ended":
        x("UPDATE recurring SET status='ended', updated_at=? WHERE id=?", (now_iso(), rid))
        flash("Monthly billing stopped. Invoices already sent are not affected.", "ok")
    return redirect(url_for("plan_view", rid=rid))


@app.post("/billing/run")
@login_required(*SEE_ALL_ROLES)
def plans_run():
    made = run_billing()
    flash(f"Billing check done: {len(made)} invoice(s) created." if made else
          "Billing check done: nothing due today.", "ok")
    return redirect(url_for("plans_page"))


def run_billing(day=None):
    """Create (and email) every monthly invoice that is due. Safe to run any time, from any process."""
    day = day or today()
    created = []
    for r in q("SELECT * FROM recurring WHERE status='active'"):
        stop = date.fromisoformat(r["stop_after"]) if r["stop_after"] else None
        for start in B.due_period_starts(r["anchor_day"], date.fromisoformat(r["next_period_start"]), day, stop):
            iid = _create_period_invoice(r, start)
            x("UPDATE recurring SET next_period_start=? WHERE id=? AND next_period_start<=?",
              (B.next_period_start(r["anchor_day"], start).isoformat(), r["id"], start.isoformat()))
            if iid:
                created.append(iid)
                _email_period_invoice(iid)
        r2 = q("SELECT next_period_start FROM recurring WHERE id=?", (r["id"],), one=True)
        if stop and date.fromisoformat(r2["next_period_start"]) > stop:
            x("UPDATE recurring SET status='ended', updated_at=? WHERE id=?", (now_iso(), r["id"]))
    return created


def _create_period_invoice(r, start):
    client = q("SELECT * FROM clients WHERE id=?", (r["client_id"],), one=True)
    if not client:
        log.error("Monthly billing #%s: client %s missing", r["id"], r["client_id"])
        return None
    label = B.period_label(r["anchor_day"], start)
    items = [dict(it, description=f"{it['description']} ({label})") for it in plan_items(r["id"])]
    data = {"client_name": client["name"], "client_email": client["email"] or "", "client_phone": client["phone"] or "",
            "client_address": client["address"] or "", "client_id": client["id"],
            "issue_date": start.isoformat(),
            "due_date": (start + timedelta(days=int(settings()["default_due_days"] or 0))).isoformat(),
            "tax_name": r["tax_name"], "tax_rate": r["tax_rate"], "discount_cents": r["discount_cents"],
            "notes": r["notes"], "terms": r["terms"], "etransfer_email": r["etransfer_email"]}
    total = compute_totals(items, r["tax_rate"], r["discount_cents"])["total"]
    status = L.after_send(L.Invoice(L.DRAFT, total, 0, data["due_date"], r["created_by"]))
    try:
        with db() as con:
            return _insert_invoice(con, data, items, r["created_by"], status,
                                   f"Created and marked sent by monthly billing ({label})",
                                   recurring_id=r["id"], period_start=start.isoformat())
    except sqlite3.IntegrityError:
        return None          # another process already created this period


def _email_period_invoice(iid):
    inv = q("SELECT * FROM invoices WHERE id=?", (iid,), one=True)
    if inv["status"] == L.PAID:
        return
    if not smtp_ready():
        add_event(iid, "Not emailed automatically: email sending isn't set up")
        return
    if not inv["client_email"]:
        add_event(iid, "Not emailed automatically: the client has no email")
        return
    s = settings()
    try:
        send_email(inv["client_email"], fill_template(s["email_subject"], inv), fill_template(s["email_body"], inv),
                   reply_to=s["business_email"] or None)
        add_event(iid, f"Emailed automatically to {inv['client_email']}")
    except Exception as e:  # noqa: BLE001
        log.exception("Automatic email failed for %s", inv["number"])
        add_event(iid, f"Automatic email to {inv['client_email']} failed: {e}")


def start_scheduler(interval=600):
    """Run monthly billing every `interval` seconds in one process (file lock), started per worker."""
    import fcntl
    import threading

    def loop():
        lock = open(os.path.join(DATA_DIR, "billing.lock"), "w")
        holder = False
        while True:
            if not holder:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    holder = True
                    log.info("Monthly billing scheduler running (pid %s)", os.getpid())
                except OSError:
                    pass
            if holder:
                try:
                    with app.test_request_context(base_url=BASE_URL or "http://localhost/"):
                        made = run_billing()
                    if made:
                        log.info("Monthly billing created invoices %s", made)
                except Exception:  # noqa: BLE001
                    log.exception("Monthly billing run failed")
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="monthly-billing").start()


@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOAD_DIR, name, max_age=86400)


# --------------------------------------------------------------------------
# Settings, users, account
# --------------------------------------------------------------------------

TEXT_SETTINGS = ["business_name", "business_address", "business_email", "business_phone", "business_website",
                 "tax_number_label", "tax_number", "accent_color", "invoice_title", "invoice_prefix", "next_number",
                 "default_due_days", "tax_name", "tax_rate", "currency", "default_notes", "default_terms",
                 "creator_label", "etransfer_email", "etransfer_emails", "etransfer_instructions", "email_subject", "email_body",
                 "smtp_host", "smtp_port", "smtp_security", "smtp_user", "smtp_from"]
BOOL_SETTINGS = ["show_artist", "etransfer_enabled", "helcim_enabled", "helcim_fee"]


@app.route("/settings", methods=["GET", "POST"])
@login_required("admin")
def settings_page():
    if request.method == "POST":
        vals = {k: request.form.get(k, "").replace("\r\n", "\n").strip() for k in TEXT_SETTINGS}
        for k in BOOL_SETTINGS:
            vals[k] = "1" if request.form.get(k) else "0"
        errors = []
        if not re.match(r"^#[0-9a-fA-F]{6}$", vals["accent_color"]):
            errors.append("Accent color must look like #df620c.")
        if not vals["next_number"].isdigit():
            errors.append("Next invoice number must be digits only.")
        if not vals["default_due_days"].isdigit():
            errors.append("Default due days must be a whole number.")
        try:
            to_decimal(vals["tax_rate"], "tax rate")
        except ValueError as e:
            errors.append(str(e))
        if vals["currency"] not in ("CAD", "USD"):
            errors.append("Currency must be CAD or USD (Helcim supports these).")
        if vals["smtp_port"] and not vals["smtp_port"].isdigit():
            errors.append("Email port must be a number, e.g. 465.")
        if vals["smtp_security"] not in ("ssl", "starttls", "none"):
            vals["smtp_security"] = "ssl"
        for key in ("helcim_token", "smtp_password"):       # blank keeps what's saved
            new = request.form.get(key, "").strip()
            if request.form.get(f"remove_{key}"):
                vals[f"secret_{key}"] = ""
            elif new:
                vals[f"secret_{key}"] = new
        logo = request.files.get("logo")
        if logo and logo.filename:
            ext = logo.filename.rsplit(".", 1)[-1].lower() if "." in logo.filename else ""
            if ext not in ("png", "jpg", "jpeg", "webp"):
                errors.append("Logo must be PNG, JPG or WEBP.")
            else:
                name = f"logo-{secrets.token_hex(4)}.{ext}"
                logo.save(os.path.join(UPLOAD_DIR, name))
                vals["logo"] = name
        if request.form.get("remove_logo"):
            vals["logo"] = ""
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("settings.html", **_settings_context(dict(settings(), **vals))), 400
        save_settings(vals)
        flash("Settings saved.", "ok")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", **_settings_context(settings()))


def _settings_context(s_):
    return dict(s=s_, helcim_token=bool(helcim_token()), smtp=smtp_ready(),
                helcim_source="portainer" if os.environ.get("HELCIM_API_TOKEN", "").strip() else "app",
                helcim_saved=bool(secret("helcim_token")), smtp_source=smtp_config()["source"],
                smtp_password_saved=bool(secret("smtp_password")))


@app.post("/settings/test-email")
@login_required("admin")
def settings_test_email():
    to = request.form.get("to", "").strip() or current_user()["email"]
    if not smtp_ready():
        flash("Fill in the email server and From address, save, then send a test.", "error")
        return redirect(url_for("settings_page") + "#email")
    try:
        send_email(to, f"Test email from {settings()['business_name']} invoices",
                   "Email sending works. Invoices and monthly billing emails will be sent from this address.",
                   reply_to=settings()["business_email"] or None)
    except Exception as e:  # noqa: BLE001
        log.exception("Test email failed")
        flash(f"Test email failed: {e}", "error")
        return redirect(url_for("settings_page") + "#email")
    flash(f"Test email sent to {to}. Check that inbox (and spam).", "ok")
    return redirect(url_for("settings_page") + "#email")


@app.route("/users", methods=["GET", "POST"])
@login_required("admin")
def users_page():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        name = request.form.get("name", "").strip()
        role = request.form.get("role", "advertising")
        pw = request.form.get("password", "")
        if not email or not name or role not in ROLES or len(pw) < 10:
            flash("Fill in name, email and role. Password needs 10+ characters.", "error")
        elif q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            flash("That email already has an account.", "error")
        else:
            x("INSERT INTO users(email,name,password_hash,role,created_at) VALUES(?,?,?,?,?)",
              (email, name, generate_password_hash(pw), role, now_iso()))
            flash(f"Account created for {name}. Send them the URL, email and password.", "ok")
        return redirect(url_for("users_page"))
    return render_template("users.html", users=q("SELECT * FROM users ORDER BY active DESC, name"))


@app.post("/users/<int:uid>")
@login_required("admin")
def user_update(uid):
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not u:
        abort(404)
    me = current_user()
    role = request.form.get("role", u["role"])
    active = 1 if request.form.get("active") else 0
    if uid == me["id"] and (role != "admin" or not active):
        flash("You can't remove your own admin access.", "error")
        return redirect(url_for("users_page"))
    if role not in ROLES:
        abort(400)
    x("UPDATE users SET role=?, active=? WHERE id=?", (role, active, uid))
    pw = request.form.get("password", "")
    if pw:
        if len(pw) < 10:
            flash("Password needs 10+ characters. Other changes saved.", "error")
            return redirect(url_for("users_page"))
        x("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
    flash(f"Saved {u['name']}.", "ok")
    return redirect(url_for("users_page"))


@app.route("/account", methods=["GET", "POST"])
@login_required()
def account():
    u = current_user()
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name and name != u["name"]:
            x("UPDATE users SET name=? WHERE id=?", (name, u["id"]))
            flash("Name updated.", "ok")
        new = request.form.get("new_password", "")
        if new:
            if not check_password_hash(u["password_hash"], request.form.get("current_password", "")):
                flash("Current password is wrong.", "error")
            elif len(new) < 10:
                flash("New password needs 10+ characters.", "error")
            else:
                x("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), u["id"]))
                flash("Password changed.", "ok")
        return redirect(url_for("account"))
    return render_template("account.html")


@app.route("/export.csv")
@login_required(*SEE_ALL_ROLES)
def export_csv():
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Number", "Status", "Client", "Client email", "Issue date", "Due date", "Subtotal", "Discount",
                "Tax name", "Tax", "Total", "Paid", "Balance", "Created by"])
    for r in q("""SELECT i.*, u.name AS artist_name FROM invoices i LEFT JOIN users u ON u.id=i.created_by
                  ORDER BY i.id"""):
        w.writerow([r["number"], r["status"], r["client_name"], r["client_email"], r["issue_date"], r["due_date"],
                    plain_money(r["subtotal_cents"]), plain_money(r["discount_cents"]), r["tax_name"],
                    plain_money(r["tax_cents"]), plain_money(r["total_cents"]), plain_money(r["paid_cents"]),
                    plain_money(r["total_cents"] - r["paid_cents"]), r["artist_name"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=invoices-{today()}.csv"})


@app.route("/healthz")
def healthz():
    q("SELECT 1")
    return "ok"


def _is_json_path():
    return request.path.startswith("/i/") and "/helcim/" in request.path


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(405)
def _err(e):
    if _is_json_path():
        return jsonify(error=getattr(e, "description", "Error")), e.code
    return render_template("error.html", code=e.code, message=getattr(e, "description", "")), e.code


@app.errorhandler(Exception)
def _crash(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    log.exception("Unhandled error on %s %s", request.method, request.path)
    if _is_json_path():
        return jsonify(error="Card payments hit a problem on our side. Please use e-Transfer or try again later."), 400
    return render_template("error.html", code=500, message="Something went wrong. Try again, and if it keeps "
                                                          "happening check the container logs in Portainer."), 500


@app.context_processor
def _inject():
    return {"s": settings(), "section": _section()}


SECTIONS = {"invoices": ("dashboard", "invoice_"), "billing": ("plan",), "clients": ("client",),
            "items": ("catalog",), "account": ("settings_page", "users_page", "user_", "account")}


def _section():
    ep = request.endpoint or ""
    for name, prefixes in SECTIONS.items():
        if any(ep.startswith(p_) for p_ in prefixes):
            return name
    return ""


STATUS_LABELS = {"draft": "Draft", "sent": "Sent", "partial": "Partly paid", "paid": "Paid", "void": "Void",
                 "overdue": "Overdue", "active": "Active", "paused": "Paused", "ended": "Ended"}


@app.template_filter("status_label")
def status_label(st):
    return STATUS_LABELS.get(st, str(st).capitalize())


@app.template_filter("initials")
def initials(name):
    parts = [w for w in str(name or "").split() if w]
    return ("".join(w[0] for w in parts[:2]) or "?").upper()


def greeting():
    hour = datetime.now(ZoneInfo(os.environ.get("TZ") or "America/Toronto")).hour
    return "Good morning" if hour < 12 else ("Good afternoon" if hour < 18 else "Good evening")


app.jinja_env.globals["greeting"] = greeting


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=os.environ.get("FLASK_DEBUG") == "1")

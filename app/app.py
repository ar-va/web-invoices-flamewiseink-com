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

import requests
from flask import (Flask, Response, abort, flash, g, jsonify, redirect,
                   render_template, request, send_from_directory, session,
                   url_for)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
DATA_DIR = os.environ.get("DATA_DIR", "/data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "invoices.db")
os.makedirs(UPLOAD_DIR, exist_ok=True)

HELCIM_API = os.environ.get("HELCIM_API_URL", "https://api.helcim.com/v2").rstrip("/")
HELCIM_TOKEN = os.environ.get("HELCIM_API_TOKEN", "").strip()
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

ROLES = {
    "admin": ("Admin", "everything, incl. settings and accounts"),
    "manager": ("Studio Manager", "all invoices, no settings or accounts"),
    "advertising": ("Advertising", "only invoices they created"),
}
SEE_ALL_ROLES = ("admin", "manager")
OLD_ROLE_NAMES = {"staff": "manager", "artist": "advertising"}
STATUSES = ["draft", "sent", "partial", "paid", "void"]
PAY_METHODS = ["e-Transfer", "Helcim (card)", "Cash", "Debit/Credit (terminal)", "Other"]

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
    "business_email": "ads@flamewiseink.ca",
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
CREATE INDEX IF NOT EXISTS idx_items_inv ON items(invoice_id);
CREATE INDEX IF NOT EXISTS idx_pay_inv ON payments(invoice_id);
"""


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def bootstrap():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(invoices)")}
    if "etransfer_email" not in cols:
        con.execute("ALTER TABLE invoices ADD COLUMN etransfer_email TEXT")
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


bootstrap()

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
        s.update({r["key"]: r["value"] for r in q("SELECT key, value FROM settings")})
        g.settings = s
    return g.settings


def save_settings(values):
    con = db()
    with con:
        for k, v in values.items():
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (k, v))
    g.pop("settings", None)


def add_event(invoice_id, text):
    x("INSERT INTO events(invoice_id,at,text) VALUES(?,?,?)", (invoice_id, now_iso(), text))


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


def display_status(inv):
    st = inv["status"]
    if st in ("sent", "partial") and inv["due_date"] < date.today().isoformat():
        return "overdue"
    return st


app.jinja_env.globals.update(display_status=display_status, ROLES=ROLES, PAY_METHODS=PAY_METHODS)


def etransfer_email_for(inv):
    """The invoice's own e-Transfer email, or the default from Settings."""
    try:
        own = inv["etransfer_email"]
    except (KeyError, IndexError):
        own = None
    return own or settings()["etransfer_email"]


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


def can_see(inv):
    u = current_user()
    if not u:
        return False
    return u["role"] in SEE_ALL_ROLES or inv["created_by"] == u["id"]


def sees_all():
    u = current_user()
    return bool(u) and u["role"] in SEE_ALL_ROLES


app.jinja_env.globals["sees_all"] = sees_all


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
    if u["role"] not in SEE_ALL_ROLES:
        where.append("i.created_by=?")
        args.append(u["id"])
    base_where, base_args = list(where), list(args)

    f = request.args.get("f", "open")
    today = date.today().isoformat()
    if f == "open":
        where.append("i.status IN ('draft','sent','partial')")
    elif f == "overdue":
        where.append("i.status IN ('sent','partial') AND i.due_date < ?")
        args.append(today)
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
        "outstanding": q(f"SELECT COALESCE(SUM(total_cents-paid_cents),0) v FROM invoices i WHERE {bw} AND status IN ('sent','partial')", base_args, one=True)["v"],
        "overdue": q(f"SELECT COUNT(*) v FROM invoices i WHERE {bw} AND status IN ('sent','partial') AND due_date < ?", base_args + [today], one=True)["v"],
        "drafts": q(f"SELECT COUNT(*) v FROM invoices i WHERE {bw} AND status='draft'", base_args, one=True)["v"],
        "paid_month": q(f"""SELECT COALESCE(SUM(p.amount_cents),0) v FROM payments p JOIN invoices i ON i.id=p.invoice_id
                           WHERE {bw} AND p.paid_on >= ?""", base_args + [date.today().replace(day=1).isoformat()], one=True)["v"],
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
        "etransfer_email": form.get("etransfer_email", "").strip(),
    }
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

    items = []
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
    return data, items, errors


def _form_context(inv, items, is_new):
    past_clients = q("""SELECT client_name, client_email, client_phone, client_address FROM invoices
                        WHERE id IN (SELECT MAX(id) FROM invoices GROUP BY lower(client_name))
                        ORDER BY client_name LIMIT 1000""")
    clients = {r["client_name"]: {"email": r["client_email"] or "", "phone": r["client_phone"] or "",
                                  "address": r["client_address"] or ""} for r in past_clients}
    quick = []
    for line in settings()["quick_items"].splitlines():
        if "|" in line:
            d, p = line.rsplit("|", 1)
            if d.strip():
                quick.append({"description": d.strip(), "price": p.strip()})
    return dict(inv=inv, items=items, is_new=is_new, clients=clients, quick_items=quick)


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
        flash("Invoice created.", "ok")
        return redirect(url_for("invoice_view", iid=iid))
    today = date.today()
    inv = {
        "client_name": request.args.get("client", ""), "client_email": "", "client_phone": "", "client_address": "",
        "issue_date": today.isoformat(),
        "due_date": (today + timedelta(days=int(s["default_due_days"] or 0))).isoformat(),
        "tax_name": s["tax_name"], "tax_rate": s["tax_rate"], "notes": s["default_notes"], "terms": s["default_terms"],
        "etransfer_email": s["etransfer_email"],
        "number": "", "discount": "",
    }
    items = [{"description": "", "qty": "1", "price": "", "taxable": True}]
    return render_template("invoice_form.html", **_form_context(inv, items, True))


@app.route("/invoices/<int:iid>/edit", methods=["GET", "POST"])
@login_required()
def invoice_edit(iid):
    inv = get_invoice(iid)
    if inv["status"] == "void":
        flash("Voided invoices can't be edited. Reopen it first.", "error")
        return redirect(url_for("invoice_view", iid=iid))
    if request.method == "POST":
        data, items, errors = parse_invoice_form(request.form)
        if data["number"] and data["number"] != inv["number"]:
            if q("SELECT 1 FROM invoices WHERE number=? AND id<>?", (data["number"], iid), one=True):
                errors.append(f"Invoice number {data['number']} is already used.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("invoice_form.html", **_form_context(dict(data, id=iid), items, False)), 400
        save_invoice(iid, data, items)
        flash("Invoice saved.", "ok")
        return redirect(url_for("invoice_view", iid=iid))
    items = [{"description": r["description"], "qty": fmt_qty(r["qty"]), "price": plain_money(r["unit_cents"]),
              "taxable": bool(r["taxable"])} for r in q("SELECT * FROM items WHERE invoice_id=? ORDER BY position", (iid,))]
    data = dict(inv)
    data["discount"] = plain_money(inv["discount_cents"]) if inv["discount_cents"] else ""
    return render_template("invoice_form.html", **_form_context(data, items, False))


def save_invoice(iid, data, items):
    totals = compute_totals(items, data["tax_rate"], data["discount_cents"])
    con = db()
    ts = now_iso()
    with con:
        if iid is None:
            row = con.execute("SELECT value FROM settings WHERE key='next_number'").fetchone()
            nxt = int(row[0]) if row else int(DEFAULTS["next_number"])
            prefix = settings()["invoice_prefix"]
            number = f"{prefix}{nxt}"
            while con.execute("SELECT 1 FROM invoices WHERE number=?", (number,)).fetchone():
                nxt += 1
                number = f"{prefix}{nxt}"
            con.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('next_number',?)", (str(nxt + 1),))
            cur = con.execute(
                """INSERT INTO invoices(number,token,status,client_name,client_email,client_phone,client_address,
                   issue_date,due_date,tax_name,tax_rate,discount_cents,notes,terms,subtotal_cents,tax_cents,total_cents,
                   created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (number, secrets.token_urlsafe(18), "draft", data["client_name"], data["client_email"], data["client_phone"],
                 data["client_address"], data["issue_date"], data["due_date"], data["tax_name"], data["tax_rate"],
                 totals["discount"], data["notes"], data["terms"], totals["subtotal"], totals["tax"], totals["total"],
                 current_user()["id"], ts, ts))
            iid = cur.lastrowid
            con.execute("INSERT INTO events(invoice_id,at,text) VALUES(?,?,?)",
                        (iid, ts, f"Created by {current_user()['name']}"))
        else:
            number = data["number"] or q("SELECT number FROM invoices WHERE id=?", (iid,), one=True)["number"]
            con.execute(
                """UPDATE invoices SET number=?,client_name=?,client_email=?,client_phone=?,client_address=?,issue_date=?,
                   due_date=?,tax_name=?,tax_rate=?,discount_cents=?,notes=?,terms=?,subtotal_cents=?,tax_cents=?,
                   total_cents=?,updated_at=? WHERE id=?""",
                (number, data["client_name"], data["client_email"], data["client_phone"], data["client_address"],
                 data["issue_date"], data["due_date"], data["tax_name"], data["tax_rate"], totals["discount"],
                 data["notes"], data["terms"], totals["subtotal"], totals["tax"], totals["total"], ts, iid))
            con.execute("DELETE FROM items WHERE invoice_id=?", (iid,))
            con.execute("INSERT INTO events(invoice_id,at,text) VALUES(?,?,?)",
                        (iid, ts, f"Edited by {current_user()['name']}"))
        con.execute("UPDATE invoices SET etransfer_email=? WHERE id=?", (data.get("etransfer_email") or None, iid))
        for pos, it in enumerate(items):
            con.execute("""INSERT INTO items(invoice_id,position,description,qty,unit_cents,taxable,amount_cents)
                           VALUES(?,?,?,?,?,?,?)""",
                        (iid, pos, it["description"], it["qty"], it["unit_cents"], 1 if it["taxable"] else 0,
                         it["amount_cents"]))
    g.pop("settings", None)
    refresh_invoice(iid)
    return iid


def refresh_invoice(iid):
    """Recompute paid amount and status from payments."""
    inv = q("SELECT * FROM invoices WHERE id=?", (iid,), one=True)
    paid = q("SELECT COALESCE(SUM(amount_cents),0) v FROM payments WHERE invoice_id=?", (iid,), one=True)["v"]
    status, paid_at = inv["status"], inv["paid_at"]
    if status != "void":
        if inv["total_cents"] > 0 and paid >= inv["total_cents"]:
            status = "paid"
            paid_at = paid_at or now_iso()
        elif paid > 0:
            status, paid_at = "partial", None
        elif status in ("paid", "partial"):
            status, paid_at = "sent", None
    x("UPDATE invoices SET paid_cents=?, status=?, paid_at=? WHERE id=?", (paid, status, paid_at, iid))
    if status == "paid" and inv["status"] != "paid":
        add_event(iid, "Paid in full")


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
    return render_template("invoice_view.html", inv=inv, items=items, payments=payments, events=events,
                           smtp=smtp_ready(), today=date.today().isoformat())


@app.post("/invoices/<int:iid>/status")
@login_required()
def invoice_status(iid):
    inv = get_invoice(iid)
    action = request.form.get("action")
    who = current_user()["name"]
    if action == "sent" and inv["status"] == "draft":
        x("UPDATE invoices SET status='sent', sent_at=COALESCE(sent_at,?) WHERE id=?", (now_iso(), iid))
        add_event(iid, f"Marked as sent by {who}")
    elif action == "void" and inv["status"] != "void":
        x("UPDATE invoices SET status='void' WHERE id=?", (iid,))
        add_event(iid, f"Voided by {who}")
    elif action == "reopen" and inv["status"] in ("void", "sent"):
        x("UPDATE invoices SET status='draft' WHERE id=?", (iid,))
        add_event(iid, f"Moved back to draft by {who}")
        refresh_invoice(iid)
    flash("Status updated.", "ok")
    return redirect(url_for("invoice_view", iid=iid))


@app.post("/invoices/<int:iid>/payments")
@login_required()
def payment_add(iid):
    inv = get_invoice(iid)
    try:
        amount = to_cents(request.form.get("amount"))
        if amount <= 0:
            raise ValueError("Amount must be more than $0.")
        paid_on = request.form.get("paid_on") or date.today().isoformat()
        datetime.strptime(paid_on, "%Y-%m-%d")
    except ValueError as e:
        flash(f"Payment not saved: {e}", "error")
        return redirect(url_for("invoice_view", iid=iid))
    method = request.form.get("method") if request.form.get("method") in PAY_METHODS else "Other"
    x("""INSERT INTO payments(invoice_id,method,amount_cents,paid_on,reference,note,created_by,created_at)
         VALUES(?,?,?,?,?,?,?,?)""",
      (iid, method, amount, paid_on, request.form.get("reference", "").strip(), request.form.get("note", "").strip(),
       current_user()["id"], now_iso()))
    if inv["status"] == "draft":
        x("UPDATE invoices SET status='sent', sent_at=COALESCE(sent_at,?) WHERE id=?", (now_iso(), iid))
    add_event(iid, f"{money(amount)} {method} payment recorded by {current_user()['name']}")
    refresh_invoice(iid)
    flash(f"{money(amount)} payment recorded.", "ok")
    return redirect(url_for("invoice_view", iid=iid))


@app.post("/invoices/<int:iid>/payments/<int:pid>/delete")
@login_required(*SEE_ALL_ROLES)
def payment_delete(iid, pid):
    get_invoice(iid)
    p = q("SELECT * FROM payments WHERE id=? AND invoice_id=?", (pid, iid), one=True)
    if p:
        x("DELETE FROM payments WHERE id=?", (pid,))
        add_event(iid, f"{money(p['amount_cents'])} {p['method']} payment removed by {current_user()['name']}")
        refresh_invoice(iid)
        flash("Payment removed.", "ok")
    return redirect(url_for("invoice_view", iid=iid))


@app.post("/invoices/<int:iid>/duplicate")
@login_required()
def invoice_duplicate(iid):
    inv = get_invoice(iid)
    s = settings()
    today = date.today()
    data = {k: inv[k] for k in ("client_name", "client_email", "client_phone", "client_address", "tax_name",
                                 "tax_rate", "notes", "terms", "etransfer_email")}
    data.update(issue_date=today.isoformat(),
                due_date=(today + timedelta(days=int(s["default_due_days"] or 0))).isoformat(),
                discount_cents=inv["discount_cents"], number="")
    items = [{"description": r["description"], "qty": r["qty"], "unit_cents": r["unit_cents"],
              "taxable": bool(r["taxable"]), "amount_cents": r["amount_cents"]}
             for r in q("SELECT * FROM items WHERE invoice_id=? ORDER BY position", (iid,))]
    new_id = save_invoice(None, data, items)
    flash(f"Copied {inv['number']} into a new draft.", "ok")
    return redirect(url_for("invoice_edit", iid=new_id))


@app.post("/invoices/<int:iid>/delete")
@login_required("admin")
def invoice_delete(iid):
    inv = get_invoice(iid)
    x("DELETE FROM invoices WHERE id=?", (iid,))
    flash(f"Invoice {inv['number']} deleted.", "ok")
    return redirect(url_for("dashboard"))


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------


def smtp_ready():
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def send_email(to, subject, body, reply_to=None):
    msg = EmailMessage()
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(body)
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    security = os.environ.get("SMTP_SECURITY", "starttls").lower()
    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
        if security == "starttls":
            server.starttls()
    try:
        if os.environ.get("SMTP_USER"):
            server.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
        server.send_message(msg)
    finally:
        server.quit()


@app.post("/invoices/<int:iid>/email")
@login_required()
def invoice_email(iid):
    inv = get_invoice(iid)
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
    if inv["status"] == "draft":
        x("UPDATE invoices SET status='sent' WHERE id=?", (iid,))
    x("UPDATE invoices SET sent_at=? WHERE id=?", (now_iso(), iid))
    add_event(iid, f"Emailed to {to} by {current_user()['name']}")
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
    return bool(HELCIM_TOKEN) and settings()["helcim_enabled"] == "1"


app.jinja_env.globals["helcim_ready"] = helcim_ready


@app.route("/i/<token>")
def public_invoice(token):
    inv = public_invoice_or_404(token)
    preview = can_see(inv)
    if inv["status"] == "draft" and not preview:
        abort(404)
    if not current_user() and not inv["viewed_at"]:
        x("UPDATE invoices SET viewed_at=? WHERE id=?", (now_iso(), inv["id"]))
        add_event(inv["id"], "Client opened the invoice link")
    items, payments = invoice_bundle(inv["id"])
    return render_template("public_invoice.html", inv=inv, items=items, payments=payments, preview=preview,
                           just_paid=request.args.get("paid") == "1", public=True)


def helcim_headers():
    return {"accept": "application/json", "content-type": "application/json", "api-token": HELCIM_TOKEN}


@app.post("/i/<token>/helcim/start")
def helcim_start(token):
    inv = public_invoice_or_404(token)
    if not helcim_ready():
        return jsonify(error="Card payments are not available."), 404
    if inv["status"] not in ("sent", "partial"):
        return jsonify(error="This invoice can't be paid online."), 400
    due = inv["total_cents"] - inv["paid_cents"]
    if due <= 0:
        return jsonify(error="This invoice is already paid."), 400
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
        payload["hasConvenienceFee"] = 1
    try:
        r = requests.post(f"{HELCIM_API}/helcim-pay/initialize", json=payload, headers=helcim_headers(), timeout=20)
        body = r.json() if r.content else {}
    except Exception:  # noqa: BLE001
        log.exception("Helcim initialize failed")
        return jsonify(error="Card payments are unavailable right now. Please use e-Transfer or try again later."), 502
    if r.status_code >= 300 or "checkoutToken" not in body:
        log.error("Helcim initialize HTTP %s: %s", r.status_code, str(body)[:500])
        return jsonify(error="Card payments are unavailable right now. Please use e-Transfer or try again later."), 502
    x("INSERT INTO helcim_sessions(invoice_id,checkout_token,secret_token,amount_cents,created_at) VALUES(?,?,?,?,?)",
      (inv["id"], body["checkoutToken"], body["secretToken"], due, now_iso()))
    return jsonify(checkoutToken=body["checkoutToken"])


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


def helcim_get_transaction(tid):
    try:
        r = requests.get(f"{HELCIM_API}/card-transactions/{int(tid)}", headers=helcim_headers(), timeout=20)
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
    api_txn = helcim_get_transaction(tid)
    source = api_txn or (txn if hash_ok else None)
    if not source:
        add_event(inv["id"], f"Card payment attempt (Helcim transaction {tid}) could not be verified - check Helcim")
        return jsonify(error=fail_msg), 400
    status = str(source.get("status", "")).upper()
    try:
        charged = to_cents(source.get("amount"))
    except ValueError:
        charged = 0
    if status != "APPROVED" or charged <= 0:
        add_event(inv["id"], f"Helcim transaction {tid} not approved (status {status or 'unknown'})")
        return jsonify(error="The payment was not approved."), 400
    if charged < sess["amount_cents"] and settings()["helcim_fee"] != "1":
        log.warning("Helcim txn %s amount %s less than requested %s", tid, charged, sess["amount_cents"])
    applied = min(charged, sess["amount_cents"])
    note = ""
    if charged > applied:
        note = f"Card charged {money(charged)} incl. {money(charged - applied)} convenience fee"
    if not api_txn:
        note = (note + "; " if note else "") + "verified by hash only"
    try:
        x("""INSERT INTO payments(invoice_id,method,amount_cents,paid_on,reference,helcim_transaction_id,note,created_at)
             VALUES(?,?,?,?,?,?,?,?)""",
          (inv["id"], "Helcim (card)", applied, date.today().isoformat(),
           f"Helcim #{tid}" + (f" / approval {source.get('approvalCode')}" if source.get("approvalCode") else ""),
           tid, note, now_iso()))
    except sqlite3.IntegrityError:
        return jsonify(ok=True)
    x("UPDATE helcim_sessions SET used=1 WHERE id=?", (sess["id"],))
    add_event(inv["id"], f"Client paid {money(applied)} by card (Helcim #{tid})")
    refresh_invoice(inv["id"])
    return jsonify(ok=True)


@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOAD_DIR, name, max_age=86400)


# --------------------------------------------------------------------------
# Settings, users, account
# --------------------------------------------------------------------------

TEXT_SETTINGS = ["business_name", "business_address", "business_email", "business_phone", "business_website",
                 "tax_number_label", "tax_number", "accent_color", "invoice_title", "invoice_prefix", "next_number",
                 "default_due_days", "tax_name", "tax_rate", "currency", "default_notes", "default_terms",
                 "quick_items", "creator_label", "etransfer_email", "etransfer_emails", "etransfer_instructions", "email_subject", "email_body"]
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
            return render_template("settings.html", s=dict(settings(), **vals), helcim_token=bool(HELCIM_TOKEN),
                                   smtp=smtp_ready()), 400
        save_settings(vals)
        flash("Settings saved.", "ok")
        return redirect(url_for("settings_page"))
    return render_template("settings.html", s=settings(), helcim_token=bool(HELCIM_TOKEN), smtp=smtp_ready())


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
                    headers={"Content-Disposition": f"attachment; filename=invoices-{date.today()}.csv"})


@app.route("/healthz")
def healthz():
    q("SELECT 1")
    return "ok"


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def _err(e):
    if request.path.startswith("/i/") and request.path.count("/") > 2:
        return jsonify(error=getattr(e, "description", "Error")), e.code
    return render_template("error.html", code=e.code, message=getattr(e, "description", "")), e.code


@app.context_processor
def _inject():
    return {"s": settings()}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=os.environ.get("FLASK_DEBUG") == "1")

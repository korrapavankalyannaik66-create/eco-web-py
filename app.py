import os
import re
import sys
import uuid
import json
import random
import secrets
import smtplib
import threading
from datetime import datetime
from email.message import EmailMessage
from functools import wraps
from urllib import request as urlrequest, parse as urlparse, error as urlerror
import base64

from flask import (Flask, render_template, request, redirect, url_for, session,
                   flash, jsonify, send_from_directory, abort)
from flask_sqlalchemy import SQLAlchemy
from markupsafe import escape
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename  # noqa: F401  (kept for reference)
from google import genai
from google.genai import types

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


def load_env_file(path):
    """Tiny .env loader: reads KEY=value lines from a file named .env next to app.py."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env_file(os.path.join(BASE_DIR, ".env"))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")  # SET A REAL SECRET_KEY IN PRODUCTION

app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "uploads")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///ecoloop.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024          # 5 MB upload limit
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE") == "1"  # set to 1 when using HTTPS

db = SQLAlchemy(app)
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "pdf"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------------------------------------------------------------------------
# GEMINI LIVE DYNAMIC AI INTEGRATION
# ---------------------------------------------------------------------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
genai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

ECO_SYSTEM_PROMPT = """
You are EcoLoop AI, a dynamic customer support assistant for the EcoLoop E-Waste Platform.
Platform Context & Rules:
- Purpose: Safe, government-regulated e-waste collection, inspection, and recycling.
- Roles: Seller, Govt Inspector, Company Admin.
- New Seller and Company Admin accounts must upload an ID proof and wait for a Govt Inspector to verify it before they can sell or trade.
- Workflow: Seller submits pickup -> Govt Inspector physically verifies -> Company Admin disburses payout via UPI/Bank/Card.

Answer concisely (under 3-4 sentences) with markdown formatting and emojis.
"""

user_chats = {}

# ---------------------------------------------------------------------------
# SQLALCHEMY DATABASE MODELS
# ---------------------------------------------------------------------------

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # Seller, Inspector, Admin
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=True)
    location = db.Column(db.String(200), nullable=True)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    gov_id = db.Column(db.String(100), nullable=True)
    doc_file = db.Column(db.String(200), nullable=True)
    company_name = db.Column(db.String(100), nullable=True)
    company_gst = db.Column(db.String(50), nullable=True)
    password = db.Column(db.String(255), nullable=False)   # stores a password HASH
    # Pending -> (Inspector approves) -> Active | Rejected | Suspended
    status = db.Column(db.String(20), default="Pending")
    reject_reason = db.Column(db.String(300), nullable=True)
    verified_at = db.Column(db.String(30), nullable=True)

class Company(db.Model):
    id = db.Column(db.String(20), primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    gst = db.Column(db.String(50), nullable=False)
    location = db.Column(db.String(100), nullable=False)
    bonus = db.Column(db.Integer, default=5)
    description = db.Column(db.String(200))
    approved = db.Column(db.Boolean, default=True)  # False until the inspector verifies the admin

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    unit = db.Column(db.String(20), nullable=False)
    rate = db.Column(db.Integer, nullable=False)

class Transaction(db.Model):
    id = db.Column(db.String(20), primary_key=True)
    date = db.Column(db.String(20), nullable=False)
    company = db.Column(db.String(100), nullable=False)
    seller = db.Column(db.String(100), nullable=False)
    seller_email = db.Column(db.String(120), nullable=False)
    seller_phone = db.Column(db.String(20))
    items = db.Column(db.Text, nullable=False)
    payout = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(30), default="In Progress")
    payment_method = db.Column(db.String(50), default="-")
    txn_ref = db.Column(db.String(50), default="-")

# ---------------------------------------------------------------------------
# SMALL MIGRATION (adds new columns to an existing ecoloop.db, no data loss)
# ---------------------------------------------------------------------------

def migrate_schema():
    with db.engine.begin() as conn:
        def cols(table):
            return {r[1] for r in conn.exec_driver_sql(f'PRAGMA table_info("{table}")')}

        user_cols = cols("user")
        for col, ddl in [
            ("latitude", "FLOAT"),
            ("longitude", "FLOAT"),
            ("reject_reason", "VARCHAR(300)"),
            ("verified_at", "VARCHAR(30)"),
        ]:
            if col not in user_cols:
                conn.exec_driver_sql(f'ALTER TABLE "user" ADD COLUMN {col} {ddl}')

        if "approved" not in cols("company"):
            conn.exec_driver_sql('ALTER TABLE "company" ADD COLUMN approved BOOLEAN DEFAULT 1')

# ---------------------------------------------------------------------------
# INITIAL SEED DATA
# ---------------------------------------------------------------------------

def seed_initial_data():
    if Company.query.count() == 0:
        db.session.add_all([
            Company(id="comp-1", name="EcoLoop Solutions", gst="37AAAAA1234A1Z1", location="Vijayawada, AP", bonus=5, description="Official E-Waste Processing Partner", approved=True),
            Company(id="comp-2", name="GreenCycle India", gst="36BBBBB5678B2Z2", location="Hyderabad, TS", bonus=10, description="Green Technology Recyclers", approved=True)
        ])

    if Product.query.count() == 0:
        db.session.add_all([
            Product(id=1, name="Mobile phones", unit="unit", rate=120),
            Product(id=2, name="Laptops", unit="unit", rate=1500),
            Product(id=3, name="Monitors / TVs", unit="unit", rate=350),
            Product(id=4, name="Printers", unit="unit", rate=250),
            Product(id=5, name="Hard drives", unit="unit", rate=80),
            Product(id=6, name="Chargers & adapters", unit="kg", rate=40),
            Product(id=7, name="Circuit boards (PCB)", unit="kg", rate=120),
            Product(id=8, name="Cables & wires", unit="kg", rate=30)
        ])

    if User.query.count() == 0:
        pw = generate_password_hash("password123")
        db.session.add_all([
            User(name="Ramesh Kumar", role="Seller", email="seller@demo.com", phone="9876543210", location="Auto Nagar, Vijayawada", gov_id="1234-5678-9012", doc_file="id_proof_sample.pdf", password=pw, status="Active"),
            User(name="Inspector Officer V. Rao", role="Inspector", email="inspector@demo.com", phone="9876543211", gov_id="GOV-INSP-9901", doc_file="govt_badge.pdf", password=pw, status="Active"),
            User(name="Priya Sharma", role="Admin", email="admin@demo.com", phone="9876543212", location="Hyderabad, TS", gov_id="DEMO-ADMIN-ID", doc_file="admin_id_sample.pdf", company_name="EcoLoop Solutions", company_gst="37AAAAA1234A1Z1", password=pw, status="Active")
        ])

    db.session.commit()

# ---------------------------------------------------------------------------
# CLI PRINT DATA FUNCTION
# ---------------------------------------------------------------------------

def print_database_data():
    with app.app_context():
        print("\n==================== REGISTERED USERS ====================", flush=True)
        users = User.query.all()
        if not users:
            print("No users found in database.", flush=True)
        for u in users:
            print(f"ID: {u.id} | Name: {u.name} | Role: {u.role} | Email: {u.email} | Location: {u.location} | GPS: {u.latitude},{u.longitude} | Doc: {u.doc_file} | Status: {u.status}", flush=True)

        print("\n==================== RECYCLING COMPANIES ====================", flush=True)
        companies = Company.query.all()
        if not companies:
            print("No companies found.", flush=True)
        for c in companies:
            print(f"ID: {c.id} | Name: {c.name} | GST: {c.gst} | Location: {c.location} | Bonus: +{c.bonus}% | Approved: {c.approved}", flush=True)

        print("\n==================== E-WASTE PRODUCTS & RATES ====================", flush=True)
        products = Product.query.all()
        if not products:
            print("No products found.", flush=True)
        for p in products:
            print(f"ID: {p.id} | Name: {p.name} | Unit: {p.unit} | Rate: ₹{p.rate}", flush=True)

        print("\n==================== TRANSACTIONS ====================", flush=True)
        transactions = Transaction.query.all()
        if not transactions:
            print("No transactions found in queue.", flush=True)
        for t in transactions:
            print(f"Req ID: {t.id} | Date: {t.date} | Seller: {t.seller} ({t.seller_email}) | Company: {t.company} | Items: {t.items} | Payout: ₹{t.payout} | Status: {t.status} | Ref: {t.txn_ref}", flush=True)
        print("===========================================================\n", flush=True)

# ---------------------------------------------------------------------------
# NOTIFICATIONS (EMAIL + SMS)
#
# Email  -> set SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS, MAIL_FROM
#           (Gmail: use an "App Password")
# SMS    -> set TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM  (Twilio number)
# If these are not set, the message is printed in the terminal instead,
# so you can test the whole flow without any account.
# ---------------------------------------------------------------------------

def normalize_phone(phone):
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+") and 10 <= len(digits) <= 15:
        return "+" + digits
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    return None

def send_email(to_addr, subject, body):
    """Returns (ok, detail)."""
    host = os.getenv("SMTP_HOST")
    if not host:
        print(f"[EMAIL not configured] To: {to_addr} | {subject}\n{body}\n", flush=True)
        return False, "not configured (add SMTP_HOST, SMTP_USER, SMTP_PASS to the .env file)"
    if not to_addr:
        return False, "no email address on this account"
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = os.getenv("MAIL_FROM") or os.getenv("SMTP_USER", "no-reply@ecoloop.local")
        msg["To"] = to_addr
        msg.set_content(body)
        port = int(os.getenv("SMTP_PORT", "587"))
        server = smtplib.SMTP_SSL(host, port, timeout=15) if port == 465 else smtplib.SMTP(host, port, timeout=15)
        with server as s:
            if port != 465:
                s.starttls()
            if os.getenv("SMTP_USER"):
                s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
            s.send_message(msg)
        return True, "sent"
    except smtplib.SMTPAuthenticationError:
        return False, "login failed - check SMTP_USER / SMTP_PASS (Gmail needs an App Password)"
    except Exception as e:
        print("Email send error:", e, flush=True)
        return False, f"error: {e}"

def send_sms(phone, body):
    """Returns (ok, detail)."""
    to = normalize_phone(phone)
    sid, token, sender = os.getenv("TWILIO_SID"), os.getenv("TWILIO_TOKEN"), os.getenv("TWILIO_FROM")
    if not to:
        return False, f"invalid phone number '{phone}'"
    if not (sid and token and sender):
        print(f"[SMS not configured] To: {to} | {body}\n", flush=True)
        return False, "not configured (add TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM to the .env file)"
    try:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        data = urlparse.urlencode({"To": to, "From": sender, "Body": body}).encode()
        req = urlrequest.Request(url, data=data)
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
        urlrequest.urlopen(req, timeout=15).read()
        return True, "sent"
    except urlerror.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("message", str(e))
        except Exception:
            detail = str(e)
        print("SMS send error:", detail, flush=True)
        return False, f"error: {detail}"
    except (urlerror.URLError, OSError) as e:
        print("SMS send error:", e, flush=True)
        return False, f"error: {e}"

def notify_now(email, phone, subject, message):
    """Send email + SMS and return (flash_level, honest_report_text)."""
    ok_e, d_e = send_email(email, subject, message)
    ok_s, d_s = send_sms(phone, message)
    if ok_e and ok_s:
        return "success", "📧 Email sent ✅ | 📱 SMS sent ✅"
    parts = ["📧 Email sent ✅" if ok_e else f"📧 Email NOT sent: {d_e}",
             "📱 SMS sent ✅" if ok_s else f"📱 SMS NOT sent: {d_s}"]
    return ("warning" if (ok_e or ok_s) else "danger"), " | ".join(parts)

# ---------------------------------------------------------------------------
# SECURITY HELPERS
# ---------------------------------------------------------------------------

def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]

app.jinja_env.globals["csrf_token"] = csrf_token

@app.before_request
def csrf_protect():
    if request.method == "POST":
        sent = request.form.get("_csrf") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("_csrf", "")
        if not expected or not secrets.compare_digest(sent.encode(), expected.encode()):
            abort(400)

@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp

@app.errorhandler(400)
def bad_request(_e):
    flash("❌ Your session expired or the request was invalid. Please try again.", "danger")
    return redirect(request.referrer or url_for("login"))

@app.errorhandler(413)
def too_large(_e):
    flash("❌ File too large. Maximum allowed size is 5 MB.", "danger")
    return redirect(request.referrer or url_for("login"))

def parse_coord(val, lo, hi):
    try:
        v = float(val)
        return v if lo <= v <= hi else None
    except (TypeError, ValueError):
        return None

def valid_signature(head, ext):
    """Check the real file bytes, not just the extension."""
    if ext in ("jpg", "jpeg"):
        return head.startswith(b"\xff\xd8\xff")
    if ext == "png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == "pdf":
        return head.startswith(b"%PDF")
    return False

def save_id_file(file):
    """Validate and store an uploaded ID proof under a random name.
    Returns (stored_filename, error_message)."""
    if not file or not file.filename:
        return None, "❌ Please upload your ID proof (JPG, PNG or PDF)."
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        return None, "❌ Only JPG, PNG or PDF files are allowed for ID proof."
    head = file.stream.read(16)
    file.stream.seek(0)
    if not valid_signature(head, ext):
        return None, "❌ The uploaded file is not a valid JPG, PNG or PDF."
    stored = f"{uuid.uuid4().hex}.{ext}"
    file.save(os.path.join(app.config["UPLOAD_FOLDER"], stored))
    return stored, None

def check_password(user, password):
    """Verify password. Old plain-text passwords are upgraded to hashes on first login."""
    if user.password.startswith(("scrypt:", "pbkdf2:")):
        return check_password_hash(user.password, password)
    if secrets.compare_digest(user.password.encode(), password.encode()):
        user.password = generate_password_hash(password)
        db.session.commit()
        return True
    return False

def validate_common(form, need_phone=True):
    name = form.get("name", "").strip()
    email = form.get("email", "").strip().lower()
    phone = form.get("phone", "").strip()
    password = form.get("password", "")
    if not name or len(name) > 100:
        return "❌ Please enter a valid name."
    if not EMAIL_RE.match(email):
        return "❌ Please enter a valid email address."
    if need_phone and not normalize_phone(phone):
        return "❌ Please enter a valid mobile number (needed to send your verification SMS)."
    if len(password) < 8:
        return "❌ Password must be at least 8 characters."
    if User.query.filter_by(email=email).first():
        return "❌ Email already registered! Please login instead."
    return None

# ---------------------------------------------------------------------------
# AUTHENTICATION / ACCESS CONTROL
# ---------------------------------------------------------------------------

def get_current_user():
    if "user_id" not in session:
        return None
    user = db.session.get(User, session["user_id"])
    if not user or user.status == "Suspended":
        session.clear()
        return None
    return user

def login_required(roles=None, active_only=True):
    """roles: allowed roles. active_only: Pending/Rejected users are sent back to
    the dashboard (which shows them the verification-status screen)."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = get_current_user()
            if not user:
                return redirect(url_for("login"))
            if active_only and user.status != "Active":
                return redirect(url_for("dashboard"))
            if roles and user.role not in roles:
                flash("🚫 You do not have permission to open that page.", "danger")
                return redirect(url_for("dashboard"))
            return fn(user, *args, **kwargs)
        return wrapper
    return deco

# ---------------------------------------------------------------------------
# API ROUTES
# ---------------------------------------------------------------------------

@app.route("/api/ai_chat", methods=["POST"])
def ai_chat():
    data = request.get_json(silent=True) or {}
    user_msg = str(data.get("message", "")).strip()[:500]

    if not user_msg:
        return jsonify({"response": "I'm EcoLoop Live AI! Ask me anything about e-waste rates, pickup scheduling, or government inspection rules."})

    sess_id = session.get("user_id", request.remote_addr)

    if genai_client:
        try:
            if sess_id not in user_chats:
                user_chats[sess_id] = genai_client.chats.create(
                    model="gemini-2.5-flash",
                    config=types.GenerateContentConfig(
                        system_instruction=ECO_SYSTEM_PROMPT,
                        temperature=0.7
                    )
                )

            chat = user_chats[sess_id]
            response = chat.send_message(user_msg)
            return jsonify({"response": str(escape(response.text))})

        except Exception as e:
            print("Gemini API Error:", e)

    msg_lower = user_msg.lower()
    if "rate" in msg_lower or "price" in msg_lower or "cost" in msg_lower:
        res = "💡 <b>Current Base Rates:</b> Laptops ₹1,500/unit, Mobile phones ₹120/unit, Monitors ₹350/unit, PCB boards ₹120/kg. Verified recycling companies offer additional bonuses (+5% to +10%)!"
    elif "pickup" in msg_lower or "sell" in msg_lower or "schedule" in msg_lower:
        res = "🛍️ <b>Selling Steps:</b> Register as a seller with your ID proof, wait for the Govt Inspector to verify your account, then choose a partner and submit your pickup request!"
    elif "verify" in msg_lower or "approve" in msg_lower or "pending" in msg_lower or "id" in msg_lower.split():
        res = "🪪 <b>Account Verification:</b> Sellers and Company Admins upload an ID proof at sign-up. A Govt Inspector reviews it, and you get an email + SMS as soon as your account is approved."
    elif "inspect" in msg_lower or "govt" in msg_lower:
        res = "🏛️ <b>Inspector Duties:</b> Government Officers verify ID proofs, check material authenticity, ensure non-hazardous handling, and audit registered sellers and admins."
    elif "payment" in msg_lower or "payout" in msg_lower or "money" in msg_lower:
        res = "💳 <b>Payouts:</b> Disbursed directly to your account via UPI, Bank Transfer, or Card right after the inspector approves your request."
    else:
        res = f"🌱 I'm your EcoLoop Assistant. You asked: <i>\"{escape(user_msg)}\"</i>. Feel free to ask about e-waste rates, scheduling a pickup, account verification, or payout statuses!"

    return jsonify({"response": res})

@app.route("/api/update_location", methods=["POST"])
def update_location():
    user = get_current_user()
    if not user:
        return jsonify({"status": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    lat = parse_coord(data.get("latitude"), -90, 90)
    lng = parse_coord(data.get("longitude"), -180, 180)
    if lat is None or lng is None:
        return jsonify({"status": "invalid"}), 400

    user.latitude = lat
    user.longitude = lng
    db.session.commit()
    return jsonify({"status": "success", "lat": lat, "lng": lng})

# ---------------------------------------------------------------------------
# MAIN PUBLIC HERO LANDING PAGE AND AUTH ROUTES
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html", page="landing")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "").strip()

        user = User.query.filter_by(email=email).first()

        if user and check_password(user, password):
            if role and user.role.lower() != role.lower():
                flash(f"❌ Account registered as {user.role}. Please use the correct portal.", "danger")
                return render_template("index.html", page="login")

            if user.status == "Suspended":
                flash("❌ Account Suspended by Government Inspector.", "danger")
                return render_template("index.html", page="login")

            session.clear()
            session["user_id"] = user.id
            if user.status == "Active":
                flash(f"✅ Welcome back, {user.name}!", "success")
            return redirect(url_for("dashboard"))
        else:
            flash("❌ Invalid email or password. Please check your login credentials.", "danger")

    return render_template("index.html", page="login")

@app.route("/register/seller", methods=["GET", "POST"])
def register_seller():
    if request.method == "POST":
        err = validate_common(request.form)
        if not err and not request.form.get("gov_id", "").strip():
            err = "❌ Please enter your Govt ID number."
        if err:
            flash(err, "danger")
            return render_template("index.html", page="register_seller")

        stored, err = save_id_file(request.files.get("id_file"))
        if err:
            flash(err, "danger")
            return render_template("index.html", page="register_seller")

        new_user = User(
            name=request.form.get("name").strip(),
            email=request.form.get("email").strip().lower(),
            phone=request.form.get("phone").strip(),
            location=request.form.get("location", "").strip(),
            latitude=parse_coord(request.form.get("latitude"), -90, 90),
            longitude=parse_coord(request.form.get("longitude"), -180, 180),
            gov_id=request.form.get("gov_id").strip(),
            doc_file=stored,
            password=generate_password_hash(request.form.get("password")),
            role="Seller",
            status="Pending"
        )
        db.session.add(new_user)
        db.session.commit()

        session.clear()
        session["user_id"] = new_user.id
        flash("🎉 Account created! Your ID proof was sent to the Govt Inspector. You can start selling once it is verified.", "success")
        return redirect(url_for("dashboard"))

    return render_template("index.html", page="register_seller")

@app.route("/register/admin", methods=["GET", "POST"])
def register_admin():
    if request.method == "POST":
        err = validate_common(request.form)
        company_name = request.form.get("company_name", "").strip()
        company_gst = request.form.get("company_gst", "").strip().upper()
        if not err and (not company_name or not company_gst):
            err = "❌ Company name and GST number are required."
        if not err and not request.form.get("gov_id", "").strip():
            err = "❌ Please enter the admin's Govt ID number."
        if not err and Company.query.filter_by(gst=company_gst).first():
            err = "❌ A company with this GST number is already registered."
        if err:
            flash(err, "danger")
            return render_template("index.html", page="register_admin")

        stored, err = save_id_file(request.files.get("id_file"))
        if err:
            flash(err, "danger")
            return render_template("index.html", page="register_admin")

        location = request.form.get("location", "").strip() or "Corporate Hub"

        # Company stays hidden from sellers until the inspector approves this admin.
        db.session.add(Company(
            id=f"comp-{uuid.uuid4().hex[:8]}",
            name=company_name,
            gst=company_gst,
            location=location,
            bonus=6,
            description="Registered Recycler Partner",
            approved=False
        ))

        new_user = User(
            name=request.form.get("name").strip(),
            email=request.form.get("email").strip().lower(),
            phone=request.form.get("phone").strip(),
            location=location,
            latitude=parse_coord(request.form.get("latitude"), -90, 90),
            longitude=parse_coord(request.form.get("longitude"), -180, 180),
            gov_id=request.form.get("gov_id").strip(),
            doc_file=stored,
            company_name=company_name,
            company_gst=company_gst,
            password=generate_password_hash(request.form.get("password")),
            role="Admin",
            status="Pending"
        )
        db.session.add(new_user)
        db.session.commit()

        session.clear()
        session["user_id"] = new_user.id
        flash("🎉 Company account created! Your GST and ID proof were sent to the Govt Inspector for verification.", "success")
        return redirect(url_for("dashboard"))

    return render_template("index.html", page="register_admin")

@app.route("/register/inspector", methods=["GET", "POST"])
def register_inspector():
    if request.method == "POST":
        err = validate_common(request.form, need_phone=False)
        if err:
            flash(err, "danger")
            return render_template("index.html", page="register_inspector")

        stored, err = save_id_file(request.files.get("id_file"))
        if err:
            flash(err, "danger")
            return render_template("index.html", page="register_inspector")

        new_user = User(
            name=request.form.get("name").strip(),
            email=request.form.get("email").strip().lower(),
            phone=request.form.get("phone", "").strip(),
            gov_id=request.form.get("gov_id", "").strip(),
            doc_file=stored,
            password=generate_password_hash(request.form.get("password")),
            role="Inspector",
            status="Active"
        )
        db.session.add(new_user)
        db.session.commit()

        session.clear()
        session["user_id"] = new_user.id
        flash("🏛️ Govt Inspector Credential Created!", "success")
        return redirect(url_for("dashboard"))

    return render_template("index.html", page="register_inspector")

@app.route("/logout")
def logout():
    session.clear()
    flash("🔒 Logged out safely.", "info")
    return redirect(url_for("login"))

# ---------------------------------------------------------------------------
# PROTECTED ID-PROOF FILES  (only the Inspector, or the file's own owner)
# ---------------------------------------------------------------------------

@app.route("/uploads/<path:filename>")
def uploaded_doc(filename):
    user = get_current_user()
    if not user:
        abort(403)
    owner = User.query.filter_by(doc_file=filename).first()
    if not owner:
        abort(404)
    is_inspector = user.role == "Inspector" and user.status == "Active"
    if not is_inspector and user.id != owner.id:
        abort(403)
    resp = send_from_directory(app.config["UPLOAD_FOLDER"], filename)
    resp.headers["Cache-Control"] = "private, no-store"
    return resp

# ---------------------------------------------------------------------------
# DASHBOARD & GENERAL PAGES
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@login_required(active_only=False)
def dashboard(user):
    # Pending / Rejected users only see the verification-status screen (template handles it)
    if user.status != "Active":
        return render_template("index.html", page="dashboard", user=user, transactions=[], companies=[])

    # DATA PRIVACY FILTER: Sellers only see their own requests; Admin & Inspector see all
    if user.role == "Seller":
        transactions = Transaction.query.filter_by(seller_email=user.email).all()
    else:
        transactions = Transaction.query.all()

    companies = Company.query.filter_by(approved=True).all()
    return render_template("index.html", page="dashboard", user=user, transactions=transactions, companies=companies)

@app.route("/company_detail/<comp_id>")
@login_required(roles=["Seller"])
def company_detail(user, comp_id):
    comp = Company.query.filter_by(id=comp_id, approved=True).first()
    if not comp:
        flash("⚠️ That recycling partner is not available.", "warning")
        return redirect(url_for("sell"))
    products = Product.query.all()
    return render_template("index.html", page="company_detail", user=user, company=comp, products=products)

@app.route("/sell", methods=["GET", "POST"])
@login_required(roles=["Seller"])
def sell(user):
    if request.method == "POST":
        company = Company.query.filter_by(id=request.form.get("company_id"), approved=True).first()
        if not company:
            flash("⚠️ Please choose a valid recycling partner.", "warning")
            return redirect(url_for("sell"))
        products = Product.query.all()

        items_summary = []
        gross = 0
        for p in products:
            try:
                qty = int(request.form.get(f"qty_{p.id}", 0) or 0)
            except ValueError:
                qty = 0
            qty = min(qty, 100000)
            if qty > 0:
                gross += qty * p.rate
                items_summary.append(f"{p.name} ({qty} {p.unit})")

        if gross == 0:
            flash("⚠️ Select a quantity for at least one item.", "warning")
            return redirect(url_for("company_detail", comp_id=company.id))

        bonus = round(gross * (company.bonus / 100))
        fee = round(gross * 0.08)
        payout = gross + bonus - fee

        new_tx = Transaction(
            id=f"REQ-{random.randint(1000, 9999)}",
            date=request.form.get("pickup_date", ""),
            company=company.name,
            seller=user.name,
            seller_email=user.email,
            seller_phone=user.phone or "N/A",
            items=", ".join(items_summary),
            payout=payout,
            status="In Progress"
        )
        db.session.add(new_tx)
        db.session.commit()

        flash(f"✅ Pickup request submitted to {company.name}! Request ID: {new_tx.id}", "success")
        return redirect(url_for("history"))

    companies = Company.query.filter_by(approved=True).all()
    return render_template("index.html", page="sell", user=user, companies=companies)

# ---------------------------------------------------------------------------
# INSPECTOR: ACCOUNT VERIFICATION + MATERIAL INSPECTION
# ---------------------------------------------------------------------------

@app.route("/inspector")
@login_required(roles=["Inspector"])
def inspector(user):
    users = User.query.all()
    pending_users = User.query.filter(User.status == "Pending", User.role != "Inspector").all()
    transactions = Transaction.query.all()
    return render_template("index.html", page="inspector", user=user, users=users,
                           pending_users=pending_users, transactions=transactions)

@app.route("/inspector/review/<int:user_id>")
@login_required(roles=["Inspector"])
def review_user(user, user_id):
    target = db.session.get(User, user_id)
    if not target or target.role == "Inspector":
        flash("⚠️ Account not found.", "warning")
        return redirect(url_for("inspector"))

    doc_kind = "missing"
    if target.doc_file and os.path.isfile(os.path.join(app.config["UPLOAD_FOLDER"], target.doc_file)):
        ext = target.doc_file.rsplit(".", 1)[-1].lower()
        doc_kind = "pdf" if ext == "pdf" else "image"

    seller_tx = Transaction.query.filter_by(seller_email=target.email).all() if target.role == "Seller" else []
    company = (Company.query.filter_by(gst=target.company_gst).first()
               if target.role == "Admin" and target.company_gst else None)

    return render_template("index.html", page="review_user", user=user, target=target,
                           doc_kind=doc_kind, seller_tx=seller_tx, company=company)

@app.route("/inspector/accounts")
@login_required(roles=["Inspector"])
def accounts(user):
    """Inspector can look up any seller / company admin at any time."""
    f_role = request.args.get("role", "")
    f_status = request.args.get("status", "")
    q = request.args.get("q", "").strip()

    query = User.query.filter(User.role != "Inspector")
    if f_role in ("Seller", "Admin"):
        query = query.filter(User.role == f_role)
    if f_status in ("Active", "Pending", "Rejected", "Suspended"):
        query = query.filter(User.status == f_status)
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(User.name.ilike(like), User.email.ilike(like),
                                    User.phone.ilike(like), User.location.ilike(like),
                                    User.company_name.ilike(like), User.gov_id.ilike(like)))
    people = query.order_by(User.id.desc()).all()
    return render_template("index.html", page="accounts", user=user, people=people,
                           f_role=f_role, f_status=f_status, q=q)

@app.route("/inspector/approve/<int:user_id>", methods=["POST"])
@login_required(roles=["Inspector"])
def approve_user(user, user_id):
    target = db.session.get(User, user_id)
    if not target or target.role == "Inspector":
        return redirect(url_for("inspector"))

    target.status = "Active"
    target.reject_reason = None
    target.verified_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    if target.role == "Admin" and target.company_gst:
        Company.query.filter_by(gst=target.company_gst).update({"approved": True})
    db.session.commit()

    message = (f"Hello {target.name}, your EcoLoop account has been verified by the Government Inspector. "
               f"Your account is now active - log in and start your business! 🌱")
    level, report = notify_now(target.email, target.phone, "✅ Your EcoLoop account is verified", message)

    flash(f"✅ {target.name} approved and the account is now active.", "success")
    flash(report, level)
    return redirect(url_for("inspector"))

@app.route("/inspector/reject/<int:user_id>", methods=["POST"])
@login_required(roles=["Inspector"])
def reject_user(user, user_id):
    target = db.session.get(User, user_id)
    if not target or target.role == "Inspector":
        return redirect(url_for("inspector"))

    reason = request.form.get("reason", "").strip()[:300] or "ID proof could not be verified."
    target.status = "Rejected"
    target.reject_reason = reason
    db.session.commit()

    message = (f"Hello {target.name}, we could not verify your EcoLoop account. Reason: {reason} "
               f"Please contact support or register again with a valid ID proof.")
    level, report = notify_now(target.email, target.phone, "❌ EcoLoop account verification failed", message)

    flash(f"⚠️ {target.name} was rejected.", "info")
    flash(report, level)
    return redirect(url_for("inspector"))

@app.route("/inspector/toggle_user/<int:user_id>", methods=["POST"])
@login_required(roles=["Inspector"])
def toggle_user(user, user_id):
    u = db.session.get(User, user_id)
    if u and u.role != "Inspector" and u.status in ("Active", "Suspended"):
        u.status = "Suspended" if u.status == "Active" else "Active"
        db.session.commit()
        flash(f"⚠️ User {u.name} status updated to {u.status}.", "info")
    return redirect(url_for("inspector"))

@app.route("/inspector/verify/<req_id>", methods=["POST"])
@login_required(roles=["Inspector"])
def verify_request(user, req_id):
    t = db.session.get(Transaction, req_id)
    if t and t.status == "In Progress":
        t.status = "Verified"
        db.session.commit()
        flash(f"✅ Request {req_id} physical inspection approved!", "success")
    return redirect(url_for("inspector"))

# ---------------------------------------------------------------------------
# COMPANY ADMIN
# ---------------------------------------------------------------------------

@app.route("/payments", methods=["GET", "POST"])
@login_required(roles=["Admin"])
def payments(user):
    if request.method == "POST":
        req_id = request.form.get("req_id")
        method = request.form.get("method")
        if method not in ("UPI", "Bank Transfer"):
            method = "UPI"
        t = db.session.get(Transaction, req_id)
        if t and t.status == "Verified":
            t.status = "Paid"
            t.payment_method = method
            t.txn_ref = f"TXN-{random.randint(100000, 999999)}"
            db.session.commit()
            flash(f"💸 Payment Disbursed for {req_id}! Ref: {t.txn_ref}", "success")

    verified_tx = Transaction.query.filter_by(status="Verified").all()
    return render_template("index.html", page="payments", user=user, transactions=verified_tx)

@app.route("/products", methods=["GET", "POST"])
@login_required(roles=["Admin"])
def products(user):
    if request.method == "POST":
        try:
            prod_id = int(request.form.get("product_id"))
            rate = int(request.form.get("rate"))
        except (TypeError, ValueError):
            rate = -1
        if rate >= 0:
            p = db.session.get(Product, prod_id)
            if p:
                p.rate = rate
                db.session.commit()
                flash("✅ Product rate updated successfully in SQLite database!", "success")
        else:
            flash("⚠️ Enter a valid rate.", "warning")

    prods = Product.query.all()
    return render_template("index.html", page="products", user=user, products=prods)

@app.route("/sellers")
@login_required(roles=["Admin"])
def sellers(user):
    seller_list = User.query.filter_by(role="Seller", status="Active").all()
    return render_template("index.html", page="sellers", user=user, sellers=seller_list)

# ---------------------------------------------------------------------------
# SHARED PAGES (active accounts only)
# ---------------------------------------------------------------------------

@app.route("/history")
@login_required()
def history(user):
    if user.role == "Seller":
        tx_list = Transaction.query.filter_by(seller_email=user.email).all()
    else:
        tx_list = Transaction.query.all()

    return render_template("index.html", page="history", user=user, transactions=tx_list)

@app.route("/settings", methods=["GET", "POST"])
@login_required()
def settings(user):
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            user.name = name[:100]
        user.phone = request.form.get("phone", "").strip()[:20]
        if "location" in request.form:
            user.location = request.form.get("location", "").strip()[:200]
        db.session.commit()
        flash("⚙️ Profile & Account Settings saved!", "success")

    return render_template("index.html", page="settings", user=user)

@app.route("/help")
@login_required()
def help_page(user):
    return render_template("index.html", page="help", user=user)

# ---------------------------------------------------------------------------
# ENTRYPOINT: CHECK FOR CLI COMMANDS BEFORE STARTING FLASK SERVER
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        migrate_schema()
        seed_initial_data()

    if len(sys.argv) > 1 and sys.argv[1] == "view-data":
        print_database_data()
    elif len(sys.argv) > 1 and sys.argv[1] == "test-notify":
        # usage: python app.py test-notify you@gmail.com 9876543210
        t_email = sys.argv[2] if len(sys.argv) > 2 else ""
        t_phone = sys.argv[3] if len(sys.argv) > 3 else ""
        print("EMAIL:", send_email(t_email, "EcoLoop test", "This is a test message from EcoLoop."))
        print("SMS:  ", send_sms(t_phone, "EcoLoop test message."))
    else:
        # debug=True exposes an interactive console - keep it OFF unless FLASK_DEBUG=1 (dev only)
        if __name__ == '__main__': app.run(debug=True, port=8000)
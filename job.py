"""
=====================================================================
 Wambui Shadrack Advocates — Legal Portal Backend (v2, production)
 Flask + PostgreSQL + M-Pesa Daraja STK Push + Stripe + Resend Email
 Two portals: Client (case number) + Staff (email → role detected)
=====================================================================
DEPLOY (Render):
  Start command:  gunicorn -w 2 -k gthread --threads 8 -t 120 app:app
"""
import os
import random
import logging
import base64
import json
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import quote
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as pgpool
from requests.auth import HTTPBasicAuth
from flask import (
    Flask, request, jsonify, g, send_from_directory, abort
)
from flask_cors import CORS
from werkzeug.utils import secure_filename
import stripe

# =========================================================
# ⚙️ APP CONFIG
# =========================================================
app = Flask(__name__)

frontend_origins = os.environ.get("FRONTEND_URL", "*").split(",")
CORS(app, resources={r"/api/*": {"origins": frontend_origins, "supports_credentials": False}})

app.config['DATABASE_URL'] = os.environ.get(
    'DATABASE_URL',
    'dbname=postgres user=postgres password=jose1023 host=localhost port=5432'
)
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', './client_docs/')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024  # 25 MB upload cap

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
SYSTEM_STATE = {"LOCKDOWN_MODE": False}

# =========================================================
# 🗄️ DATABASE — Connection Pool
# =========================================================
DB_POOL = None

def init_pool():
    global DB_POOL
    if DB_POOL is None:
        DB_POOL = pgpool.ThreadedConnectionPool(
            minconn=1, maxconn=10,
            dsn=app.config['DATABASE_URL'],
            cursor_factory=RealDictCursor
        )
        logging.info("✅ DB pool initialized")

def get_db():
    if 'db' not in g:
        if DB_POOL is None:
            init_pool()
        g.db = DB_POOL.getconn()
    return g.db

@app.teardown_appcontext
def close_db(_e=None):
    db = g.pop('db', None)
    if db is not None:
        try:
            db.rollback()
        except Exception:
            pass
        DB_POOL.putconn(db)

# =========================================================
# 🛠️ DB SCHEMA + SEED
# =========================================================
def init_db():
    init_pool()
    conn = DB_POOL.getconn()
    try:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id SERIAL PRIMARY KEY,
                full_name VARCHAR(255) NOT NULL,
                phone_number VARCHAR(50) UNIQUE,
                email VARCHAR(255) UNIQUE,
                role VARCHAR(50) NOT NULL
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_users_email_lower ON users (LOWER(email));")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS otp_vault_email (
                email VARCHAR(255) PRIMARY KEY,
                code VARCHAR(6) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                case_id SERIAL PRIMARY KEY,
                case_number VARCHAR(255) UNIQUE NOT NULL,
                case_parties TEXT,
                client_name VARCHAR(255),
                client_phone VARCHAR(50),
                client_email VARCHAR(255),
                next_court_date VARCHAR(255),
                coming_up_for TEXT,
                matter_notes TEXT,
                total_balance NUMERIC(15,2) DEFAULT 0.00,
                paid_balance NUMERIC(15,2) DEFAULT 0.00,
                ai_access_granted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cases_number ON cases (case_number);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cases_number_lower ON cases (LOWER(case_number));")
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS case_documents (
                doc_id SERIAL PRIMARY KEY,
                case_number VARCHAR(255) NOT NULL,
                filename VARCHAR(500) NOT NULL,
                original_name VARCHAR(500),
                file_size BIGINT,
                uploaded_by_role VARCHAR(50) NOT NULL,
                uploaded_by_name VARCHAR(255),
                visible_to_client BOOLEAN DEFAULT TRUE,
                upload_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_docs_case ON case_documents (case_number);")
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_client_logs (
                log_id SERIAL PRIMARY KEY,
                case_number VARCHAR(255),
                client_name VARCHAR(255),
                actor VARCHAR(50),
                question TEXT NOT NULL,
                ai_response TEXT NOT NULL,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mpesa_transactions (
                tx_id SERIAL PRIMARY KEY,
                case_number VARCHAR(255),
                phone_number VARCHAR(50),
                amount NUMERIC(15,2),
                purpose VARCHAR(50) DEFAULT 'balance',
                merchant_request_id VARCHAR(255),
                checkout_request_id VARCHAR(255) UNIQUE,
                mpesa_receipt VARCHAR(255),
                result_code INTEGER,
                result_desc TEXT,
                status VARCHAR(50) DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stripe_transactions (
                tx_id SERIAL PRIMARY KEY,
                case_number VARCHAR(255),
                amount NUMERIC(15,2),
                currency VARCHAR(10),
                stripe_session_id VARCHAR(255) UNIQUE,
                stripe_payment_intent VARCHAR(255),
                status VARCHAR(50) DEFAULT 'PENDING',
                customer_email VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            );
        """)
        
        seed_users = [
            ('Shadrack Wambui', '0700260086', 'shadrack@wambuishadrack.co.ke', 'admin'),
            ('Jeff Kangethe',   '0704704758', 'jeff@wambuishadrack.co.ke',     'advocate'),
            ('Jane Onyango',    '0795204923', 'jane@wambuishadrack.co.ke',     'secretary'),
        ]
        for name, phone, email, role in seed_users:
            cur.execute("""
                INSERT INTO users (full_name, phone_number, email, role)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role, full_name = EXCLUDED.full_name;
            """, (name, phone, email, role))
        conn.commit()
        cur.close()
        logging.info("💾 Database schema synchronized.")
    except Exception as e:
        conn.rollback()
        logging.exception(f"DB init failure: {e}")
    finally:
        DB_POOL.putconn(conn)

# =========================================================
# 🔑 HELPERS
# =========================================================
def _normalize_phone(phone: str) -> str:
    p = str(phone or '').strip().replace(' ', '').replace('-', '').replace('+', '')
    if p.startswith('0') and len(p) == 10:
        p = '254' + p[1:]
    elif p.startswith('7') and len(p) == 9:
        p = '254' + p
    return p

def _normalize_email(value: str) -> str:
    return (value or '').strip().lower()

def json_error(msg, code=400, **extra):
    payload = {"success": False, "message": msg}
    payload.update(extra)
    return jsonify(payload), code

# =========================================================
# 📧 RESEND EMAIL (OTP)
# =========================================================
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "onboarding@resend.dev")

def send_otp_email(email: str, otp: str, name: str = ""):
    if not RESEND_API_KEY:
        logging.warning(f"📭 STUB email to {email}: OTP={otp}")
        return False, "RESEND_API_KEY missing"
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "from": RESEND_FROM,
                "to": [email],
                "subject": "Wambui Shadrack Advocates — Verification Code",
                "html": f"""
                    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;">
                      <h2 style="color:#0a2540;">Wambui Shadrack &amp; Associates</h2>
                      <p>Hello {name or 'Counsel'},</p>
                      <p>Your secure verification code is:</p>
                      <div style="font-size:32px;font-weight:bold;letter-spacing:8px;color:#c9a961;
                                  background:#f7f5ef;padding:16px;text-align:center;border-radius:8px;">
                        {otp}
                      </div>
                      <p style="color:#666;font-size:13px;">This code expires in 10 minutes.</p>
                    </div>
                """,
            },
            timeout=15,
        )
        if r.status_code in (200, 201):
            return True, "delivered"
        return False, f"Resend {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"Email exception: {e}"

# =========================================================
# 💰 M-PESA DARAJA
# =========================================================
MPESA_ENV = os.environ.get('MPESA_ENV', 'sandbox').lower()
MPESA_CONSUMER_KEY = os.environ.get('MPESA_CONSUMER_KEY', '')
MPESA_CONSUMER_SECRET = os.environ.get('MPESA_CONSUMER_SECRET', '')
MPESA_SHORTCODE = os.environ.get('MPESA_SHORTCODE', '4747331')
MPESA_PASSKEY = os.environ.get('MPESA_PASSKEY', '')
MPESA_CALLBACK_URL = os.environ.get('MPESA_CALLBACK_URL', '')
MPESA_TRANSACTION_TYPE = os.environ.get('MPESA_TRANSACTION_TYPE', 'CustomerPayBillOnline')
MPESA_BASE = 'https://api.safaricom.co.ke' if MPESA_ENV == 'production' else 'https://sandbox.safaricom.co.ke'

def get_mpesa_token():
    if not MPESA_CONSUMER_KEY or not MPESA_CONSUMER_SECRET:
        raise RuntimeError("M-Pesa credentials not configured.")
    r = requests.get(
        f"{MPESA_BASE}/oauth/v1/generate?grant_type=client_credentials",
        auth=HTTPBasicAuth(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET),
        timeout=20,
    )
    r.raise_for_status()
    tok = r.json().get('access_token')
    if not tok:
        raise RuntimeError(f"Daraja token error: {r.text}")
    return tok

def initiate_stk_push(phone, amount, account_ref, description="Legal Fees"):
    token = get_mpesa_token()
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    password = base64.b64encode(
        f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{ts}".encode()
    ).decode('utf-8')
    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": ts,
        "TransactionType": MPESA_TRANSACTION_TYPE,
        "Amount": int(round(float(amount))),
        "PartyA": _normalize_phone(phone),
        "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": _normalize_phone(phone),
        "CallBackURL": MPESA_CALLBACK_URL,
        "AccountReference": (account_ref or "LegalFees")[:12],
        "TransactionDesc": (description or "Legal Fees")[:13],
    }
    r = requests.post(
        f"{MPESA_BASE}/mpesa/stkpush/v1/processrequest",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    logging.info(f"STK ({r.status_code}): {data}")
    return r.status_code, data

# =========================================================
# 💳 STRIPE
# =========================================================
stripe.api_key = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
STRIPE_SUCCESS_URL = os.environ.get('STRIPE_SUCCESS_URL', 'https://example.com/success')
STRIPE_CANCEL_URL = os.environ.get('STRIPE_CANCEL_URL', 'https://example.com/cancel')
STRIPE_CURRENCY = os.environ.get('STRIPE_CURRENCY', 'kes').lower()

# =========================================================
# 🛡️ ROLE-CHECK MIDDLEWARE
# =========================================================
def require_staff(roles=('admin', 'advocate', 'secretary')):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            email = _normalize_email(request.headers.get('X-User-Email', ''))
            if not email:
                return json_error("Authentication required.", 401)
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT role, full_name FROM users WHERE LOWER(email)=%s", (email,))
            row = cur.fetchone()
            if not row or row['role'] not in roles:
                return json_error("Forbidden.", 403)
            g.current_user = {"email": email, "role": row['role'], "name": row['full_name']}
            return fn(*args, **kwargs)
        return wrapper
    return deco

# =========================================================
# 🩺 HEALTH
# =========================================================
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat()})

# =========================================================
# 🔐 AUTH
# =========================================================
@app.route('/api/auth/login-router', methods=['POST'])
def login_router():
    payload = request.get_json(silent=True) or {}
    credential = (payload.get('credential') or '').strip()
    if not credential:
        return json_error("Login field cannot be blank.")
    if '@' in credential:
        return initiate_staff_login(_normalize_email(credential))
    return client_login(credential)

def initiate_staff_login(email):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT full_name, role FROM users
            WHERE LOWER(email)=%s AND role IN ('admin','advocate','secretary');
        """, (email,))
        account = cur.fetchone()
        if not account:
            return json_error("Access denied: not registered staff.", 403)
        otp = str(random.randint(100000, 999999))
        cur.execute("""
            INSERT INTO otp_vault_email (email, code, expires_at)
            VALUES (%s, %s, NOW() + INTERVAL '10 minutes')
            ON CONFLICT (email) DO UPDATE
              SET code=EXCLUDED.code,
                  created_at=CURRENT_TIMESTAMP,
                  expires_at=EXCLUDED.expires_at;
        """, (email, otp))
        conn.commit()
        ok, info = send_otp_email(email, otp, account['full_name'])
        logging.info(f"OTP for {email}: ok={ok} info={info}")
        return jsonify({
            "success": True,
            "mode": "otp_required",
            "role_preview": account['role'],
            "message": f"OTP sent to {email}." if ok else f"OTP saved (email failed: {info})"
        })
    except Exception as e:
        logging.exception("Staff login error")
        return json_error(f"Auth fault: {e}", 500)

@app.route('/api/auth/verify-otp', methods=['POST'])
def verify_otp():
    data = request.get_json(silent=True) or {}
    email = _normalize_email(data.get('email') or '')
    code = (data.get('code') or '').strip()
    if not email or not code:
        return json_error("Email and code required.")
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT code FROM otp_vault_email
            WHERE email=%s AND expires_at > NOW();
        """, (email,))
        rec = cur.fetchone()
        if not rec or rec['code'] != code:
            return json_error("Invalid or expired OTP.", 401)
        cur.execute("DELETE FROM otp_vault_email WHERE email=%s;", (email,))
        cur.execute("SELECT full_name, role FROM users WHERE LOWER(email)=%s;", (email,))
        prof = cur.fetchone()
        conn.commit()
        if not prof:
            return json_error("Staff profile missing.", 404)
        return jsonify({
            "success": True,
            "email": email,
            "role": prof['role'],
            "user_name": prof['full_name'],
        })
    except Exception as e:
        logging.exception("verify error")
        return json_error(f"Vault read error: {e}", 500)

def client_login(case_number):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT case_id, case_number, case_parties, client_name,
                   ai_access_granted, next_court_date, coming_up_for,
                   total_balance, paid_balance
            FROM cases WHERE LOWER(case_number) = LOWER(%s)
            LIMIT 1
        """, (case_number,))
        case = cur.fetchone()
        if not case:
            return json_error("No case found for that number.", 404)
        total = float(case['total_balance'] or 0)
        paid = float(case['paid_balance'] or 0)
        score = random.randint(55, 98)
        return jsonify({
            "success": True,
            "mode": "client_dashboard",
            "data": {
                "case_id": case['case_id'],
                "case_number": case['case_number'],
                "case_parties": case['case_parties'],
                "client_name": case['client_name'],
                "next_court_date": str(case['next_court_date'] or ''),
                "coming_up_for": case['coming_up_for'],
                "financials": {"total": total, "paid": paid, "balance": total - paid},
                "ai_unlocked": case['ai_access_granted'],
                "case_predictor": {"score": score, "analysis": f"Outcome trends at {score}% favorable."}
            }
        })
    except Exception as e:
        logging.exception("client login")
        return json_error(f"DB failure: {e}", 500)

# =========================================================
# 📂 DOCUMENTS
# =========================================================
@app.route('/api/documents/client-upload', methods=['POST'])
def client_upload():
    file = request.files.get('file')
    case_number = (request.form.get('case_number') or '').strip()
    if not file or not case_number:
        return json_error("Missing file or case number.")
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT client_name FROM cases WHERE LOWER(case_number)=LOWER(%s)", (case_number,))
    case = cur.fetchone()
    if not case:
        return json_error("Case not found.", 404)
    original = file.filename or 'upload.bin'
    safe = secure_filename(original)
    stamp = datetime.now().strftime('%Y%m%d%H%M%S')
    stored = f"{case_number.replace('/', '_')}__{stamp}__{safe}"
    path = os.path.join(app.config['UPLOAD_FOLDER'], stored)
    file.save(path)
    size = os.path.getsize(path)
    cur.execute("""
        INSERT INTO case_documents
        (case_number, filename, original_name, file_size,
         uploaded_by_role, uploaded_by_name, visible_to_client)
        VALUES (%s,%s,%s,%s,'client',%s,TRUE)
        RETURNING doc_id
    """, (case_number, stored, original, size, case['client_name']))
    doc_id = cur.fetchone()['doc_id']
    conn.commit()
    return jsonify({"success": True, "doc_id": doc_id, "filename": stored})

@app.route('/api/staff/upload-document', methods=['POST'])
@require_staff()
def staff_upload():
    file = request.files.get('file')
    case_number = (request.form.get('case_number') or '').strip()
    visible = (request.form.get('visible_to_client') or 'true').lower() == 'true'
    if not file or not case_number:
        return json_error("Missing file or case number.")
    original = file.filename or 'upload.bin'
    safe = secure_filename(original)
    stamp = datetime.now().strftime('%Y%m%d%H%M%S')
    stored = f"{case_number.replace('/', '_')}__{stamp}__{safe}"
    path = os.path.join(app.config['UPLOAD_FOLDER'], stored)
    file.save(path)
    size = os.path.getsize(path)
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO case_documents
        (case_number, filename, original_name, file_size,
         uploaded_by_role, uploaded_by_name, visible_to_client)
        VALUES (%s,%s,%s,%s,'staff',%s,%s)
        RETURNING doc_id
    """, (case_number, stored, original, size, g.current_user['name'], visible))
    doc_id = cur.fetchone()['doc_id']
    conn.commit()
    return jsonify({"success": True, "doc_id": doc_id, "filename": stored})

@app.route('/api/documents/list', methods=['GET'])
def list_docs():
    case_number = (request.args.get('case_number') or '').strip()
    email = _normalize_email(request.headers.get('X-User-Email', ''))
    is_staff = False
    if email:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT role FROM users WHERE LOWER(email)=%s", (email,))
        r = cur.fetchone()
        is_staff = r and r['role'] in ('admin', 'advocate', 'secretary')
    conn = get_db(); cur = conn.cursor()
    if is_staff and not case_number:
        cur.execute("SELECT * FROM case_documents ORDER BY upload_date DESC LIMIT 500")
    elif is_staff:
        cur.execute("""
            SELECT * FROM case_documents WHERE LOWER(case_number)=LOWER(%s)
            ORDER BY upload_date DESC
        """, (case_number,))
    else:
        if not case_number:
            return json_error("case_number required.", 400)
        cur.execute("""
            SELECT * FROM case_documents
            WHERE LOWER(case_number)=LOWER(%s) AND visible_to_client=TRUE
            ORDER BY upload_date DESC
        """, (case_number,))
    docs = cur.fetchall()
    return jsonify({"success": True, "documents": docs})

@app.route('/api/documents/download/<path:filename>', methods=['GET'])
@require_staff()
def staff_download(filename):
    safe_name = secure_filename(filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_name)
    if not os.path.exists(file_path):
        return json_error("File not found.", 404)
    return send_from_directory(app.config['UPLOAD_FOLDER'], safe_name, as_attachment=True)

@app.route('/api/documents/<int:doc_id>', methods=['DELETE'])
@require_staff()
def delete_doc(doc_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT filename FROM case_documents WHERE doc_id=%s", (doc_id,))
    row = cur.fetchone()
    if not row:
        return json_error("Document not found.", 404)
    try:
        os.remove(os.path.join(app.config['UPLOAD_FOLDER'], row['filename']))
    except OSError:
        pass
    cur.execute("DELETE FROM case_documents WHERE doc_id=%s", (doc_id,))
    conn.commit()
    return jsonify({"success": True})

# =========================================================
# 📁 STAFF CASE CRUD
# =========================================================
@app.route('/api/staff/cases', methods=['GET'])
@require_staff()
def list_cases():
    q = (request.args.get('q') or '').strip()
    conn = get_db(); cur = conn.cursor()
    
    if q:
        like = f"%{q}%"
        cur.execute("""
            SELECT * FROM cases
            WHERE case_number ILIKE %s OR case_parties ILIKE %s OR client_name ILIKE %s
            ORDER BY updated_at DESC LIMIT 500
        """, (like, like, like))
    else:
        cur.execute("SELECT * FROM cases ORDER BY updated_at DESC LIMIT 500")
        
    raw_rows = cur.fetchall()
    clean_rows = []
    
    for r in raw_rows:
        case_dict = dict(r)
        # Fix the decimal parsing issue
        case_dict['total_balance'] = float(case_dict.get('total_balance') or 0.0)
        case_dict['paid_balance'] = float(case_dict.get('paid_balance') or 0.0)
        
        if g.current_user['role'] != 'admin':
            case_dict.pop('total_balance', None)
            case_dict.pop('paid_balance', None)
            
        clean_rows.append(case_dict)
        
    return jsonify({"success": True, "cases": clean_rows})

@app.route('/api/staff/cases', methods=['POST'])
@require_staff()
def create_case():
    d = request.get_json(silent=True) or {}
    case_number = (d.get('case_number') or '').strip()
    if not case_number:
        return json_error("case_number required.")
    is_admin = g.current_user['role'] == 'admin'
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO cases (case_number, case_parties, client_name, client_phone, client_email,
                               next_court_date, coming_up_for, matter_notes,
                               total_balance, paid_balance)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING *
        """, (
            case_number,
            d.get('case_parties'),
            d.get('client_name'),
            d.get('client_phone'),
            d.get('client_email'),
            d.get('next_court_date'),
            d.get('coming_up_for'),
            d.get('matter_notes'),
            float(d.get('total_balance') or 0) if is_admin else 0,
            float(d.get('paid_balance') or 0) if is_admin else 0,
        ))
        case = cur.fetchone()
        conn.commit()
        return jsonify({"success": True, "case": case})
    except psycopg2.IntegrityError:
        conn.rollback()
        return json_error("Case number already exists.", 409)

@app.route('/api/staff/cases/<int:case_id>', methods=['PATCH'])
@require_staff()
def update_case(case_id):
    d = request.get_json(silent=True) or {}
    is_admin = g.current_user['role'] == 'admin'
    editable = ['case_number', 'case_parties', 'client_name', 
                'next_court_date', 'coming_up_for', 'matter_notes']
    if is_admin:
        editable += ['total_balance', 'paid_balance', 'ai_access_granted']
    sets, vals = [], []
    for k in editable:
        if k in d:
            sets.append(f"{k}=%s")
            vals.append(d[k])
    if not sets:
        return json_error("No editable fields supplied.")
    sets.append("updated_at=CURRENT_TIMESTAMP")
    vals.append(case_id)
    conn = get_db(); cur = conn.cursor()
    cur.execute(f"UPDATE cases SET {', '.join(sets)} WHERE case_id=%s RETURNING *", vals)
    row = cur.fetchone()
    conn.commit()
    if not row:
        return json_error("Case not found.", 404)
    return jsonify({"success": True, "case": row})

@app.route('/api/staff/search/<int:case_id>', methods=['DELETE'])
@require_staff(roles=('admin',))
def delete_case(case_id):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM cases WHERE case_id=%s", (case_id,))
    conn.commit()
    return list_cases()
    return jsonify({"success": True})

# =========================================================
# 🤖 AI
# =========================================================
LOVABLE_API_KEY = os.environ.get('LOVABLE_API_KEY', '')
AI_MODEL = os.environ.get('AI_MODEL', 'google/gemini-2.5-flash')
LEGAL_SYSTEM_PROMPT = """You are a Kenyan legal research assistant for Wambui Shadrack & Associates Advocates.
GROUNDING RULES (strict):
1. Base every answer on the Constitution of Kenya, 2010.
2. Reference Kenyan landmark presidential election petitions where relevant.
3. Use 4-part structure: ISSUE, LAW, APPLICATION, CONCLUSION.
"""

@app.route('/api/ai/consult', methods=['POST'])
def ai_consult():
    data = request.get_json(silent=True) or {}
    question = (data.get('question') or '').strip()
    actor = (data.get('actor') or 'client').lower()
    case_number = (data.get('case_number') or '').strip()
    user_name = (data.get('user_name') or '').strip()
    
    if not question:
        return json_error("Question cannot be blank.")
        
    if actor == 'client':
        tone = "plain English, brief"
    else:
        tone = "advocate-grade with full citations"
        
    if not LOVABLE_API_KEY:
        answer = "[Offline AI] ISSUE: Configure LOVABLE_API_KEY for full analysis."
    else:
        try:
            r = requests.post(
                "https://ai.gateway.lovable.dev/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {LOVABLE_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": AI_MODEL,
                    "messages": [
                        {"role": "system", "content": LEGAL_SYSTEM_PROMPT + f"\nAnswer in {tone}."},
                        {"role": "user", "content": question},
                    ],
                    "temperature": 0.3,
                },
                timeout=60,
            )
            r.raise_for_status()
            answer = r.json()['choices'][0]['message']['content']
        except Exception as e:
            return json_error(f"AI failure: {e}", 502)

    # Log the AI interaction
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO ai_client_logs (case_number, client_name, actor, question, ai_response)
            VALUES (%s, %s, %s, %s, %s)
        """, (case_number, user_name, actor, question, answer))
        conn.commit()
    except Exception as e:
        logging.error(f"Failed to log AI consult: {e}")
  # =========================================================
# 🆕 MISSING STAFF / ADMIN / SYSTEM ENDPOINTS
# =========================================================
from flask import request, jsonify

# ---------- helper: alias add-matter -> cases ----------
@app.route('/api/staff/add-matter', methods=['POST'])
@require_staff(('admin', 'advocate', 'secretary'))
def staff_add_matter_alias():
    """Alias so frontend POST /api/staff/add-matter works."""
    data = request.get_json(force=True) or {}
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            INSERT INTO cases
              (case_number, client_name, client_phone, client_email,
               matter_type, description, status, total_fee, balance,
               next_court_date, assigned_to, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, NOW())
            RETURNING id, case_number
        """, (
            data.get('case_number'),
            data.get('client_name'),
            data.get('client_phone'),
            data.get('client_email'),
            data.get('matter_type'),
            data.get('description'),
            data.get('status', 'open'),
            data.get('total_fee', 0),
            data.get('balance', data.get('total_fee', 0)),
            data.get('next_court_date'),
            data.get('assigned_to'),
        ))
        row = cur.fetchone()
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "id": row[0], "case_number": row[1]})
    except Exception as e:
        app.logger.error(f"add-matter failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- helper: alias update-matter -> PATCH cases ----------
@app.route('/api/staff/update-matter', methods=['POST'])
@require_staff(('admin', 'advocate', 'secretary'))
def staff_update_matter_alias():
    data = request.get_json(force=True) or {}
    matter_id = data.get('id')
    if not matter_id:
        return jsonify({"success": False, "error": "id required"}), 400
    fields, values = [], []
    for k in ('status', 'description', 'next_court_date',
              'total_fee', 'balance', 'notes', 'assigned_to'):
        if k in data:
            fields.append(f"{k} = %s")
            values.append(data[k])
    if not fields:
        return jsonify({"success": False, "error": "no fields"}), 400
    values.append(matter_id)
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute(f"UPDATE cases SET {', '.join(fields)}, updated_at=NOW() WHERE id=%s", values)
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        app.logger.error(f"update-matter failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- alias staff-upload -> upload-document ----------
@app.route('/api/documents/staff-upload', methods=['POST'])
@require_staff(('admin', 'advocate', 'secretary'))
def staff_upload_alias():
    # delegate to existing handler
    return staff_upload_document()  # type: ignore[name-defined]


# ---------- AI monitoring ----------
@app.route('/api/staff/ai-monitoring', methods=['GET'])
@require_staff(('admin', 'advocate', 'secretary'))
def staff_ai_monitoring():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT id, user_email, query, response, tokens_used, created_at
            FROM ai_queries
            ORDER BY created_at DESC
            LIMIT 100
        """)
        rows = [
            {"id": r[0], "user_email": r[1], "query": r[2],
             "response": r[3], "tokens_used": r[4],
             "created_at": r[5].isoformat() if r[5] else None}
            for r in cur.fetchall()
        ]
        cur.close(); conn.close()
        return jsonify({"success": True, "queries": rows, "total": len(rows)})
    except Exception as e:
        app.logger.error(f"ai-monitoring failed: {e}")
        return jsonify({"success": False, "queries": [], "error": str(e)}), 200


# ---------- system metrics ----------
@app.route('/api/system/metrics', methods=['GET'])
@require_staff(('admin', 'advocate', 'secretary'))
def system_metrics():
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM cases"); cases = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM cases WHERE status='open'"); open_cases = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM mpesa_transactions WHERE status='Success'"); paid = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(amount),0) FROM mpesa_transactions WHERE status='Success'"); revenue = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ai_queries"); ai_calls = cur.fetchone()[0]
        cur.close(); conn.close()
        return jsonify({
            "success": True,
            "lockdown": SYSTEM_STATE.get('LOCKDOWN_MODE', False),
            "ai_unlocked": SYSTEM_STATE.get('AI_UNLOCKED', True),
            "metrics": {
                "total_cases": cases,
                "open_cases": open_cases,
                "paid_transactions": paid,
                "revenue_kes": float(revenue or 0),
                "ai_calls": ai_calls,
            }
        })
    except Exception as e:
        app.logger.error(f"system metrics failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- admin: system override (lockdown switch) ----------
@app.route('/api/admin/system-override', methods=['POST'])
@require_staff(('admin',))
def admin_system_override():
    data = request.get_json(force=True) or {}
    action = data.get('action')  # 'lockdown_on' | 'lockdown_off' | 'ai_lock' | 'ai_unlock'
    if action == 'lockdown_on':
        SYSTEM_STATE['LOCKDOWN_MODE'] = True
    elif action == 'lockdown_off':
        SYSTEM_STATE['LOCKDOWN_MODE'] = False
    elif action == 'ai_lock':
        SYSTEM_STATE['AI_UNLOCKED'] = False
    elif action == 'ai_unlock':
        SYSTEM_STATE['AI_UNLOCKED'] = True
    else:
        return jsonify({"success": False, "error": "unknown action"}), 400
    return jsonify({"success": True, "state": SYSTEM_STATE})


# ---------- billing: AI unlock (after payment) ----------
@app.route('/api/billing/ai-unlock', methods=['POST'])
@require_staff(('admin', 'advocate', 'secretary'))
def billing_ai_unlock():
    data = request.get_json(force=True) or {}
    case_number = data.get('case_number')
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("UPDATE cases SET ai_unlocked=TRUE WHERE case_number=%s", (case_number,))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "case_number": case_number, "ai_unlocked": True})
    except Exception as e:
        app.logger.error(f"ai-unlock failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
      
    return jsonify({"success": True, "answer": answer})
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
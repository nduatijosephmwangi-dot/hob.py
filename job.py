"""
=====================================================================
 Wambui Shadrack Associates — Legal Portal Backend (v3, production)
 Flask + PostgreSQL + M-Pesa Daraja STK Push + Resend Email + Scheduler
=====================================================================
"""
import os
import random
import logging
import base64
import atexit
from datetime import datetime, date, timedelta
from functools import wraps
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as pgpool
from requests.auth import HTTPBasicAuth
from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler
import stripe

# =========================================================
# ⚙️ APP CONFIG
# =========================================================
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

app.config['DATABASE_URL'] = os.environ.get('DATABASE_URL', 'dbname=postgres user=postgres password=jose1023 host=localhost port=5432')
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', './client_docs/')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
SYSTEM_STATE = {"LOCKDOWN_MODE": False}

# =========================================================
# 🗄️ DATABASE CONNECTION POOL
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
        logging.info("✅ PostgreSQL pool initialized")

def get_db():
    if 'db' not in g:
        if DB_POOL is None: init_pool()
        g.db = DB_POOL.getconn()
    return g.db

@app.teardown_appcontext
def close_db(_e=None):
    db = g.pop('db', None)
    if db is not None:
        try: db.rollback()
        except Exception: pass
        DB_POOL.putconn(db)

# =========================================================
# 🛠️ DB SCHEMA + SEED
# =========================================================
def init_db():
    init_pool()
    conn = DB_POOL.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id SERIAL PRIMARY KEY,
                    full_name VARCHAR(255) NOT NULL,
                    phone_number VARCHAR(50) UNIQUE,
                    email VARCHAR(255) UNIQUE,
                    role VARCHAR(50) NOT NULL
                );
                CREATE TABLE IF NOT EXISTS otp_vault_email (
                    email VARCHAR(255) PRIMARY KEY,
                    code VARCHAR(6) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cases (
                    case_id SERIAL PRIMARY KEY,
                    case_number VARCHAR(255) UNIQUE NOT NULL,
                    client_name VARCHAR(255),
                    case_parties VARCHAR(255),   
                    next_court_date VARCHAR(255),
                    coming_up_for TEXT,
                    total_balance NUMERIC(15,2) DEFAULT 0.00,
                    paid_balance NUMERIC(15,2) DEFAULT 0.00,
                    ai_access_granted BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
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
            
            seed_users = [
                ('Shadrack Wambui', '0700260086', 'shadrack@wambuishadrack.co.ke', 'admin'),
                ('Jeff Kangethe',   '0704704758', 'nduatijosephmwangi@gmail.com',     'advocate'),
                ('Jane Onyango',    '0795204923', 'jane@wambuishadrack.co.ke',     'secretary'),
            ]
            for name, phone, email, role in seed_users:
                cur.execute("""
                    INSERT INTO users (full_name, phone_number, email, role)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (email) DO UPDATE SET role = EXCLUDED.role, full_name = EXCLUDED.full_name;
                """, (name, phone, email, role))
        conn.commit()
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
    if p.startswith('0') and len(p) == 10: p = '254' + p[1:]
    elif p.startswith('7') and len(p) == 9: p = '254' + p
    return p

def _normalize_email(value: str) -> str:
    return (value or '').strip().lower()

def json_error(msg, code=400, **extra):
    payload = {"success": False, "message": msg}
    payload.update(extra)
    return jsonify(payload), code

def require_staff(roles=('admin', 'advocate', 'secretary')):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if SYSTEM_STATE['LOCKDOWN_MODE']:
                return json_error("SYSTEM IN LOCKDOWN.", 403)
            email = _normalize_email(request.headers.get('X-User-Email', ''))
            if not email:
                return json_error("Authentication required.", 401)
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute("SELECT role, full_name FROM users WHERE LOWER(email)=%s", (email,))
                row = cur.fetchone()
            if not row or row['role'] not in roles:
                return json_error("Forbidden.", 403)
            g.current_user = {"email": email, "role": row['role'], "name": row['full_name']}
            return fn(*args, **kwargs)
        return wrapper
    return deco

# =========================================================
# 📧 RESEND EMAIL ENGINE
# =========================================================
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "onboarding@resend.dev")

def send_generic_email(to_email: str, subject: str, html_body: str):
    if not RESEND_API_KEY:
        logging.warning("Resend API key missing. Email not sent.")
        return False
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": RESEND_FROM, "to": [to_email], "subject": subject, "html": html_body},
            timeout=15,
        )
        return r.status_code in (200, 201)
    except Exception as e:
        logging.error(f"Email exception: {e}")
        return False

def send_otp_email(email: str, otp: str, name: str = ""):
    html = f"""
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
    """
    return send_generic_email(email, "Wambui Shadrack Advocates — Verification Code", html)

# =========================================================
# ⏰ AUTOMATED 7-DAY REMINDER ENGINE
# =========================================================
def run_weekly_reminders():
    """Hunts for cases exactly 7 days away and emails the staff."""
    with app.app_context():
        conn = DB_POOL.getconn()
        try:
            with conn.cursor() as cur:
                target_date = (date.today() + timedelta(days=7)).strftime('%Y-%m-%d')
                
                # Fetch matters happening in exactly 7 days
                cur.execute("""
                    SELECT case_number, case_parties, coming_up_for 
                    FROM cases 
                    WHERE next_court_date = %s
                """, (target_date,))
                upcoming = cur.fetchall()
                
                if not upcoming:
                    logging.info(f"[{datetime.now()}] No matters scheduled for {target_date}. No reminders sent.")
                    return
                
                # Fetch staff emails
                cur.execute("SELECT email FROM users WHERE role IN ('admin', 'advocate', 'secretary') AND email IS NOT NULL")
                staff_emails = [row['email'] for row in cur.fetchall()]
                
                if not staff_emails: return

                # Compile HTML Alert
                html = f"""
                <div style="font-family: Arial, sans-serif; max-width: 600px;">
                    <h2 style="color: #c9a961;">Wambui Shadrack Associates - 7-Day Alert</h2>
                    <p>Good morning. The following matters are scheduled for next week (<b>{target_date}</b>):</p>
                    <hr>
                """
                for c in upcoming:
                    parties = c.get('case_parties') or "Parties Not Listed"
                    html += f"""
                    <div style="margin-bottom: 15px; padding: 10px; background-color: #f8fafc; border-left: 4px solid #c9a961;">
                        <p><strong>File:</strong> {c['case_number']}</p>
                        <p><strong>Parties:</strong> {parties}</p>
                        <p><strong>Action Required:</strong> {c.get('coming_up_for', 'N/A')}</p>
                    </div>
                    """
                html += "</div>"

                # Dispatch Email to all staff
                for email in staff_emails:
                    send_generic_email(email, f"🚨 Upcoming Hearings: {target_date}", html)
                
                logging.info(f"[{datetime.now()}] 7-Day Reminders sent for {target_date} to {len(staff_emails)} staff members.")
        finally:
            DB_POOL.putconn(conn)

# Initialize Scheduler
scheduler = BackgroundScheduler()
scheduler.add_job(func=run_weekly_reminders, trigger="cron", hour=6, minute=0) # Runs daily at 6:00 AM
scheduler.start()
atexit.register(lambda: scheduler.shutdown())

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
    return r.json().get('access_token')

def initiate_stk_push(phone, amount, account_ref, description="Legal Fees"):
    try:
        token = get_mpesa_token()
        ts = datetime.now().strftime('%Y%m%d%H%M%S')
        password = base64.b64encode(f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{ts}".encode()).decode('utf-8')
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
        return r.status_code, r.json() if r.status_code in (200, 201) else {"error": r.text}
    except Exception as e:
        return 500, {"error": str(e)}

# =========================================================
# 🔐 AUTH & OTP
# =========================================================
@app.route('/api/auth/login-router', methods=['POST'])
def login_router():
    if SYSTEM_STATE['LOCKDOWN_MODE']:
        return jsonify({"success": False, "message": "PORTAL UNDER SECURITY LOCKDOWN. ACCESS DENIED."}), 503
    payload = request.get_json(silent=True) or {}
    credential = (payload.get('credential') or '').strip()
    if not credential: return json_error("Login field cannot be blank.")
    
    conn = get_db()
    if '@' in credential:
        email = _normalize_email(credential)
        with conn.cursor() as cur:
            cur.execute("SELECT full_name, role FROM users WHERE LOWER(email)=%s", (email,))
            account = cur.fetchone()
        
        if not account: return json_error("Access denied: Not a registered staff member.", 403)
        
        otp = str(random.randint(100000, 999999))
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO otp_vault_email (email, code, expires_at)
                VALUES (%s, %s, NOW() + INTERVAL '10 minutes')
                ON CONFLICT (email) DO UPDATE SET code=EXCLUDED.code, expires_at=EXCLUDED.expires_at;
            """, (email, otp))
        conn.commit()
        
        ok = send_otp_email(email, otp, account['full_name'])
        logging.info(f"🔑 [SECURITY TESTING] GENERATED OTP FOR {email} IS: {otp} | Delivered: {ok}")
        
        return jsonify({
            "success": True, "mode": "otp_required", "role_preview": account['role'], 
            "message": f"Check your email for the verification code."
        })
    else:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT case_number, client_name, case_parties, ai_access_granted, next_court_date, coming_up_for, total_balance, paid_balance
                FROM cases WHERE LOWER(case_number) = LOWER(%s) LIMIT 1
            """, (credential,))
            case = cur.fetchone()
            
        if not case: return json_error("No case found matching that reference.", 404)
        total, paid = float(case['total_balance'] or 0), float(case['paid_balance'] or 0)
        
        return jsonify({
            "success": True, "mode": "client_dashboard",
            "data": {
                "case_number": case['case_number'], 
                "client_name" : case['client_name'],
                "case_parties": case.get('case_parties') or "Parties not established", # Enforces display in client portal
                "next_court_date": case['next_court_date'], 
                "coming_up_for": case['coming_up_for'],
                "financials": {"total": total, "paid": paid, "balance": total - paid},
                "ai_unlocked": case['ai_access_granted']
            }
        })

@app.route('/api/auth/verify-otp', methods=['POST'])
def verify_otp():
    data = request.get_json(silent=True) or {}
    email = _normalize_email(data.get('email') or data.get('phone') or '')
    code = (data.get('code') or '').strip()
    if not email or not code: return json_error("Identity parameters missing.")
        
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT code FROM otp_vault_email WHERE email=%s AND expires_at > NOW();", (email,))
        rec = cur.fetchone()
        
    if not rec or rec['code'] != code: return json_error("Invalid or Expired Security Token.", 401)
        
    with conn.cursor() as cur:
        cur.execute("DELETE FROM otp_vault_email WHERE email=%s;", (email,))
        cur.execute("SELECT full_name, role FROM users WHERE LOWER(email)=%s;", (email,))
        prof = cur.fetchone()
    conn.commit()
    
    return jsonify({"success": True, "email": email, "role": prof['role'], "user_name": prof['full_name']})

# =========================================================
# 📂 STAFF OPERATIONS 
# =========================================================
@app.route('/api/staff/cases', methods=['GET'])
@app.route('/api/staff/search', methods=['GET', 'POST'])
@require_staff()
def list_or_search_cases():
    q = request.args.get('q') or (request.get_json(silent=True) or {}).get('query') or ''
    q = q.strip()
    conn = get_db()
    
    with conn.cursor() as cur:
        if q:
            like = f"%{q}%"
            cur.execute("""
                SELECT * FROM cases
                WHERE case_number     ILIKE %s
                   OR client_name     ILIKE %s
                   OR case_parties    ILIKE %s
                   OR coming_up_for   ILIKE %s
                   OR next_court_date ILIKE %s
                ORDER BY updated_at DESC
            """, (like, like, like, like, like))
        else:
            # When the search bar is empty, it returns the entire firm roster
            cur.execute("SELECT * FROM cases ORDER BY updated_at DESC")
        
        raw_rows = cur.fetchall()
        
    clean_rows = []
    for r in raw_rows:
        c = dict(r)
        c['total_balance'] = float(c.get('total_balance') or 0.0)
        c['paid_balance'] = float(c.get('paid_balance') or 0.0)
        c['case_parties'] = c.get('case_parties') or "N/A" # Enforce visibility
        if g.current_user['role'] != 'admin':
            c['total_balance'] = "RESTRICTED"
            c['paid_balance'] = "RESTRICTED"
        clean_rows.append(c)
    return jsonify({"success": True, "cases": clean_rows, "results": clean_rows})

@app.route('/api/staff/upcoming', methods=['GET'])
@require_staff()
def upcoming_cases():
    """Powers the staff dashboard feed showing all matters coming up in the next 7 days."""
    conn = get_db()
    today_str = date.today().strftime('%Y-%m-%d')
    next_week_str = (date.today() + timedelta(days=7)).strftime('%Y-%m-%d')
    
    with conn.cursor() as cur:
        cur.execute("""
            SELECT case_id, case_number, client_name, case_parties, coming_up_for, next_court_date
            FROM cases
            WHERE next_court_date >= %s AND next_court_date <= %s
            ORDER BY next_court_date ASC
        """, (today_str, next_week_str))
        cases = cur.fetchall()
        
    # Ensure parties formatting
    for c in cases:
        c['case_parties'] = c.get('case_parties') or "Parties Not Listed"
        
    return jsonify({"success": True, "cases": cases})

@app.route('/api/staff/add-matter', methods=['POST'])
@require_staff()
def add_matter():
    data = request.get_json(silent=True) or {}
    case_number = (data.get('case_number') or '').strip()
    if not case_number: return json_error("Case Reference Number is required.")
        
    is_admin = g.current_user['role'] == 'admin'
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO cases (case_number, client_name, case_parties, total_balance, paid_balance)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                case_number,
                data.get('client_name') or data.get('case_parties'),
                data.get('case_parties') or data.get('client_name'),
                float(data.get('total_balance') or 0) if is_admin else 0,
                float(data.get('paid_balance') or 0) if is_admin else 0,
            ))
        conn.commit()
        return jsonify({"success": True, "message": "Registry Ledger Updated Successfully."})
    except psycopg2.IntegrityError:
        conn.rollback()
        return json_error("Case number already exists in registry.", 409)

@app.route('/api/staff/update-matter', methods=['POST'])
@require_staff()
def update_matter():
    data = request.get_json(silent=True) or {}
    case_number = data.get('case_number')
    if not case_number: return json_error("Matter Identification Ref Required.")
        
    sets, vals = [], []
    fields = ['next_court_date', 'coming_up_for', 'case_parties']
    if g.current_user['role'] == 'admin': fields += ['total_balance', 'paid_balance']
    for f in fields:
        if f in data and data[f] is not None:
            sets.append(f"{f} = %s")
            vals.append(data[f])
    if not sets: return json_error("No modifications detected.")
        
    sets.append("updated_at = CURRENT_TIMESTAMP")
    vals.append(case_number)
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute(f"UPDATE cases SET {', '.join(sets)} WHERE case_number = %s", vals)
    conn.commit()
    return jsonify({"success": True, "message": "Case File Modified Successfully."})

# =========================================================
# 🗂️ SECURE DOCUMENT HANDLING
# =========================================================
@app.route('/api/documents/list', methods=['GET'])
def list_docs():
    case_number = (request.args.get('case_number') or '').strip()
    if not case_number: return jsonify({"success": True, "files": []})
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM case_documents WHERE case_number=%s ORDER BY upload_date DESC", (case_number,))
        files = cur.fetchall()
    return jsonify({"success": True, "files": files})

@app.route('/api/documents/client-upload', methods=['POST'])
@app.route('/api/documents/staff-upload', methods=['POST'])
def unified_upload():
    file = request.files.get('document')
    case_number = (request.form.get('case_number') or '').strip()
    if not file or not case_number: return json_error("Missing file or case routing number.")
        
    safe_name = secure_filename(file.filename or 'upload.pdf')
    stamp = datetime.now().strftime('%Y%m%d%H%M%S')
    stored_name = f"{case_number.replace('/', '_')}__{stamp}__{safe_name}"
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], stored_name))
    
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO case_documents (case_number, filename, original_name, uploaded_by_role) VALUES (%s, %s, %s, 'system')", (case_number, stored_name, safe_name))
    conn.commit()
    return jsonify({"success": True, "message": "Document successfully ingested to secure storage."})

@app.route('/api/documents/download/<filename>', methods=['GET'])
@require_staff()
def download_doc(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

# =========================================================
# 🛡️ SYSTEM METRICS & AI LOGS
# =========================================================
@app.route('/api/system/metrics', methods=['GET'])
@require_staff()
def system_metrics():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) as count FROM cases;"); cases_count = cur.fetchone()['count']
        cur.execute("SELECT COUNT(*) as count FROM ai_client_logs;"); ai_count = cur.fetchone()['count']
    return jsonify({
        "success": True, "cloud_status": "SECURE" if not SYSTEM_STATE['LOCKDOWN_MODE'] else "ISOLATED",
        "live_metrics": {"total_requests": cases_count + ai_count, "failed_logins": 0, "ai_queries_processed": ai_count}
    })

@app.route('/api/staff/ai-monitoring', methods=['GET'])
@require_staff()
def ai_monitoring():
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT case_number, question FROM ai_client_logs ORDER BY logged_at DESC LIMIT 50")
        logs = cur.fetchall()
    return jsonify({"success": True, "logs": [{"case_number": r['case_number'], "client_question": r['question']} for r in logs]})

@app.route('/api/admin/system-override', methods=['POST'])
def admin_system_override():
    email = _normalize_email(request.headers.get('X-User-Email', ''))
    if not email: return json_error("Authentication required.", 401)
    
    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("SELECT role FROM users WHERE LOWER(email)=%s", (email,))
        row = cur.fetchone()
    
    if not row or row['role'] != 'admin':
        return json_error("Forbidden: admin only.", 403)

    action = (request.get_json(silent=True) or {}).get('action', '').upper()
    if action == 'LOCK':
        SYSTEM_STATE['LOCKDOWN_MODE'] = True
    elif action == 'UNLOCK':
        SYSTEM_STATE['LOCKDOWN_MODE'] = False
    else:
        return json_error("Unknown action. Use LOCK or UNLOCK.", 400)
    return jsonify({
        "success": True,
        "lockdown": SYSTEM_STATE['LOCKDOWN_MODE'],
        "message": f"System state shifted to {action}."
    })

# =========================================================
# 💸 BILLING 
# =========================================================
@app.route('/api/billing/ai-unlock', methods=['POST'])
def billing_ai_unlock():
    data = request.get_json(silent=True) or {}
    case_number = data.get('case_number')
    method = data.get('method')
    amount = data.get('amount', 500)
    phone = data.get('phone')

    if method == 'mpesa' and phone:
        status, resp = initiate_stk_push(phone, amount, case_number, "AI Access Unlock")
        if status not in (200, 201):
            return json_error("M-Pesa transaction failed to initiate.", 400, details=resp)

    conn = get_db()
    with conn.cursor() as cur:
        cur.execute("UPDATE cases SET ai_access_granted=TRUE WHERE case_number=%s", (case_number,))
    conn.commit()
    return jsonify({"success": True, "message": "Transaction verified. AI framework unlocked."})

# =========================================================
# 🧠 AI ENGINE
# =========================================================
LOVABLE_API_KEY = os.environ.get('LOVABLE_API_KEY', '')
AI_MODEL = os.environ.get('AI_MODEL', 'google/gemini-2.5-flash')

@app.route('/api/ai/consult', methods=['POST'])
def ai_consult():
    data = request.get_json(silent=True) or {}
    q = data.get('question', '')
    case_no = data.get('case_number', 'Staff Query')
    actor = data.get('actor', 'client')
    tone = "plain English" if actor == 'client' else "advocate-grade with full citations"
    
    if not LOVABLE_API_KEY:
        answer = "[Offline AI] ISSUE: Configure LOVABLE_API_KEY for full analysis."
    else:
        try:
            r = requests.post(
                "https://ai.gateway.lovable.dev/v1/chat/completions",
                headers={"Authorization": f"Bearer {LOVABLE_API_KEY}", "Content-Type": "application/json"},
                json={
                    "model": AI_MODEL,
                    "messages": [
                        {"role": "system", "content": f"You are a Kenyan legal assistant. Answer in {tone}."},
                        {"role": "user", "content": q},
                    ],
                    "temperature": 0.3,
                },
                timeout=60,
            )
            r.raise_for_status()
            answer = r.json()['choices'][0]['message']['content']
        except Exception as e:
            return json_error(f"AI Gateway failure: {e}", 502)

    conn = get_db()
    with conn.cursor() as cur:
        # Fixed: Included missing actor column in the insert statement
        cur.execute("""
            INSERT INTO ai_client_logs (case_number, actor, question, ai_response) 
            VALUES (%s, %s, %s, %s)
        """, (case_no, actor, q, answer))
    conn.commit()
    return jsonify({"success": True, "engine": AI_MODEL, "answer": answer})

# =========================================================
# 🚀 SERVER START
# =========================================================
with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
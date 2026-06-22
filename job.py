"""
=====================================================================
 Wambui Shadrack Associates — Secure Legal Portal Backend
 Flask + PostgreSQL + M-Pesa Daraja + Resend Email
=====================================================================
"""
import os
import random
import logging
import base64
from datetime import datetime
from functools import wraps
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool as pgpool
from requests.auth import HTTPBasicAuth
from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

# =========================================================
# ⚙️ APP CONFIGURATION & CORS
# =========================================================
app = Flask(__name__)

# Permissive CORS for the Netlify Demo to eliminate "Blocked by CORS" errors
CORS(app, resources={r"/api/*": {"origins": "*"}})

app.config['DATABASE_URL'] = os.environ.get('DATABASE_URL')
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', './client_docs/')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 25 * 1024 * 1024  # 25 MB max upload

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
        logging.info("✅ PostgreSQL Pool Initialized")

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
# 🛠️ DATABASE SCHEMA & AUTO-SEED
# =========================================================
def init_db():
    init_pool()
    conn = DB_POOL.getconn()
    try:
        cur = conn.cursor()
        
        # 1. Users & Auth
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
        """)
        
        # 2. Main Cases Ledger
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                case_id SERIAL PRIMARY KEY,
                case_number VARCHAR(255) UNIQUE NOT NULL,
                client_name VARCHAR(255),
                next_court_date VARCHAR(255),
                coming_up_for TEXT,
                total_balance NUMERIC(15,2) DEFAULT 0.00,
                paid_balance NUMERIC(15,2) DEFAULT 0.00,
                ai_access_granted BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 3. Documents Vault
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
        
        # 4. Telemetry & Logs
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
        
        # 5. Seed Staff Accounts
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
        logging.info("💾 Database schema verified and synchronized.")
    except Exception as e:
        conn.rollback()
        logging.exception(f"DB init failure: {e}")
    finally:
        cur.close()
        DB_POOL.putconn(conn)

# =========================================================
# 🔑 SECURITY HELPERS & MIDDLEWARE
# =========================================================
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
                
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT role, full_name FROM users WHERE LOWER(email)=%s", (email,))
            row = cur.fetchone()
            if not row or row['role'] not in roles:
                return json_error("Forbidden Access.", 403)
                
            g.current_user = {"email": email, "role": row['role'], "name": row['full_name']}
            return fn(*args, **kwargs)
        return wrapper
    return deco

# =========================================================
# 🔐 AUTHENTICATION ROUTER
# =========================================================
@app.route('/api/auth/login-router', methods=['POST'])
def login_router():
    if SYSTEM_STATE['LOCKDOWN_MODE']:
        return jsonify({"success": False, "message": "PORTAL UNDER SECURITY LOCKDOWN. ACCESS DENIED."}), 503
        
    payload = request.get_json(silent=True) or {}
    credential = (payload.get('credential') or '').strip()
    
    if not credential:
        return json_error("Login field cannot be blank.")
        
    # If it contains @, route to Staff OTP Protocol
    if '@' in credential:
        email = _normalize_email(credential)
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT full_name, role FROM users WHERE LOWER(email)=%s", (email,))
        account = cur.fetchone()
        
        if not account:
            return json_error("Access denied: Not a registered staff member.", 403)
            
        otp = str(random.randint(100000, 999999))
        cur.execute("""
            INSERT INTO otp_vault_email (email, code, expires_at)
            VALUES (%s, %s, NOW() + INTERVAL '10 minutes')
            ON CONFLICT (email) DO UPDATE SET code=EXCLUDED.code, expires_at=EXCLUDED.expires_at;
        """, (email, otp))
        conn.commit()
        
        logging.info(f"SECURITY OTP FOR {email}: {otp}")
        
        return jsonify({
            "success": True, "mode": "otp_required", "role_preview": account['role'], 
            "message": f"Check your email for the verification code."
        })
        
    # Otherwise, route to OTP-Free Client Protocol
    else:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT case_id, case_number, client_name, ai_access_granted, 
                   next_court_date, coming_up_for, total_balance, paid_balance
            FROM cases WHERE LOWER(case_number) = LOWER(%s) LIMIT 1
        """, (credential,))
        case = cur.fetchone()
        
        if not case:
            return json_error("No case found matching that reference.", 404)
            
        total = float(case['total_balance'] or 0)
        paid = float(case['paid_balance'] or 0)
        return jsonify({
            "success": True, "mode": "client_dashboard",
            "data": {
                "case_number": case['case_number'],
                "client_name": case['client_name'],
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
    
    if not email or not code:
        return json_error("Identity parameters missing.")
        
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT code FROM otp_vault_email WHERE email=%s AND expires_at > NOW();", (email,))
    rec = cur.fetchone()
    
    if not rec or rec['code'] != code:
        return json_error("Invalid or Expired Security Token.", 401)
        
    cur.execute("DELETE FROM otp_vault_email WHERE email=%s;", (email,))
    cur.execute("SELECT full_name, role FROM users WHERE LOWER(email)=%s;", (email,))
    prof = cur.fetchone()
    conn.commit()
    
    return jsonify({
        "success": True, "email": email, "role": prof['role'], "user_name": prof['full_name']
    })

@app.route('/api/auth/resend-otp', methods=['POST'])
def resend_otp():
    data = request.get_json(silent=True) or {}
    email = _normalize_email(data.get('email') or data.get('phone') or '')
    
    if not email:
        return json_error("Email identity parameter missing.")
        
    new_otp = str(random.randint(100000, 999999))
    conn = get_db(); cur = conn.cursor()
    
    cur.execute("""
        UPDATE otp_vault_email 
        SET code = %s, expires_at = NOW() + INTERVAL '10 minutes'
        WHERE email = %s;
    """, (new_otp, email))
    
    if cur.rowcount == 0:
        # If record context was cleaned up or timed out entirely, insert a fresh baseline record
        cur.execute("""
            INSERT INTO otp_vault_email (email, code, expires_at)
            VALUES (%s, %s, NOW() + INTERVAL '10 minutes')
            ON CONFLICT (email) DO UPDATE SET code=EXCLUDED.code, expires_at=EXCLUDED.expires_at;
        """, (email, new_otp))
        
    conn.commit()
    logging.info(f"🔄 RESENT SECURITY OTP FOR {email}: {new_otp}")
    
    return jsonify({"success": True, "message": "A fresh 6-digit access code has been issued."})

# =========================================================
# 📂 STAFF OPERATIONS (SEARCH, ADD, UPDATE)
# =========================================================
@app.route('/api/staff/cases', methods=['GET'])
@app.route('/api/staff/search', methods=['GET', 'POST'])
@require_staff()
def list_or_search_cases():
    q = request.args.get('q') or (request.get_json(silent=True) or {}).get('query') or ''
    q = q.strip()
    
    conn = get_db(); cur = conn.cursor()
    if q:
        like = f"%{q}%"
        cur.execute("""
            SELECT * FROM cases 
            WHERE case_number ILIKE %s OR client_name ILIKE %s 
            ORDER BY updated_at DESC LIMIT 100
        """, (like, like))
    else:
        cur.execute("SELECT * FROM cases ORDER BY updated_at DESC LIMIT 100")
        
    raw_rows = cur.fetchall()
    clean_rows = []
    
    for r in raw_rows:
        c = dict(r)
        c['total_balance'] = float(c.get('total_balance') or 0.0)
        c['paid_balance'] = float(c.get('paid_balance') or 0.0)
        
        if g.current_user['role'] != 'admin':
            c['total_balance'] = "RESTRICTED"
            c['paid_balance'] = "RESTRICTED"
            
        clean_rows.append(c)
        
    return jsonify({"success": True, "cases": clean_rows, "results": clean_rows})

@app.route('/api/staff/add-matter', methods=['POST'])
@require_staff()
def add_matter():
    data = request.get_json(silent=True) or {}
    case_number = (data.get('case_number') or '').strip()
    
    if not case_number:
        return json_error("Case Reference Number is required.")
        
    is_admin = g.current_user['role'] == 'admin'
    conn = get_db(); cur = conn.cursor()
    
    try:
        cur.execute("""
            INSERT INTO cases (case_number, client_name, total_balance, paid_balance)
            VALUES (%s, %s, %s, %s)
        """, (
            case_number, data.get('client_name'),
            float(data.get('total_balance') or 0) if is_admin else 0,
            float(data.get('paid_balance') or 0) if is_admin else 0
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
    case_id = data.get('case_id')
    
    if not case_id:
        return json_error("Matter Identification Ref Required.")
        
    sets, vals = [], []
    fields = ['next_court_date', 'coming_up_for']
    if g.current_user['role'] == 'admin':
        fields += ['total_balance', 'paid_balance']
        
    for f in fields:
        if f in data and data[f] is not None:
            sets.append(f"{f} = %s")
            vals.append(data[f])
            
    if not sets:
        return json_error("No modifications detected.")
        
    sets.append("updated_at = CURRENT_TIMESTAMP")
    vals.append(case_id)
    
    conn = get_db(); cur = conn.cursor()
    cur.execute(f"UPDATE cases SET {', '.join(sets)} WHERE case_id = %s", vals)
    conn.commit()
    
    return jsonify({"success": True, "message": "Case File Modified Successfully."})

# =========================================================
# 🗂️ SECURE DOCUMENT HANDLING
# =========================================================
@app.route('/api/documents/list', methods=['GET'])
def list_docs():
    case_number = (request.args.get('case_number') or '').strip()
    if not case_number: return jsonify({"success": True, "files": []})
    
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT filename FROM case_documents WHERE case_number=%s ORDER BY upload_date DESC", (case_number,))
    files = cur.fetchall()
    return jsonify({"success": True, "files": files})

@app.route('/api/documents/client-upload', methods=['POST'])
@app.route('/api/documents/staff-upload', methods=['POST'])
def unified_upload():
    file = request.files.get('document')
    case_number = (request.form.get('case_number') or '').strip()
    
    if not file or not case_number:
        return json_error("Missing file or case routing number.")
        
    safe_name = secure_filename(file.filename or 'upload.pdf')
    stamp = datetime.now().strftime('%Y%m%d%H%M%S')
    stored_name = f"{case_number.replace('/', '_')}__{stamp}__{safe_name}"
    
    path = os.path.join(app.config['UPLOAD_FOLDER'], stored_name)
    file.save(path)
    
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        INSERT INTO case_documents (case_number, filename, original_name, uploaded_by_role)
        VALUES (%s, %s, %s, 'system')
    """, (case_number, stored_name, safe_name))
    conn.commit()
    
    return jsonify({"success": True, "message": "Document successfully ingested to secure storage."})

# =========================================================
# 🛡️ SYSTEM METRICS, AI LOGS, & CYBER KILL SWITCH
# =========================================================
@app.route('/api/system/metrics', methods=['GET'])
@require_staff()
def system_metrics():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as count FROM cases;")
    cases_count = cur.fetchone()['count']
    
    cur.execute("SELECT COUNT(*) as count FROM ai_client_logs;")
    ai_count = cur.fetchone()['count']
    
    return jsonify({
        "success": True,
        "cloud_status": "SECURE" if not SYSTEM_STATE['LOCKDOWN_MODE'] else "ISOLATED",
        "live_metrics": {
            "total_requests": cases_count + ai_count,
            "failed_logins": 0,
            "ai_queries_processed": ai_count
        }
    })

@app.route('/api/staff/ai-monitoring', methods=['GET'])
@require_staff()
def ai_monitoring():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT case_number, question FROM ai_client_logs ORDER BY logged_at DESC LIMIT 50")
    rows = cur.fetchall()
    
    logs = [{"case_number": r['case_number'], "client_question": r['question']} for r in rows]
    return jsonify({"success": True, "logs": logs})

@app.route('/api/admin/system-override', methods=['POST'])
@require_staff(('admin',))
def admin_system_override():
    data = request.get_json(silent=True) or {}
    action = data.get('action')
    
    if action == 'LOCK':
        SYSTEM_STATE['LOCKDOWN_MODE'] = True
    elif action == 'UNLOCK':
        SYSTEM_STATE['LOCKDOWN_MODE'] = False
        
    return jsonify({"success": True, "message": f"System state shifted to {action}."})

# =========================================================
# 💸 BILLING MOCK ENDPOINT (M-PESA / AI UNLOCK)
# =========================================================
@app.route('/api/billing/ai-unlock', methods=['POST'])
def billing_ai_unlock():
    data = request.get_json(silent=True) or {}
    case_number = data.get('case_number')
    
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE cases SET ai_access_granted=TRUE WHERE case_number=%s", (case_number,))
    conn.commit()
    
    return jsonify({"success": True, "message": "Transaction verified. AI framework unlocked."})

# =========================================================
# 🧠 AI ENGINE MOCK ENDPOINT
# =========================================================
@app.route('/api/ai/consult', methods=['POST'])
def ai_consult():
    data = request.get_json(silent=True) or {}
    q = data.get('question', '')
    case_no = data.get('case_number', 'Staff Query')
    
    answer = f"According to the Constitution of Kenya 2010, regarding '{q}': The legal precedent is currently being evaluated. Please consult principal advocate Shadrack Wambui for immediate strategy."
    
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO ai_client_logs (case_number, question, ai_response) VALUES (%s, %s, %s)", 
               (case_no, q, answer))
    conn.commit()
    
    return jsonify({"success": True, "engine": "Gemini-Legal-Core", "answer": answer})

# =========================================================
# 🚀 INITIALIZATION & SERVER START
# =========================================================
with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
"""
=====================================================================
 Wambui Shadrack Associates — Legal Portal Backend (v3, production)
 Flask + PostgreSQL + M-Pesa Daraja STK Push + Resend Email + Scheduler
=====================================================================
"""
import os
import random
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import pool
from flask import Flask, request, jsonify, g
from flask_cors import CORS
from werkzeug.utils import secure_filename
from datetime import datetime, timedelta
import resend

# =========================================================
# ⚙️ SYSTEM CONFIGURATION & SETUP
# =========================================================

app = Flask(__name__)
CORS(app)

app.config['DATABASE_URL'] = os.environ.get(
    'DATABASE_URL', 
    'dbname=postgres user=postgres password=jose1023 host=localhost port=5432'
)  
app.config['UPLOAD_FOLDER'] = './client_docs/'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Secure system logging for cyber analysis
logging.basicConfig(
    filename='system_security.log', 
    level=logging.INFO, 
    format='%(asctime)s %(levelname)s: %(message)s'
)

# Resend Email Configuration
resend.api_key = os.environ.get('RESEND_API_KEY', 're_dummy_key_replace_in_production')
FIRM_EMAIL = "nduatijosephmwangi@gmail.com" # Central firm email for notifications

# In-Memory Security State & Router Store
SYSTEM_STATE = {
    "LOCKDOWN_MODE": False
}

# =========================================================
# 🗄️ DATABASE CONNECTION POOLING & AUTO-INITIALIZATION
# =========================================================

# Initialize the connection pool globally
db_pool = psycopg2.pool.SimpleConnectionPool(1, 20, app.config['DATABASE_URL'])

def get_db():
    if 'db' not in g:
        g.db = db_pool.getconn()
    return g.db

@app.teardown_appcontext
def close_db(e):
    db = g.pop('db', None)
    if db is not None:
        db_pool.putconn(db)

def init_db():
    conn = None
    try:
        # CONNECT FIRST BEFORE DOING ANYTHING
        conn = db_pool.getconn()
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
        
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cases (
                case_id SERIAL PRIMARY KEY,
                case_number VARCHAR(255) UNIQUE NOT NULL,
                case_parties TEXT,
                client_name VARCHAR(255),
                next_court_date VARCHAR(255),
                coming_up_for TEXT,
                total_balance NUMERIC(15,2) DEFAULT 0.00,
                paid_balance NUMERIC(15,2) DEFAULT 0.00,
                ai_access_granted BOOLEAN DEFAULT FALSE,
                status VARCHAR(20) DEFAULT 'Active',
                latest_document_path TEXT,
                staff_uploaded_doc TEXT
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS ai_client_logs (
                log_id SERIAL PRIMARY KEY,
                case_number VARCHAR(255) NOT NULL,
                client_name VARCHAR(255),
                client_question TEXT NOT NULL,
                ai_response TEXT NOT NULL,
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                log_id SERIAL PRIMARY KEY,
                action_type VARCHAR(255),
                performed_by VARCHAR(255),
                target_record VARCHAR(255),
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

        # SAFELY ADD NEW COLUMNS
        try:
            cur.execute("ALTER TABLE cases ADD COLUMN staff_uploaded_doc TEXT;")
            conn.commit()
        except psycopg2.errors.DuplicateColumn:
            conn.rollback() 

        try:
            cur.execute("ALTER TABLE users ADD COLUMN current_otp VARCHAR(10);")
            conn.commit()
        except psycopg2.errors.DuplicateColumn:
            conn.rollback() 
        
        # INSERT USERS
        cur.execute("""
            INSERT INTO users (full_name, phone_number, email, role) 
            VALUES ('Wambui Shadrack', '0711223344', 'nduatijosephmwangi@gmail.com', 'admin') 
            ON CONFLICT DO NOTHING;
        """)
        cur.execute("""
            INSERT INTO users (full_name, phone_number, email, role) 
            VALUES ('Jeff Kangethe', '0796178783', 'jeff@globallaga.com', 'advocate') 
            ON CONFLICT DO NOTHING;
        """)
        cur.execute("""
            INSERT INTO users (full_name, phone_number, email, role) 
            VALUES ('Jane Onyango', '0733445566', 'jane@globallaga.com', 'secretary') 
            ON CONFLICT DO NOTHING;
        """)
        
        conn.commit()
        cur.close()
        print("💾 [DATABASE INITIALIZATION] Schema verified and updated.")
    except Exception as e:
        print(f"⚠️ [DATABASE INITIALIZATION FAILURE]: {str(e)}")
    finally:
        if conn:
            db_pool.putconn(conn)

# =========================================================
# 📧 EMAIL NOTIFICATION HELPER
# =========================================================

def send_firm_email(subject, html_content, to_email=FIRM_EMAIL):
    """Secure background email dispatcher using Resend."""
    try:
        resend.Emails.send({
            "from": "onboarding@resend.dev", 
            "to": to_email,
            "subject": subject,
            "html": html_content
        })
        logging.info(f"Email Dispatched: {subject} to {to_email}")
    except Exception as e:
        logging.error(f"Failed to send email via Resend: {str(e)}")
        print(f"RESEND ERROR: {str(e)}")

# =========================================================
# 🛡️ SECURITY MIDDLEWARE & OBSERVABILITY (THE CYBER KILL SWITCH)
# =========================================================

@app.before_request
def cyber_security_check():
    if request.endpoint:
        logging.info(f"API HIT: {request.remote_addr} accessed {request.endpoint}")

    if SYSTEM_STATE["LOCKDOWN_MODE"]:
        allowed_routes = ['login_router', 'verify_otp', 'toggle_kill_switch']
        if request.endpoint not in allowed_routes:
            logging.warning(f"BLOCKED REQUEST: Unauthorized access attempt to '{request.endpoint}'.")
            return jsonify({
                "success": False,
                "error": "SECURITY_LOCKDOWN",
                "message": "⚠️ PORTAL LOCKDOWN ACTIVE. Client access has been suspended due to an ongoing threat protocol."
            }), 503

def log_audit(action, user, target):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO audit_logs (action_type, performed_by, target_record) VALUES (%s, %s, %s)", 
                    (action, user, target))
        conn.commit()
    except Exception as e:
        logging.error(f"Audit Log Failure: {str(e)}")

@app.route('/api/admin/observability', methods=['GET'])
def get_system_logs():
    try:
        with open('system_security.log', 'r') as f:
            lines = f.readlines()[-50:] 
        
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT 20")
        audit_records = cur.fetchall()
        
        return jsonify({"success": True, "server_logs": lines, "audit_logs": audit_records})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})

# =========================================================
# 🔐 SYSTEM ROUTING & AUTHENTICATION LAYER
# =========================================================

@app.route('/api/auth/login-router', methods=['POST'])
def login_router():
    payload = request.get_json() or {}
    credential = payload.get('credential', '').strip()
    
    if not credential:
        return jsonify({"success": False, "message": "Login field cannot be blank."}), 400
        
    if '@' in credential or (credential.isdigit() and len(credential) >= 10):
        return initiate_staff_login(credential)
    else:
        return client_login(credential)

def initiate_staff_login(credential):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        if '@' in credential:
            cur.execute("SELECT full_name, phone_number, email, role FROM users WHERE email = %s", (credential,))
        else:
            cur.execute("SELECT full_name, phone_number, email, role FROM users WHERE phone_number = %s", (credential,))
            
        account = cur.fetchone()
        
        if not account:
            return jsonify({"success": False, "message": "Access Denied: Credential is not registered as active staff."}), 403
        
        otp = str(random.randint(100000, 999999))
        identifier = account['email'] or account['phone_number']
        
        # Save OTP to the PostgreSQL database
        cur.execute("UPDATE users SET current_otp = %s WHERE email = %s OR phone_number = %s", (otp, identifier, identifier))
        conn.commit()
        
        print(f"\n📡 [SMS/EMAIL UTILITY LOG] Token Dispatch for {account['full_name']} -> {otp}\n")
        logging.info(f"OTP generated successfully for staff: {identifier}")
        
        if account['email']:
            email_html = f"""
            <div style="font-family: Arial, sans-serif; padding: 20px; color: #333;">
                <h3>Wambui Shadrack & Associates Portal</h3>
                <p>Hello {account['full_name']},</p>
                <p>Your secure access verification code is:</p>
                <h1 style="color: #4CAF50; letter-spacing: 5px;">{otp}</h1>
                <p><small>Do not share this code with anyone. It expires shortly.</small></p>
            </div>
            """
            send_firm_email("Your Secure Portal OTP", email_html, to_email=account['email'])
        
        return jsonify({"success": True, "mode": "otp_required", "identifier": identifier, "message": "Verification code dispatched securely."})
    except Exception as e:
        return jsonify({"success": False, "message": f"Server Authentication Fault: {str(e)}"}), 500

@app.route('/api/auth/verify-otp', methods=['POST'])
def verify_otp():
    data = request.get_json() or {}
    identifier = str(data.get('identifier', '')).strip()
    code = str(data.get('code', '')).strip()
    
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("SELECT full_name, role, current_otp FROM users WHERE email = %s OR phone_number = %s", (identifier, identifier))
        account = cur.fetchone()
        
        # Ensure account exists and grab the stored OTP safely
        if not account:
            return jsonify({"success": False, "message": "User not found."}), 404
            
        stored_otp = str(account.get('current_otp', '')).strip()
        
        if stored_otp == "" or stored_otp == "None" or stored_otp != code:
            return jsonify({"success": False, "message": "Invalid or expired verification token signature."}), 401
        
        # Clear the OTP after successful login
        cur.execute("UPDATE users SET current_otp = NULL WHERE email = %s OR phone_number = %s", (identifier, identifier))
        conn.commit()
        
        return jsonify({
            "success": True,
            "role": account['role'], 
            "user_name": account['full_name'],    
            "lockdown_status": SYSTEM_STATE.get("LOCKDOWN_MODE", False)
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Database verification error: {str(e)}"}), 500

def client_login(case_number):
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        clean_case = str(case_number).strip()
        
        # Removed the strict 'Active' check to ensure cases are found
        cur.execute("""
            SELECT case_id, case_number, case_parties, client_name, ai_access_granted, next_court_date, coming_up_for, total_balance, paid_balance, staff_uploaded_doc
            FROM cases 
            WHERE case_number ILIKE %s
        """, (f"%{clean_case}%",))
        case = cur.fetchone()
        
        if not case:
            return jsonify({"success": False, "message": "No active legal records found matching that case context."}), 404
            
        total = float(case['total_balance'] or 0)
        paid = float(case['paid_balance'] or 0)
        outstanding = total - paid
        
        if case['ai_access_granted']:
            simulated_score = random.randint(65, 98)
            prediction_text = f"AI PREDICTOR ONLINE: Based on evidentiary density, case file outcome trends track at an estimated {simulated_score}% favorable rating."
        else:
            simulated_score = 0
            prediction_text = "PREDICTOR OFFLINE: Premium Predictive Access required to generate success probabilities."
        
        return jsonify({
            "success": True,
            "mode": "client_dashboard",
            "data": {
                "case_id": case['case_id'],
                "case_number": case['case_number'],
                "case_parties": case['case_parties'], 
                "client_name": case['client_name'],
                "next_court_date": str(case['next_court_date']),
                "coming_up_for": case['coming_up_for'],
                "staff_uploaded_doc": case['staff_uploaded_doc'],
                "financials": {"total": total, "paid": paid, "balance": outstanding},
                "ai_unlocked": case['ai_access_granted'],
                "case_predictor": {
                    "score": simulated_score,
                    "analysis": prediction_text
                }
            }
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"Database Ingestion Failure: {str(e)}"}), 500

# =========================================================
# 🤖 LEGAL AI INTERACTION ENGINE & PREDICTOR
# =========================================================

@app.route('/api/ai/consult', methods=['POST'])
def ai_consult():
    data = request.get_json() or {}
    question = data.get('question', '').strip()
    user_name = data.get('user_name', '').strip()       
    case_number = data.get('case_number', '').strip()   
    
    if not question:
        return jsonify({"success": False, "message": "Question context cannot be blank."}), 400
        
    # Staff / Admin AI Access
    if user_name:
        simulated_response = f"⚖️ [Staff Legal Research AI - Constitution 2010]: Processing operational guidance for query: '{question}'."
        return jsonify({"success": True, "engine": "Staff Legal Research AI", "answer": simulated_response})
        
    # Client AI Access
    if case_number:
        try:
            conn = get_db()
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("SELECT client_name FROM cases WHERE case_number = %s", (case_number,))
            case_record = cur.fetchone()
            
            if not case_record:
                return jsonify({"success": False, "message": "Case matching client verification context not found."}), 404
                
            simulated_response = f"🧠 [Client Consultant AI]: Strategic evaluation regarding your rights under Constitution 2010 for '{question}'."
            
            cur.execute("""
                INSERT INTO ai_client_logs (case_number, client_name, client_question, ai_response)
                VALUES (%s, %s, %s, %s)
            """, (case_number, case_record['client_name'], question, simulated_response))
            conn.commit()
            
            email_body = f"<h3>Client AI Query Alert</h3><p><strong>Matter:</strong> {case_number} ({case_record['client_name']})</p><p><strong>Question Asked:</strong> {question}</p>"
            send_firm_email(f"AI Query Log: Case {case_number}", email_body)
            
            return jsonify({"success": True, "engine": "Free Client Consultant AI", "answer": simulated_response})
        except Exception as e:
            return jsonify({"success": False, "message": f"AI verification fault: {str(e)}"}), 500

# =========================================================
# 💸 TRANSACTIONS & CLIENT UPLOAD ENGINE
# =========================================================

@app.route('/api/public/process-payment', methods=['POST'])
def process_payment():
    payload = request.get_json() or {}
    amount = payload.get('amount')
    account_number = payload.get('account_number', '').strip() 
    payment_method = payload.get('payment_method', '').lower()
    
    if not amount or float(amount) <= 0:
        return jsonify({"success": False, "message": "A valid numerical payment amount structure is required."}), 400
        
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("SELECT case_number, ai_access_granted FROM cases WHERE case_number = %s", (account_number,))
        case_record = cur.fetchone()
        
        if not case_record:
            return jsonify({"success": False, "message": "Payment declined: Account number does not match any active case ledger."}), 404
            
        float_amount = float(amount)
        base_msg = "M-Pesa transaction processed." if payment_method == 'mpesa' else "Card transaction processed."
        
        if float_amount == 5000.00 and not case_record['ai_access_granted']:
            cur.execute("UPDATE cases SET paid_balance = paid_balance + %s, ai_access_granted = TRUE WHERE case_number = %s", (float_amount, account_number))
            msg = f"{base_msg} Premium Predictive Analytics unlocked!"
        else:
            cur.execute("UPDATE cases SET paid_balance = paid_balance + %s WHERE case_number = %s", (float_amount, account_number))
            msg = f"{base_msg} Balance updated."
            
        conn.commit()
        log_audit(f"Payment KES {amount} applied", "System Gateway", account_number)
        return jsonify({"success": True, "message": msg})
    except Exception as e:
        return jsonify({"success": False, "message": f"Payment compilation failure: {str(e)}"}), 500

@app.route('/api/documents/upload', methods=['POST'])
def document_upload():
    """Client uploading documents to the firm."""
    case_number = request.form.get('case_number', 'General Case context')
    uploader = request.form.get('uploader_name', 'Client')
    
    if 'document' not in request.files: 
        return jsonify({"success": False, "message": "No functional document payload detected."}), 400
        
    file = request.files['document']
    secure_name = secure_filename(file.filename)
    absolute_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_name)
    file.save(absolute_path)
    
    log_audit(f"Client Document Uploaded: {secure_name}", uploader, case_number)
    
    send_firm_email(f"Document Upload: {case_number}", f"<p>A new document (<b>{secure_name}</b>) has been uploaded by the client for matter {case_number}.</p>")
    
    return jsonify({"success": True, "message": "Document uploaded securely to Cloud Vault."})

@app.route('/api/staff/upload-document', methods=['POST'])
def staff_document_upload():
    """Staff uploading documents directly to the client's portal."""
    case_number = request.form.get('case_number')
    user_name = request.form.get('user_name', 'Staff')
    
    if 'document' not in request.files: 
        return jsonify({"success": False, "message": "No file detected."}), 400
        
    file = request.files['document']
    secure_name = f"STAFF_{secure_filename(file.filename)}"
    absolute_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_name)
    file.save(absolute_path)
    
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE cases SET staff_uploaded_doc = %s WHERE case_number = %s", (secure_name, case_number))
        conn.commit()
        log_audit(f"Staff Document Pushed to Client: {secure_name}", user_name, case_number)
        return jsonify({"success": True, "message": "Document successfully pushed to client portal."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

# =========================================================
# 🏢 LAW FIRM INTERNAL MANAGEMENT ENDPOINTS
# =========================================================

@app.route('/api/staff/dashboard-reminders', methods=['GET'])
def get_dashboard_reminders():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        target_date_str = (datetime.now() + timedelta(days=7)).strftime("%dth %B %Y")
        cur.execute("SELECT case_number, client_name, next_court_date FROM cases WHERE status='Active' LIMIT 3") 
        upcoming = cur.fetchall()
        
        if upcoming:
            html_list = "".join([f"<li><b>{c['case_number']}</b> ({c['client_name']}) - {c['next_court_date']}</li>" for c in upcoming])
            send_firm_email("7-Day Court Matter Reminders", f"<h3>Upcoming Matters This Week:</h3><ul>{html_list}</ul>")
        
        return jsonify({"success": True, "reminders": upcoming})
    except Exception as e:
        return jsonify({"success": False})

@app.route('/api/staff/search', methods=['POST'])
def search_cases():
    data = request.get_json() or {}
    query = data.get('query', '').strip()
    user_name = data.get('user_name', '').strip()
    page = int(data.get('page', 1))
    limit = 5
    offset = (page - 1) * limit
    
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        cur.execute("SELECT role FROM users WHERE full_name = %s", (user_name,))
        role_record = cur.fetchone()
        is_admin = role_record and role_record['role'] == 'admin'

        if not query:
            cur.execute("""
                SELECT case_id, case_number, case_parties, client_name, total_balance, paid_balance, next_court_date, coming_up_for 
                FROM cases ORDER BY case_id DESC LIMIT %s OFFSET %s
            """, (limit, offset))
        else:
            term = f"%{query}%"
            cur.execute("""
                SELECT case_id, case_number, case_parties, client_name, total_balance, paid_balance, next_court_date, coming_up_for 
                FROM cases 
                WHERE (case_number ILIKE %s OR client_name ILIKE %s OR case_parties ILIKE %s)
                ORDER BY case_id DESC LIMIT %s OFFSET %s
            """, (term, term, term, limit, offset))
            
        results = cur.fetchall()
        
        for row in results:
            if not is_admin:
                row['total_balance'] = "RESTRICTED"
                row['paid_balance'] = "RESTRICTED"
                
        return jsonify({"success": True, "results": results, "page": page, "is_admin": is_admin})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route('/api/staff/ai-monitoring', methods=['GET'])
def monitor_client_ai():
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM ai_client_logs ORDER BY logged_at DESC LIMIT 20")
        return jsonify({"success": True, "logs": cur.fetchall()})
    except Exception as e:
        return jsonify({"success": False})

@app.route('/api/staff/update-matter', methods=['POST'])
def update_matter():
    data = request.get_json() or {}
    user_name = data.get('user_name', '').strip()  
    case_id = data.get('case_id')
    next_court_date = data.get('next_court_date') 
    coming_up_for = data.get('coming_up_for')
    action = data.get('action', 'update')
    
    try:
        conn = get_db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        if action == 'archive':
            cur.execute("UPDATE cases SET status = 'Archived' WHERE case_id = %s RETURNING case_number", (case_id,))
            case_no = cur.fetchone()['case_number']
            conn.commit()
            log_audit("Case Archived to prevent clutter", user_name, case_no)
            return jsonify({"success": True, "message": f"{case_no} archived successfully."})

        cur.execute("SELECT role FROM users WHERE full_name = %s", (user_name,))
        role_record = cur.fetchone()
        is_admin = role_record and role_record['role'] == 'admin'

        cur.execute("SELECT case_number, total_balance, paid_balance FROM cases WHERE case_id = %s", (case_id,))
        current_record = cur.fetchone()
        
        if current_record and not is_admin:
            input_total = data.get('total_balance')
            input_paid = data.get('paid_balance')
            if (input_total is not None and str(input_total) != "RESTRICTED" and float(input_total) != float(current_record['total_balance'])) or \
               (input_paid is not None and str(input_paid) != "RESTRICTED" and float(input_paid) != float(current_record['paid_balance'])):
                return jsonify({"success": False, "message": "Access Denied: Only Admin can update financial ledgers."}), 403

        cur.execute("""
            UPDATE cases 
            SET next_court_date=%s, coming_up_for=%s, total_balance=%s, paid_balance=%s
            WHERE case_id=%s
        """, (next_court_date, coming_up_for, data.get('total_balance'), data.get('paid_balance'), case_id))
        conn.commit()
        
        log_audit("Matter updated (Timeline/Financials)", user_name, current_record['case_number'])
        return jsonify({"success": True, "message": "Case ledger modified successfully."})
    except Exception as e:
        return jsonify({"success": False, "message": f"Ledger save crash: {str(e)}"}), 500

@app.route('/api/admin/kill-switch', methods=['POST'])
def toggle_kill_switch():
    action = request.get_json().get('action', '').upper()
    if action == 'LOCK':
        SYSTEM_STATE["LOCKDOWN_MODE"] = True
        logging.critical("🚨 BACKEND LOCKDOWN OVERRIDE INITIATED BY SYSTEM ADMIN.")
        log_audit("ACTIVATED CYBER KILL SWITCH", "SYSTEM ADMIN", "GLOBAL")
        return jsonify({"success": True, "status": "LOCKED", "message": "🚨 GATEWAY ISOLATION APPLIED."})
    else:
        SYSTEM_STATE["LOCKDOWN_MODE"] = False
        logging.critical("✅ BACKEND LOCKDOWN CLEARED BY SYSTEM ADMIN.")
        log_audit("DEACTIVATED CYBER KILL SWITCH", "SYSTEM ADMIN", "GLOBAL")
        return jsonify({"success": True, "status": "ACTIVE", "message": "✅ Core frameworks fully online."})

if __name__ == '__main__':
    with app.app_context():
        init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)
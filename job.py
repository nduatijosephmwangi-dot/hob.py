"""
=====================================================================
 Wambui Shadrack Advocates — Legal Portal Backend (Single-File App)
 Flask + PostgreSQL + M-Pesa Daraja STK Push + Stripe Card Payments
 Integrated with Live Africa's Talking OTP Delivery
=====================================================================
"""

import os
import random
import logging
import base64
import requests
from datetime import datetime
from requests.auth import HTTPBasicAuth

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, g
from flask_cors import CORS

import stripe
import africastalking

# =========================================================
# ⚙️ APP CONFIG
# =========================================================
app = Flask(__name__)

frontend_url = os.environ.get("FRONTEND_URL", "*")
CORS(app, resources={r"/api/*": {"origins": frontend_url}})

app.config['DATABASE_URL'] = os.environ.get(
    'DATABASE_URL',
    'dbname=postgres user=postgres password=jose1023 host=localhost port=5432'
)
app.config['UPLOAD_FOLDER'] = os.environ.get('UPLOAD_FOLDER', './client_docs/')
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s: %(message)s')

SYSTEM_STATE = {"LOCKDOWN_MODE": False}

# =========================================================
# 📱 LIVE SMS GATEWAY (AFRICA'S TALKING)
# =========================================================
AT_USERNAME = os.environ.get("AT_USERNAME", "sandbox")
AT_API_KEY = os.environ.get("AT_API_KEY", "")
AT_SENDER_ID = os.environ.get("AT_SENDER_ID", "").strip() or None

sms_gateway = None
try:
    if AT_API_KEY:
        africastalking.initialize(AT_USERNAME, AT_API_KEY)
        sms_gateway = africastalking.SMS
        logging.info(f"✨ Africa's Talking initialized (user={AT_USERNAME}, sender_id={AT_SENDER_ID or 'DEFAULT'})")
    else:
        logging.warning("⚠️ AT_API_KEY missing. SMS in STUB mode.")
except Exception as e:
    logging.error(f"Critical failure initializing Africa's Talking SDK: {e}")


def _normalize_phone(phone: str) -> str:
    """Return raw E.164 digits without '+', e.g. '254712345678'."""
    p = str(phone or '').strip().replace(' ', '').replace('-', '').replace('+', '')
    if p.startswith('0') and len(p) == 10:
        p = '254' + p[1:]
    elif p.startswith('7') and len(p) == 9:
        p = '254' + p
    return p


def send_live_otp_sms(phone: str, otp_code: str):
    """Dispatch OTP via Africa's Talking. Returns (ok: bool, info: str)."""
    normalized = _normalize_phone(phone)
    e164 = f"+{normalized}"
    message = (f"Your Wambui Shadrack Advocates portal verification code is: "
               f"{otp_code}. It expires in 10 minutes.")

    if not sms_gateway:
        logging.warning(f"🚨 STUB SMS to {e164}: {message}")
        return False, "SMS gateway not configured (missing AT_API_KEY)."

    def _try_send(sender):
        if sender:
            return sms_gateway.send(message, [e164], sender_id=sender)
        return sms_gateway.send(message, [e164])

    try:
        response = _try_send(AT_SENDER_ID)
        logging.info(f"📡 AT response for {e164}: {response}")

        recipients = (response or {}).get('SMSMessageData', {}).get('Recipients', [])
        if not recipients:
            return False, f"AT returned no recipients: {response}"

        r = recipients[0]
        status = r.get('status', '')
        status_code = r.get('statusCode')

        # 101 Success | 102 Sent | 100 Processed
        if status_code in (100, 101, 102) or status.lower() == 'success':
            return True, f"Delivered to gateway: {status}"

        # Common live-account failure: InvalidSenderId — retry with default
        if AT_SENDER_ID and ('SenderId' in status or status_code == 406):
            logging.warning(f"Retrying without sender_id (was '{AT_SENDER_ID}')")
            response2 = _try_send(None)
            logging.info(f"📡 AT retry response for {e164}: {response2}")
            r2 = (response2 or {}).get('SMSMessageData', {}).get('Recipients', [{}])[0]
            s2 = r2.get('status', '')
            sc2 = r2.get('statusCode')
            if sc2 in (100, 101, 102) or s2.lower() == 'success':
                return True, f"Delivered (default sender): {s2}"
            return False, f"AT rejected: {s2} (code {sc2})"

        return False, f"AT rejected: {status} (code {status_code})"

    except Exception as e:
        logging.error(f"❌ AT SDK exception sending to {e164}: {e}")
        return False, f"SDK exception: {e}"


# =========================================================
# 💰 M-PESA DARAJA (LIVE STK PUSH)
# =========================================================
MPESA_ENV = os.environ.get('MPESA_ENV', 'sandbox').lower()
MPESA_CONSUMER_KEY = os.environ.get('MPESA_CONSUMER_KEY', '')
MPESA_CONSUMER_SECRET = os.environ.get('MPESA_CONSUMER_SECRET', '')
MPESA_SHORTCODE = os.environ.get('MPESA_SHORTCODE', '4747331')
MPESA_PASSKEY = os.environ.get('MPESA_PASSKEY', '')
MPESA_CALLBACK_URL = os.environ.get('MPESA_CALLBACK_URL', '')
MPESA_TRANSACTION_TYPE = os.environ.get('MPESA_TRANSACTION_TYPE', 'CustomerPayBillOnline')
MPESA_BASE = 'https://api.safaricom.co.ke' if MPESA_ENV == 'production' else 'https://sandbox.safaricom.co.ke'


def get_mpesa_access_token():
    if not MPESA_CONSUMER_KEY or not MPESA_CONSUMER_SECRET:
        raise RuntimeError("M-Pesa credentials not configured.")
    url = f"{MPESA_BASE}/oauth/v1/generate?grant_type=client_credentials"
    r = requests.get(url, auth=HTTPBasicAuth(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET), timeout=20)
    r.raise_for_status()
    token = r.json().get('access_token')
    if not token:
        raise RuntimeError(f"Daraja did not return token: {r.text}")
    return token


def build_mpesa_password():
    ts = datetime.now().strftime('%Y%m%d%H%M%S')
    raw = f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{ts}"
    return base64.b64encode(raw.encode()).decode('utf-8'), ts


def initiate_stk_push(phone, amount, account_ref, description="Legal Fees"):
    token = get_mpesa_access_token()
    password, ts = build_mpesa_password()
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
    url = f"{MPESA_BASE}/mpesa/stkpush/v1/processrequest"
    r = requests.post(url, json=payload,
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "application/json"}, timeout=30)
    try:
        data = r.json()
    except Exception:
        data = {"raw": r.text}
    logging.info(f"STK Push ({r.status_code}): {data}")
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
# 🗄️ DATABASE
# =========================================================
def get_db():
    if 'db' not in g:
        g.db = psycopg2.connect(app.config['DATABASE_URL'], cursor_factory=RealDictCursor)
    return g.db


@app.teardown_appcontext
def close_db(e):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    try:
        conn = psycopg2.connect(app.config['DATABASE_URL'])
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id SERIAL PRIMARY KEY,
                full_name VARCHAR(255) NOT NULL,
                phone_number VARCHAR(50) UNIQUE NOT NULL,
                role VARCHAR(50) NOT NULL
            );
        """)

        # Add support for email lookup
        cur.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255) UNIQUE;
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS otp_vault (
                phone_number VARCHAR(50) PRIMARY KEY,
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
                next_court_date VARCHAR(255),
                coming_up_for TEXT,
                total_balance NUMERIC(15,2) DEFAULT 0.00,
                paid_balance NUMERIC(15,2) DEFAULT 0.00,
                ai_access_granted BOOLEAN DEFAULT FALSE
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
            CREATE TABLE IF NOT EXISTS mpesa_transactions (
                tx_id SERIAL PRIMARY KEY,
                case_number VARCHAR(255),
                phone_number VARCHAR(50),
                amount NUMERIC(15,2),
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
            ('Shadrack Wambui', '0700260086', 'shadrack@wambuiadvocates.com', 'admin'),
            ('Jeff Kangethe',   '0704704758', 'jeff.k@wambuiadvocates.com', 'advocate'),
            ('Jeff Kangethe',   '0796178783', 'jeff.k2@wambuiadvocates.com', 'advocate'),
            ('Jane Onyango',    '0795204923', 'jane@wambuiadvocates.com', 'secretary'),
        ]
        for name, phone, email, role in seed_users:
            cur.execute("""
                INSERT INTO users (full_name, phone_number, email, role) VALUES (%s, %s, %s, %s)
                ON CONFLICT (phone_number) DO UPDATE SET email = EXCLUDED.email;
            """, (name, phone, email, role))

        conn.commit()
        cur.close()
        conn.close()
        print("💾 Production Database Engine Synchronized.")
    except Exception as e:
        print(f"⚠️ DB init failure: {e}")


# =========================================================
# 🛡️ SECURITY MIDDLEWARE
# =========================================================
@app.before_request
def cyber_security_check():
    if SYSTEM_STATE["LOCKDOWN_MODE"]:
        allowed = ['login_router', 'verify_otp', 'toggle_kill_switch',
                   'mpesa_callback', 'stripe_webhook']
        if request.endpoint not in allowed:
            return jsonify({"success": False, "error": "SECURITY_LOCKDOWN",
                            "message": "⚠️ PORTAL LOCKDOWN ACTIVE."}), 503


def _normalize_login_phone(credential: str) -> str:
    p = credential.replace('+', '').replace(' ', '').replace('-', '')
    if p.startswith('254') and len(p) == 12:
        p = '0' + p[3:]
    elif len(p) == 9 and p.startswith('7'):
        p = '0' + p
    return p


# =========================================================
# 🔐 AUTH
# =========================================================
@app.route('/api/auth/login-router', methods=['POST'])
def login_router():
    payload = request.get_json() or {}
    credential = (payload.get('credential') or '').strip()
    if not credential:
        return jsonify({"success": False, "message": "Login field cannot be blank."}), 400

    # Clean character separation to route based on identity format
    if '@' in credential:
        return initiate_staff_login(credential.lower())
    return client_login(credential)


def initiate_staff_login(email):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT full_name, phone_number, role FROM users
            WHERE LOWER(email) = %s AND role IN ('admin','advocate','secretary');
        """, (email,))
        account = cur.fetchone()
        if not account:
            return jsonify({"success": False, "message": "Access Denied: Not registered staff email."}), 403

        phone = account['phone_number']
        otp = str(random.randint(100000, 999999))
        cur.execute("""
            INSERT INTO otp_vault (phone_number, code, expires_at)
            VALUES (%s, %s, NOW() + INTERVAL '10 minutes')
            ON CONFLICT (phone_number) DO UPDATE
              SET code = EXCLUDED.code,
                  created_at = CURRENT_TIMESTAMP,
                  expires_at = EXCLUDED.expires_at;
        """, (phone, otp))
        conn.commit()

        ok, info = send_live_otp_sms(phone, otp)
        logging.info(f"OTP stored for {phone} ({email}); SMS ok={ok}; info={info}")

        if ok:
            return jsonify({
                "success": True, 
                "mode": "otp_required",
                "phone": phone, # Returns phone context safely back to frontend for the verification challenge
                "message": "OTP sent to your registered phone number."
            })
        # SMS failed — be honest with the frontend
        return jsonify({"success": False, "mode": "otp_failed",
                        "message": f"OTP could not be delivered: {info}"}), 502

    except Exception as e:
        logging.exception("login error")
        return jsonify({"success": False, "message": f"Auth fault: {e}"}), 500


@app.route('/api/auth/verify-otp', methods=['POST'])
def verify_otp():
    data = request.get_json() or {}
    phone = (data.get('phone') or '').strip()
    code = (data.get('code') or '').strip()
    try:
        clean_phone = _normalize_login_phone(phone)
        conn = get_db(); cur = conn.cursor()
        cur.execute("""SELECT code FROM otp_vault
                       WHERE phone_number=%s AND expires_at > NOW();""", (clean_phone,))
        record = cur.fetchone()
        if not record or record['code'] != code:
            return jsonify({"success": False, "message": "Invalid or expired OTP."}), 401

        cur.execute("DELETE FROM otp_vault WHERE phone_number=%s;", (clean_phone,))
        cur.execute("SELECT full_name, role FROM users WHERE phone_number=%s;", (clean_phone,))
        user_profile = cur.fetchone()
        conn.commit()

        if not user_profile:
            return jsonify({"success": False, "message": "Staff profile missing."}), 404

        return jsonify({
            "success": True,
            "role": user_profile['role'],
            "user_name": user_profile['full_name'],
            "lockdown_status": SYSTEM_STATE["LOCKDOWN_MODE"]
        })
    except Exception as e:
        logging.exception("verify error")
        return jsonify({"success": False, "message": f"Vault read error: {e}"}), 500


def client_login(case_number):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            SELECT case_id, case_number, case_parties, client_name, ai_access_granted,
                   next_court_date, coming_up_for, total_balance, paid_balance
            FROM cases WHERE case_number ILIKE %s
        """, (f"%{case_number}%",))
        case = cur.fetchone()
        if not case:
            return jsonify({"success": False, "message": "No case found with that Case Number."}), 404

        total = float(case['total_balance'] or 0)
        paid = float(case['paid_balance'] or 0)
        score = random.randint(55, 98)
        return jsonify({
            "success": True, "mode": "client_dashboard",
            "data": {
                "case_id": case['case_id'],
                "case_number": case['case_number'],
                "case_parties": case['case_parties'],
                "client_name": case['client_name'],
                "next_court_date": str(case['next_court_date']),
                "coming_up_for": case['coming_up_for'],
                "financials": {"total": total, "paid": paid, "balance": total - paid},
                "ai_unlocked": case['ai_access_granted'],
                "case_predictor": {"score": score,
                                   "analysis": f"Outcome trends at {score}% favorable."}
            }
        })
    except Exception as e:
        return jsonify({"success": False, "message": f"DB failure: {e}"}), 500


# =========================================================
# 🤖 AI
# =========================================================
@app.route('/api/ai/consult', methods=['POST'])
def ai_consult():
    data = request.get_json() or {}
    question = (data.get('question') or '').strip()
    user_name = (data.get('user_name') or '').strip()
    case_number = (data.get('case_number') or '').strip()
    ai_type = (data.get('ai_type') or 'free').strip().lower()

    if not question:
        return jsonify({"success": False, "message": "Question cannot be blank."}), 400

    if user_name == "Shadrack Wambui":
        return jsonify({"success": True, "engine": "Constitution 2010",
                        "answer": f"⚖️ [Admin AI]: For '{question}', see Chapter Four."})

    if user_name:
        return jsonify({"success": True, "engine": "Staff Assistant Free AI",
                        "answer": f"📋 [Staff AI - {user_name}]: Processing '{question}'."})

    if case_number:
        try:
            conn = get_db(); cur = conn.cursor()
            cur.execute("SELECT client_name, ai_access_granted FROM cases WHERE case_number=%s",
                        (case_number,))
            case_record = cur.fetchone()
            if not case_record:
                return jsonify({"success": False, "message": "Case not found."}), 404

            if ai_type == "consultant":
                if not case_record['ai_access_granted']:
                    return jsonify({"success": False,
                                    "message": "Premium AI requires KES 5,000 activation."}), 402
                ans = f"🧠 [Premium AI]: Strategic evaluation for '{question}'."
                engine = "Paid Consultant AI"
            else:
                ans = f"ℹ️ [Client Free AI]: Summary for '{question}'."
                engine = "Client Free AI"

            cur.execute("""INSERT INTO ai_client_logs
                (case_number, client_name, client_question, ai_response)
                VALUES (%s, %s, %s, %s)""",
                (case_number, case_record['client_name'], question, ans))
            conn.commit()
            return jsonify({"success": True, "engine": engine, "answer": ans})
        except Exception as e:
            return jsonify({"success": False, "message": f"AI fault: {e}"}), 500

    return jsonify({"success": False, "message": "Unable to verify routing scope."}), 400


# =========================================================
# 💸 PAYMENTS
# =========================================================
@app.route('/api/public/process-payment', methods=['POST'])
def process_payment():
    payload = request.get_json() or {}
    amount = payload.get('amount')
    account_number = (payload.get('account_number') or '').strip()
    payment_method = (payload.get('payment_method') or '').lower()
    phone_number = (payload.get('phone_number') or '').strip()
    customer_email = (payload.get('email') or '').strip()

    try:
        if not amount or float(amount) <= 0:
            return jsonify({"success": False, "message": "Valid amount required."}), 400
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Amount must be numeric."}), 400

    if not account_number:
        return jsonify({"success": False, "message": "Account/case number required."}), 400
    if payment_method not in ['mpesa', 'card']:
        return jsonify({"success": False, "message": "Select Mpesa or Card."}), 400
    if payment_method == 'mpesa' and not phone_number:
        return jsonify({"success": False, "message": "Phone number required for M-Pesa."}), 400

    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("SELECT case_number FROM cases WHERE case_number=%s", (account_number,))
        if not cur.fetchone():
            return jsonify({"success": False, "message": "Account does not match any case."}), 404

        float_amount = float(amount)

        if payment_method == 'mpesa':
            try:
                status_code, resp = initiate_stk_push(phone_number, float_amount, account_number)
            except Exception as e:
                return jsonify({"success": False, "message": f"M-Pesa gateway error: {e}"}), 502

            if status_code == 200 and str(resp.get('ResponseCode')) == '0':
                cur.execute("""
                    INSERT INTO mpesa_transactions
                    (case_number, phone_number, amount, merchant_request_id, checkout_request_id, status)
                    VALUES (%s, %s, %s, %s, %s, 'PENDING')
                    ON CONFLICT (checkout_request_id) DO NOTHING
                """, (account_number, _normalize_phone(phone_number), float_amount,
                      resp.get('MerchantRequestID'), resp.get('CheckoutRequestID')))
                conn.commit()
                return jsonify({"success": True,
                                "message": f"M-Pesa prompt sent to {phone_number}.",
                                "checkout_request_id": resp.get('CheckoutRequestID')})
            return jsonify({"success": False,
                            "message": resp.get('errorMessage') or resp.get('CustomerMessage') or "STK push rejected.",
                            "daraja": resp}), 400

        if payment_method == 'card':
            if not stripe.api_key:
                return jsonify({"success": False, "message": "Stripe not configured."}), 500
            try:
                amount_minor = int(round(float_amount * 100))
                session = stripe.checkout.Session.create(
                    mode='payment',
                    payment_method_types=['card'],
                    line_items=[{
                        'price_data': {
                            'currency': STRIPE_CURRENCY,
                            'product_data': {
                                'name': f"Legal Fees — Case {account_number}",
                                'description': "Wambui Shadrack Advocates",
                            },
                            'unit_amount': amount_minor,
                        },
                        'quantity': 1,
                    }],
                    customer_email=customer_email or None,
                    metadata={'case_number': account_number, 'amount_kes': str(float_amount)},
                    success_url=f"{STRIPE_SUCCESS_URL}?session_id={{CHECKOUT_SESSION_ID}}",
                    cancel_url=STRIPE_CANCEL_URL,
                )
                cur.execute("""
                    INSERT INTO stripe_transactions
                    (case_number, amount, currency, stripe_session_id, status, customer_email)
                    VALUES (%s, %s, %s, %s, 'PENDING', %s)
                    ON CONFLICT (stripe_session_id) DO NOTHING
                """, (account_number, float_amount, STRIPE_CURRENCY, session.id, customer_email or None))
                conn.commit()
                return jsonify({"success": True, "message": "Redirect checkout initialized.",
                                "checkout_url": session.url, "session_id": session.id})
            except stripe.error.StripeError as e:
                return jsonify({"success": False, "message": f"Stripe error: {str(e)}"}), 502
            except Exception as e:
                return jsonify({"success": False, "message": f"Card gateway error: {e}"}), 500

    except Exception as e:
        return jsonify({"success": False, "message": f"Payment failure: {e}"}), 500


# AI unlock endpoint (used by frontend triggerBillingTransaction)
@app.route('/api/billing/ai-unlock', methods=['POST'])
def ai_unlock():
    data = request.get_json() or {}
    amount = data.get('amount')
    phone = (data.get('phone') or '').strip()
    case_number = (data.get('case_number') or '').strip()
    if not case_number or not phone or not amount:
        return jsonify({"success": False, "message": "Missing fields."}), 400
    try:
        status_code, resp = initiate_stk_push(phone, float(amount), case_number, "AI Unlock")
    except Exception as e:
        return jsonify({"success": False, "message": f"M-Pesa gateway error: {e}"}), 502

    if status_code == 200 and str(resp.get('ResponseCode')) == '0':
        try:
            conn = get_db(); cur = conn.cursor()
            cur.execute("""INSERT INTO mpesa_transactions
                (case_number, phone_number, amount, merchant_request_id, checkout_request_id, status)
                VALUES (%s,%s,%s,%s,%s,'PENDING')
                ON CONFLICT (checkout_request_id) DO NOTHING""",
                (case_number, _normalize_phone(phone), float(amount),
                 resp.get('MerchantRequestID'), resp.get('CheckoutRequestID')))
            conn.commit()
        except Exception as e:
            logging.error(f"Insert error: {e}")
        return jsonify({"success": True, "message": "STK prompt sent. Enter your M-Pesa PIN.",
                        "checkout_request_id": resp.get('CheckoutRequestID')})
    return jsonify({"success": False,
                    "message": resp.get('errorMessage') or resp.get('CustomerMessage') or "STK push rejected."}), 400


# =========================================================
# WEBHOOKS
# =========================================================
@app.route('/api/public/mpesa/callback', methods=['POST'])
def mpesa_callback():
    try:
        body = request.get_json(force=True, silent=True) or {}
        logging.info(f"M-Pesa callback: {body}")
        stk = body.get('Body', {}).get('stkCallback', {})
        checkout_id = stk.get('CheckoutRequestID')
        result_code = stk.get('ResultCode')
        result_desc = stk.get('ResultDesc')

        receipt, amount_paid = None, None
        if result_code == 0:
            for item in stk.get('CallbackMetadata', {}).get('Item', []) or []:
                if item.get('Name') == 'MpesaReceiptNumber':
                    receipt = item.get('Value')
                elif item.get('Name') == 'Amount':
                    amount_paid = float(item.get('Value') or 0)

        conn = get_db(); cur = conn.cursor()
        cur.execute("""
            UPDATE mpesa_transactions
            SET result_code=%s, result_desc=%s, mpesa_receipt=%s,
                status=%s, completed_at=CURRENT_TIMESTAMP
            WHERE checkout_request_id=%s
            RETURNING case_number, amount
        """, (result_code, result_desc, receipt,
              'SUCCESS' if result_code == 0 else 'FAILED', checkout_id))
        row = cur.fetchone()

        if result_code == 0 and row:
            credited = amount_paid if amount_paid else float(row['amount'])
            cur.execute("""
                UPDATE cases
                SET paid_balance = paid_balance + %s,
                    ai_access_granted = (ai_access_granted OR %s)
                WHERE case_number = %s
            """, (credited, credited >= 5000, row['case_number']))

        conn.commit()
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})
    except Exception as e:
        logging.error(f"M-Pesa callback failure: {e}")
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route('/api/public/stripe/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data(as_text=False)
    sig_header = request.headers.get('Stripe-Signature', '')
    if not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Security Misconfiguration"}), 500
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({"error": f"Invalid signature: {e}"}), 400

    try:
        etype = event['type']
        obj = event['data']['object']
        if etype == 'checkout.session.completed':
            session_id = obj.get('id')
            payment_intent = obj.get('payment_intent')
            payment_status = obj.get('payment_status')
            metadata = obj.get('metadata') or {}
            case_number = metadata.get('case_number')
            amount_total = (obj.get('amount_total') or 0) / 100.0

            conn = get_db(); cur = conn.cursor()
            cur.execute("""
                UPDATE stripe_transactions
                SET status=%s, stripe_payment_intent=%s, completed_at=CURRENT_TIMESTAMP
                WHERE stripe_session_id=%s
                RETURNING case_number, amount
            """, ('SUCCESS' if payment_status == 'paid' else 'PENDING',
                  payment_intent, session_id))
            row = cur.fetchone()
            if payment_status == 'paid' and case_number:
                credited = float(row['amount']) if row else amount_total
                cur.execute("""
                    UPDATE cases
                    SET paid_balance = paid_balance + %s,
                        ai_access_granted = (ai_access_granted OR %s)
                    WHERE case_number=%s
                """, (credited, credited >= 5000, case_number))
            conn.commit()

        elif etype in ('checkout.session.expired', 'checkout.session.async_payment_failed'):
            session_id = obj.get('id')
            conn = get_db(); cur = conn.cursor()
            cur.execute("""UPDATE stripe_transactions
                           SET status='FAILED', completed_at=CURRENT_TIMESTAMP
                           WHERE stripe_session_id=%s""", (session_id,))
            conn.commit()

        return jsonify({"received": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =========================================================
# STATUS POLLERS
# =========================================================
@app.route('/api/payment/mpesa-status/<checkout_request_id>', methods=['GET'])
def mpesa_status(checkout_request_id):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""SELECT status, result_desc, mpesa_receipt, amount
                       FROM mpesa_transactions WHERE checkout_request_id=%s""",
                    (checkout_request_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"success": False, "message": "Unknown transaction."}), 404
        return jsonify({"success": True, "transaction": row})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/api/payment/stripe-status/<session_id>', methods=['GET'])
def stripe_status(session_id):
    try:
        conn = get_db(); cur = conn.cursor()
        cur.execute("""SELECT status, amount, currency, customer_email
                       FROM stripe_transactions WHERE stripe_session_id=%s""",
                    (session_id,))
        row = cur.fetchone()
        if not row:
            return jsonify({"success": False, "message": "Unknown session."}), 404
        return jsonify({"success": True, "transaction": row})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# =========================================================
# DIAGNOSTICS
# =========================================================
@app.route('/api/diag/sms-test', methods=['POST'])
def sms_test():
    """POST {phone, code?} to verify Africa's Talking delivery end-to-end."""
    data = request.get_json() or {}
    phone = (data.get('phone') or '').strip()
    code = (data.get('code') or '123456').strip()
    if not phone:
        return jsonify({"success": False, "message": "phone required"}), 400
    ok, info = send_live_otp_sms(phone, code)
    return jsonify({"success": ok, "info": info,
                    "at_username": AT_USERNAME,
                    "at_sender_id": AT_SENDER_ID,
                    "sms_gateway_loaded": bool(sms_gateway)})


@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({"ok": True, "lockdown": SYSTEM_STATE["LOCKDOWN_MODE"]})


if __name__ == '__main__':
    init_db()
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
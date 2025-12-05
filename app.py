import os
import base64
import json
import time
import requests
import psycopg2
from flask import Flask, jsonify, request, abort
from flask_cors import CORS
from flask_socketio import SocketIO, emit, join_room, leave_room

# Eventlet is recommended for Flask-SocketIO on Railway/Render
import eventlet
eventlet.monkey_patch()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Database URL
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    conn = psycopg2.connect(DATABASE_URL)
    return conn

# --- Initial DB Setup (MERGED) ---
def run_sql_setup():
    conn = None
    if not DATABASE_URL:
        print("DATABASE_URL not found. Skipping database setup.")
        return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # 1. Lawyers table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lawyers (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                rating FLOAT DEFAULT 0,
                is_online BOOLEAN DEFAULT FALSE,
                last_active TIMESTAMP,
                user_id INT -- Added for dispatch logic
            );
        """)
        
        # 2. Wallets table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                id SERIAL PRIMARY KEY,
                user_id INT,
                role TEXT,
                balance FLOAT DEFAULT 0
            );
        """)

        # 3. Case requests table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS case_requests (
                id SERIAL PRIMARY KEY,
                user_id INT,
                case_type TEXT,
                latitude FLOAT,
                longitude FLOAT,
                status TEXT DEFAULT 'searching',
                assigned_lawyer INT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        # 4. Transactions table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                from_wallet INT,
                to_wallet INT,
                amount FLOAT,
                type TEXT,
                status TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        # 5. Ratings table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id SERIAL PRIMARY KEY,
                user_id INT,
                lawyer_id INT,
                stars INT,
                comment TEXT
            );
        """)

        # 6. Mpesa Webhooks table (NEW)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mpesa_webhooks (
                id SERIAL PRIMARY KEY,
                payload JSONB,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)

        conn.commit()
        cur.close()
        print("Database setup complete.")

    except Exception as e:
        print(f"Database setup failed: {e}")
    finally:
        if conn is not None:
            conn.close()

# run_sql_setup() # Keep this commented out unless you need to re-run setup

# --- MPesa credentials & helpers ---
MPESA_CONSUMER_KEY = os.environ.get('MPESA_CONSUMER_KEY')
MPESA_CONSUMER_SECRET = os.environ.get('MPESA_CONSUMER_SECRET')
MPESA_SHORTCODE = os.environ.get('MPESA_SHORTCODE')         # e.g. 174379 (sandbox)
MPESA_PASSKEY = os.environ.get('MPESA_PASSKEY')             # STK passkey
MPESA_CALLBACK_URL = os.environ.get('MPESA_CALLBACK_URL')   # set on Railway to /api/mpesa/webhook

MPESA_BASE = "https://sandbox.safaricom.co.ke"  # sandbox; switch to production endpoint in prod

def get_mpesa_token( ):
    if not (MPESA_CONSUMER_KEY and MPESA_CONSUMER_SECRET):
        raise RuntimeError("MPESA credentials missing")
    key_secret = f"{MPESA_CONSUMER_KEY}:{MPESA_CONSUMER_SECRET}"
    b64 = base64.b64encode(key_secret.encode()).decode()
    headers = {"Authorization": f"Basic {b64}"}
    resp = requests.get(f"{MPESA_BASE}/oauth/v1/generate?grant_type=client_credentials", headers=headers)
    resp.raise_for_status()
    return resp.json()['access_token']

def lipa_na_mpesa_stk_push(phone_number, amount, account_reference, transaction_desc):
    """
    Initiate STK Push (Lipa Na Mpesa Online)
    Returns API response (and should include checkout request id)
    """
    token = get_mpesa_token()
    timestamp = time.strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}".encode()).decode()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount),
        "PartyA": phone_number,           # customer phone in format 2547XXXXXXXX
        "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": phone_number,
        "CallBackURL": MPESA_CALLBACK_URL,
        "AccountReference": account_reference,
        "TransactionDesc": transaction_desc
    }
    r = requests.post(f"{MPESA_BASE}/mpesa/stkpush/v1/processrequest", json=payload, headers=headers)
    r.raise_for_status()
    return r.json()

# -------------------------------------------
# Socket.IO: presence & real-time notifications
# We will keep a simple mapping of lawyer_id -> sid room. In prod use Redis message queue for multiple instances.
LAWYER_ROOM_PREFIX = "lawyer_"

@socketio.on('connect')
def on_connect():
    # The frontend should emit 'identify' with { role, user_id } after connect
    print('Client connected', request.sid)

@socketio.on('identify')
def on_identify(data):
    # data: { role: 'lawyer'|'client', user_id: 123 }
    role = data.get('role')
    user_id = data.get('user_id')
    if role == 'lawyer' and user_id:
        room = f"{LAWYER_ROOM_PREFIX}{user_id}"
        join_room(room)
        emit('identified', {'room': room})
        print(f"Lawyer {user_id} joined room {room}")

@socketio.on('disconnect')
def on_disconnect():
    print('Client disconnected', request.sid)

# -------------------------------------------
# API Endpoint: create dispatch request and notify top online lawyers
@app.route('/api/dispatch/request', methods=['POST'])
def dispatch_request():
    """
    Body: { client_id, case_type, lat, lng, max_fee }
    - Save request to DB
    - Find candidate lawyers (simple: online lawyers ordered by rating)
    - Emit 'case_offer' event to their socket rooms (they will get the offer and accept)
    """
    body = request.get_json()
    client_id = body.get('client_id')
    case_type = body.get('case_type')
    lat = body.get('lat')
    lng = body.get('lng')
    if not (client_id and case_type):
        return jsonify({'error': 'client_id and case_type required'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    # Create a request row
    cur.execute("INSERT INTO case_requests (user_id, case_type, latitude, longitude) VALUES (%s,%s,%s,%s) RETURNING id", (client_id, case_type, lat, lng))
    req_id = cur.fetchone()[0]
    conn.commit()

    # Select top online verified lawyers (example: is_online true)
    cur.execute("""
        SELECT id, name, rating, user_id FROM lawyers
        WHERE is_online = TRUE
        ORDER BY rating DESC NULLS LAST LIMIT 10
    """)
    candidates = cur.fetchall()
    # Offer to top N (emit socket event)
    offered = []
    for row in candidates:
        lawyer_id, name, rating, user_id = row[0], row[1], row[2], row[3]
        room = f"{LAWYER_ROOM_PREFIX}{user_id}"
        payload = {
            'request_id': req_id,
            'client_id': client_id,
            'case_type': case_type,
            'lat': lat, 'lng': lng,
            'fee_estimate': 2000,  # you can compute dynamic surge
            'timestamp': int(time.time())
        }
        # emit event to the lawyer room
        socketio.emit('case_offer', payload, room=room)
        offered.append({'lawyer_id': lawyer_id, 'user_id': user_id})
    cur.close()
    conn.close()
    return jsonify({'request_id': req_id, 'offered': offered})

# Lawyer accepts offer (called from their app)
@app.route('/api/dispatch/<int:request_id>/accept', methods=['POST'])
def accept_offer(request_id):
    """
    Body: { lawyer_user_id }
    """
    body = request.get_json()
    lawyer_user_id = body.get('lawyer_user_id')
    if not lawyer_user_id:
        return jsonify({'error': 'lawyer_user_id required'}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    # Find request and set assigned_lawyer (store lawyer_id)
    # This example assumes lawyer.user_id -> lawyers.user_id mapping exists
    cur.execute("SELECT id FROM lawyers WHERE user_id=%s", (lawyer_user_id,))
    r = cur.fetchone()
    if not r:
        cur.close(); conn.close()
        return jsonify({'error': 'lawyer not found'}), 404
    lawyer_id = r[0]
    cur.execute("UPDATE case_requests SET status='accepted', assigned_lawyer=%s WHERE id=%s RETURNING user_id", (lawyer_id, request_id))
    res = cur.fetchone()
    conn.commit()
    # Notify client via socket too (if they are connected)
    client_user_id = res[0] if res else None
    if client_user_id:
        socketio.emit('offer_accepted', {'request_id': request_id, 'lawyer_id': lawyer_id}, room=f"client_{client_user_id}")
    cur.close()
    conn.close()
    return jsonify({'status': 'accepted', 'request_id': request_id, 'lawyer_id': lawyer_id})

# MPesa STK push API - initiate checkout from frontend
@app.route('/api/payments/mpesa/stk', methods=['POST'])
def mpesa_stk():
    """
    Body: { phone_number: '2547XXXXXXXX', amount: 2000, account_ref: 'case-123' }
    Returns MPesa response (checkout request)
    """
    body = request.get_json()
    phone = body.get('phone_number')
    amount = body.get('amount')
    account_ref = body.get('account_ref', 'LegalMatch')
    desc = body.get('desc', 'Payment for LegalMatch')

    try:
        resp = lipa_na_mpesa_stk_push(phone, amount, account_ref, desc)
        # return the response to client so they can show instructions
        return jsonify({'status': 'initiated', 'mpesa': resp})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# MPesa webhook callback (Daraja will POST here)
@app.route('/api/mpesa/webhook', methods=['POST'])
def mpesa_webhook():
    data = request.get_json(silent=True)
    # Save webhook JSON to db for auditing and update wallets when payment is successful
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO mpesa_webhooks (payload) VALUES (%s)", (json.dumps(data),))
    conn.commit()
    cur.close()
    conn.close()
    # Daraja expects 200 OK quickly
    return jsonify({'ResultCode': 0, 'ResultDesc': 'Accepted'})

# Simple health
@app.route('/api/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    # Use socketio.run so websockets work
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)

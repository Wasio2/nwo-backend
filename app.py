import os
import psycopg2
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# --- Database Connection ---
DATABASE_URL = os.environ.get('DATABASE_URL')

def get_db_connection():
    if not DATABASE_URL:
        raise Exception("DATABASE_URL not found")
    conn = psycopg2.connect(DATABASE_URL)
    return conn

# --- Initial DB Setup ---
def run_sql_setup():
    conn = None
    if not DATABASE_URL:
        print("DATABASE_URL not found. Skipping database setup.")
        return
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # Lawyers table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lawyers (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                rating FLOAT DEFAULT 0,
                is_online BOOLEAN DEFAULT FALSE,
                last_active TIMESTAMP
            );
        """)

        # Wallets table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wallets (
                id SERIAL PRIMARY KEY,
                user_id INT,
                role TEXT,
                balance FLOAT DEFAULT 0
            );
        """)

        # Case requests table
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

        # Transactions table
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

        # Ratings table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id SERIAL PRIMARY KEY,
                user_id INT,
                lawyer_id INT,
                stars INT,
                comment TEXT
            );
        """)
        # --- Insert a test lawyer if none exist ---
        cur.execute("SELECT COUNT(*) FROM lawyers;")
        if cur.fetchone()[0] == 0:
            cur.execute("""
                INSERT INTO lawyers (id, name, rating, is_online)
                VALUES (1, 'Atticus Finch', 4.8, TRUE);
            """)
            print("Inserted test lawyer: Atticus Finch")
        # -----------------------------------------

        conn.commit()
        cur.close()
        print("Database setup complete.")

        conn.commit()
        cur.close()
        print("Database setup complete.")
    except Exception as e:
        print(f"Database setup failed: {e}")
    finally:
        if conn:
            conn.close()

run_sql_setup()

# --- API Endpoints ---

@app.route('/api/message', methods=['GET'])
def get_message():
    return jsonify({
        "message": "Hello from your Railway Backend!",
        "status": "success"
    })

# 1. Update lawyer online/offline status
@app.route("/api/lawyer/status", methods=["POST"])
def update_lawyer_status():
    data = request.get_json()
    lawyer_id = data.get("lawyer_id")
    is_online = data.get("is_online")
    if lawyer_id is None or is_online is None:
        return jsonify({"error": "lawyer_id and is_online required"}), 400
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            UPDATE lawyers
            SET is_online = %s, last_active = NOW()
            WHERE id = %s
        """, (is_online, lawyer_id))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Lawyer status updated", "lawyer_id": lawyer_id, "is_online": is_online})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 2. List online lawyers
@app.route("/api/lawyer/list", methods=["GET"])
def list_online_lawyers():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT id, name, rating, is_online FROM lawyers
            WHERE is_online = TRUE
            ORDER BY rating DESC, last_active DESC
        """)
        lawyers = cur.fetchall()
        cur.close()
        conn.close()
        lawyer_list = [{"id": l[0], "name": l[1], "rating": l[2], "is_online": l[3]} for l in lawyers]
        return jsonify(lawyer_list)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 3. Request a lawyer (create case request)
@app.route("/api/request-lawyer", methods=["POST"])
def request_lawyer():
    data = request.get_json()
    user_id = data.get("user_id")
    case_type = data.get("case_type")
    lat = data.get("lat")
    lng = data.get("lng")
    if not all([user_id, case_type, lat, lng]):
        return jsonify({"error": "Missing parameters"}), 400
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO case_requests (user_id, case_type, latitude, longitude)
            VALUES (%s,%s,%s,%s) RETURNING id
        """, (user_id, case_type, lat, lng))
        request_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Request created", "request_id": request_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 4. Complete a case & handle wallet payout
@app.route("/api/case/complete", methods=["POST"])
def complete_case():
    data = request.get_json()
    case_id = data.get("case_id")
    amount = data.get("amount")
    lawyer_id = data.get("lawyer_id")
    if not all([case_id, amount, lawyer_id]):
        return jsonify({"error": "Missing parameters"}), 400
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Update case status
        cur.execute("UPDATE case_requests SET status='completed' WHERE id=%s", (case_id,))
        # Payout: 20% commission
        commission = amount * 0.2
        payout = amount - commission
        # Platform wallet assumed id=1
        cur.execute("UPDATE wallets SET balance = balance + %s WHERE id=1", (commission,))
        cur.execute("UPDATE wallets SET balance = balance + %s WHERE user_id=%s", (payout, lawyer_id))
        # Record transactions
        cur.execute("INSERT INTO transactions (from_wallet, to_wallet, amount, type, status) VALUES (0,%s,%s,'payout','success')", (lawyer_id, payout))
        cur.execute("INSERT INTO transactions (from_wallet, to_wallet, amount, type, status) VALUES (0,1,%s,'commission','success')", (commission,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Case completed & payout processed"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 5. Rate a lawyer
@app.route("/api/rate-lawyer", methods=["POST"])
def rate_lawyer():
    data = request.get_json()
    user_id = data.get("user_id")
    lawyer_id = data.get("lawyer_id")
    stars = data.get("stars")
    comment = data.get("comment","")
    if not all([user_id, lawyer_id, stars]):
        return jsonify({"error": "Missing parameters"}), 400
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO ratings (user_id, lawyer_id, stars, comment)
            VALUES (%s,%s,%s,%s)
        """, (user_id, lawyer_id, stars, comment))
        # Update average rating
        cur.execute("""
            UPDATE lawyers SET rating = (
                SELECT AVG(stars) FROM ratings WHERE lawyer_id=%s
            ) WHERE id=%s
        """, (lawyer_id, lawyer_id))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"message": "Rating submitted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# 6. Wallet balance check
@app.route("/api/wallet/<int:user_id>", methods=["GET"])
def wallet_balance(user_id):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT balance FROM wallets WHERE user_id=%s", (user_id,))
        balance = cur.fetchone()
        cur.close()
        conn.close()
        if balance:
            return jsonify({"user_id": user_id, "balance": float(balance[0])})
        return jsonify({"error": "Wallet not found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- Run Flask ---
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

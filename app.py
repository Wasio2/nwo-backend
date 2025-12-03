import os
import psycopg2
from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# --- Database Connection and Setup ---
# Railway automatically sets the DATABASE_URL environment variable
DATABASE_URL = os.environ.get('DATABASE_URL')

def run_sql_setup():
    """
    Connects to the database and runs initial setup commands.
    """
    conn = None
    if not DATABASE_URL:
        print("DATABASE_URL not found. Skipping database setup.")
        return

    try:
        # Use the provided database URL to connect
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()

        # 1. Create the 'lawyers' table if it doesn't exist
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lawyers (
                id SERIAL PRIMARY KEY,
                name VARCHAR(255) NOT NULL
            );
        """)
        
        # 2. Add the new columns (your requested changes)
        # We use a check to avoid errors if the columns already exist
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='lawyers' AND column_name='is_online') THEN
                    ALTER TABLE lawyers ADD COLUMN is_online BOOLEAN DEFAULT FALSE;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='lawyers' AND column_name='last_active') THEN
                    ALTER TABLE lawyers ADD COLUMN last_active TIMESTAMP;
                END IF;
            END
            $$;
        """)

        conn.commit()
        cur.close()
        print("Database setup complete: 'lawyers' table created/updated.")

    except Exception as e:
        print(f"Database setup failed: {e}")
    finally:
        if conn is not None:
            conn.close()

# Run the setup function when the application starts
run_sql_setup()

# --- API Endpoint ---
@app.route('/api/message', methods=['GET'])
def get_message():
    """
    A simple API endpoint that returns a welcome message.
    """
    data = {
        "message": "Hello from your Railway Backend! (Database Setup Attempted)",
        "status": "success",
    }
    return jsonify(data)



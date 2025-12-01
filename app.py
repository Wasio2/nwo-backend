from flask import Flask, jsonify
from flask_cors import CORS

app = Flask(_name_)
# Enable CORS for all domains to allow your Vercel frontend to access the API
CORS(app)

@app.route('/api/message', methods=['GET'])
def get_message():
    """
    A simple API endpoint that returns a welcome message.
    """
    data = {
        "message": "Hello from your Railway Backend!",
        "status": "success",
        "timestamp": "2025-12-01T12:00:00Z" # Placeholder, will be updated by the server
    }
    return jsonify(data)

if _name_ == '_main_':
    # Railway will set the PORT environment variable, so we use it here
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

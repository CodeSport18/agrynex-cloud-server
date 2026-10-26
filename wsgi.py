import logging
import os

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

from app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    # Local development only. Render runs gunicorn (see render.yaml).
    app.extensions["socketio"].run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                 debug=False, allow_unsafe_werkzeug=True)

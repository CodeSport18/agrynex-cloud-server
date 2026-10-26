import os


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY")
    MONGODB_URI = os.environ.get("MONGODB_URI")
    MONGODB_DB = os.environ.get("MONGODB_DB", "agrynex")

    # Shared secret the Raspberry Pi uses to connect.
    DEVICE_TOKEN = os.environ.get("DEVICE_TOKEN")

    # First admin account, created on startup if there are no users yet.
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    S3_BUCKET = os.environ.get("S3_BUCKET")
    # Only set when testing against an S3-compatible server such as moto.
    S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL") or None

    # App login tokens stay valid this long.
    TOKEN_TTL_DAYS = int(os.environ.get("TOKEN_TTL_DAYS", "30"))
    # How long pre-signed media links stay valid.
    MEDIA_URL_TTL_S = int(os.environ.get("MEDIA_URL_TTL_S", "3600"))

    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true"

    # Set to "false" in tests to skip the background retention job.
    RUN_BACKGROUND_JOBS = os.environ.get("RUN_BACKGROUND_JOBS", "true").lower() == "true"

    REQUIRED = ["SECRET_KEY", "MONGODB_URI", "DEVICE_TOKEN", "S3_BUCKET"]

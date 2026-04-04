"""
Multi-user auth helpers.
SECRET_KEY must be set in .env for sessions to survive server restarts.
"""
import hmac
import os
import secrets
import warnings

SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    warnings.warn("SECRET_KEY not set — sessions will expire on server restart", stacklevel=1)


def make_session_token(user_id: int) -> str:
    """Create a signed session token for the given user ID."""
    payload = str(user_id)
    sig = hmac.new(SECRET_KEY.encode(), payload.encode(), "sha256").hexdigest()
    return f"{payload}.{sig}"


def verify_session_token(token: str | None) -> int | None:
    """Verify a session token and return the user ID, or None if invalid."""
    if not token:
        return None
    try:
        payload, sig = token.rsplit(".", 1)
        expected = hmac.new(SECRET_KEY.encode(), payload.encode(), "sha256").hexdigest()
        if hmac.compare_digest(sig, expected):
            return int(payload)
    except (ValueError, AttributeError):
        pass
    return None

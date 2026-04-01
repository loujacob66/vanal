"""
Simple single-password auth for the web UI.
Set WEB_PASSWORD in .env to enable. Leave blank to disable auth entirely.
"""
import hmac
import os
import secrets

WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")

# Signing secret — regenerated each server restart (tokens expire on restart)
_SECRET = secrets.token_hex(32)


def auth_required() -> bool:
    return bool(WEB_PASSWORD)


def verify_password(password: str) -> bool:
    if not WEB_PASSWORD:
        return True
    return hmac.compare_digest(password.encode(), WEB_PASSWORD.encode())


def make_token() -> str:
    """Create a signed session token from the current password."""
    return hmac.new(_SECRET.encode(), WEB_PASSWORD.encode(), "sha256").hexdigest()


def verify_token(token: str | None) -> bool:
    if not WEB_PASSWORD:
        return True  # auth disabled — all requests allowed
    if not token:
        return False
    return hmac.compare_digest(token, make_token())

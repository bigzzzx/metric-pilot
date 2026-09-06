"""Local password authentication with opaque server-side sessions."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from backend.core import db

PBKDF2_ITERATIONS = 600_000
SESSION_COOKIE = "data_agent_session"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256": return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    connection = db()
    connection.execute("INSERT INTO auth_sessions(token,user_id,expires_at) VALUES (?,?,?)", (token, user_id, expires_at))
    connection.commit(); connection.close()
    return token


def revoke_session(token: str | None) -> None:
    if not token: return
    connection = db(); connection.execute("DELETE FROM auth_sessions WHERE token=?", (token,)); connection.commit(); connection.close()


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token: return None
    connection = db()
    row = connection.execute("""
        SELECT u.id,u.username FROM auth_sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token=? AND s.expires_at > ? AND u.is_active=1
    """, (token, datetime.now(timezone.utc).isoformat())).fetchone()
    connection.close()
    return dict(row) if row else None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user

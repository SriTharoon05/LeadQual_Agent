"""Authentication helpers and API middleware.

Tokens are signed, expire, and are verified on every protected request. Existing
plaintext admin rows remain usable only in development so installations can
migrate them to bcrypt without locking themselves out.
"""

from datetime import datetime, timedelta, timezone
import logging
import re

import bcrypt
import jwt
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

PUBLIC_PATHS = {"/auth/login", "/health", "/health/live", "/health/ready", "/openapi.json", "/docs", "/docs/oauth2-redirect"}


def create_access_token(username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + timedelta(minutes=settings.AUTH_TOKEN_TTL_MINUTES),
        "iss": "quality-lead-api",
    }
    return jwt.encode(payload, settings.AUTH_SECRET_KEY, algorithm="HS256")


def verify_access_token(token: str) -> dict:
    return jwt.decode(
        token,
        settings.AUTH_SECRET_KEY,
        algorithms=["HS256"],
        issuer="quality-lead-api",
        options={"require": ["sub", "iat", "exp", "iss"]},
    )


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8"))
        except ValueError:
            return False
    if settings.APP_ENV.lower() == "development":
        import secrets
        return secrets.compare_digest(stored, password)
    logger.warning("Rejected login for an unhashed admin password in production")
    return False


class AuthenticationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path.rstrip("/") or "/"
        if request.method == "OPTIONS" or path in PUBLIC_PATHS:
            return await call_next(request)

        # A short-lived, file-scoped ticket is verified by this route. Never
        # accept it for API/status calls or as a general authentication token.
        if (request.method == "GET" and request.query_params.get("ticket")
                and re.fullmatch(r"/jobs/[0-9a-fA-F-]{36}/exports/[0-9a-fA-F-]{36}/file", path)):
            return await call_next(request)

        value = request.headers.get("Authorization", "")
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        try:
            claims = verify_access_token(token)
            request.state.username = claims["sub"]
        except jwt.ExpiredSignatureError:
            return JSONResponse({"detail": "Session expired."}, status_code=401)
        except jwt.PyJWTError:
            return JSONResponse({"detail": "Invalid authentication token."}, status_code=401)
        return await call_next(request)

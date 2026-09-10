"""Browser sign-in using the existing owner password and expiring signed cookies."""
import base64
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

SESSION_COOKIE = "radar_session"
LOGIN_COOKIE = "radar_login"
SESSION_SECONDS = 7 * 24 * 60 * 60


def equal(a, b):
    return hmac.compare_digest(a.encode(), b.encode())


def issue_token(password, purpose, lifetime):
    payload = f"{int(time.time()) + lifetime}.{secrets.token_urlsafe(24)}"
    signature = hmac.new(password.encode(), f"radar:{purpose}:{payload}".encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def valid_token(token, password, purpose):
    try:
        expiry, nonce, signature = token.split(".")
        if not nonce or int(expiry) <= time.time():
            return False
        expected = hmac.new(password.encode(), f"radar:{purpose}:{expiry}.{nonce}".encode(), hashlib.sha256).hexdigest()
        return equal(signature, expected)
    except (ValueError, AttributeError):
        return False


def authenticated(request, password):
    if valid_token(request.cookies.get(SESSION_COOKIE, ""), password, "session"):
        return True
    try:
        scheme, value = request.headers.get("authorization", "").split(" ", 1)
        supplied = base64.b64decode(value, validate=True).decode().split(":", 1)[1]
        return scheme.lower() == "basic" and equal(supplied, password)
    except (ValueError, IndexError, UnicodeError):
        return False


def safe_next(value):
    if not value.startswith("/") or value.startswith("//") or "\\" in value or any(ord(c) < 32 for c in value):
        return "/"
    parsed = urlsplit(value)
    return value if not parsed.scheme and not parsed.netloc else "/"


def secure_cookie(request):
    return request.url.hostname not in {"localhost", "127.0.0.1", "testserver"}


def register(app, templates):
    def require_private(request):
        if request.state.demo:
            raise HTTPException(404)

    def login_page(request, next_path, error=""):
        password = os.environ.get("ADMIN_PASSWORD", "")
        token = issue_token(password, "login", 900) if password else ""
        response = templates.TemplateResponse(request, "login.html", {
            "next_path": safe_next(next_path), "csrf": token, "error": error,
            "configured": bool(password),
        })
        if password:
            response.set_cookie(LOGIN_COOKIE, token, max_age=900, httponly=True,
                                secure=secure_cookie(request), samesite="strict")
        return response

    @app.get("/login")
    def login(request: Request, next: str = "/"):
        require_private(request)
        return login_page(request, next)

    @app.post("/login")
    async def sign_in(request: Request):
        require_private(request)
        form = await request.form()
        password = os.environ.get("ADMIN_PASSWORD", "")
        csrf = str(form.get("csrf", ""))
        origin = request.headers.get("origin")
        if (not password or not valid_token(csrf, password, "login")
                or not equal(csrf, request.cookies.get(LOGIN_COOKIE, ""))
                or (origin and urlsplit(origin).netloc != request.url.netloc)):
            return login_page(request, str(form.get("next", "/")), "This sign-in page expired. Please try again.")
        if not equal(str(form.get("password", "")), password):
            return login_page(request, str(form.get("next", "/")), "That password wasn't correct. Try your Radar workspace password.")
        response = RedirectResponse(safe_next(str(form.get("next", "/"))), 303)
        response.set_cookie(SESSION_COOKIE, issue_token(password, "session", SESSION_SECONDS),
                            max_age=SESSION_SECONDS, httponly=True,
                            secure=secure_cookie(request), samesite="strict")
        response.delete_cookie(LOGIN_COOKIE, secure=secure_cookie(request), httponly=True, samesite="strict")
        return response

    @app.post("/logout")
    def logout(request: Request):
        require_private(request)
        response = RedirectResponse("/login", 303)
        response.delete_cookie(SESSION_COOKIE, secure=secure_cookie(request), httponly=True, samesite="strict")
        return response

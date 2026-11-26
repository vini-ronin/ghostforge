"""Login de operador único e credenciais distintas para API e runners."""
import hashlib
import hmac
import os
import secrets
import threading
import time
from getpass import getpass

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, TimestampSigner

router = APIRouter()
COOKIE = "ghostforge_session"
SESSION_TTL = 12 * 3600
_attempt_lock = threading.Lock()
_attempts = []


def setting(name: str) -> str:
    return os.environ.get(name, "").strip()


def production() -> bool:
    return setting("GHOSTFORGE_MODE") == "production"


def password_hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32).hex()


def valid_credentials(email: str, password: str) -> bool:
    try:
        salt, expected = setting("GHOSTFORGE_PASSWORD_HASH").split(":", 1)
        if len(salt) != 32 or len(expected) != 64:
            return False
        actual = password_hash(password, bytes.fromhex(salt))
    except ValueError:
        return False
    user_ok = hmac.compare_digest(email.strip().casefold().encode(), setting("GHOSTFORGE_EMAIL").casefold().encode())
    return bool(user_ok & hmac.compare_digest(actual, expected))


def signer() -> TimestampSigner:
    return TimestampSigner(setting("GHOSTFORGE_SESSION_SECRET"), salt="ghostforge-login-v1")


def bearer_matches(request: Request, secret: str) -> bool:
    kind, _, token = request.headers.get("authorization", "").partition(" ")
    return bool(secret) and kind.lower() == "bearer" and hmac.compare_digest(token.encode(), secret.encode())


async def guard(request: Request, call_next):
    if not production():
        return await call_next(request)
    path = request.url.path
    if path in {"/api/render_single", "/api/setup_job", "/api/process_stream"}:
        return JSONResponse({"detail": "Use /api/jobs para render via Actions."}, status_code=404)
    if path.startswith("/runner/"):
        if not bearer_matches(request, setting("GHOSTFORGE_RUNNER_TOKEN")):
            return JSONResponse({"detail": "Runner não autorizado."}, status_code=401)
        return await call_next(request)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != setting("GHOSTFORGE_PUBLIC_URL").rstrip("/"):
            return JSONResponse({"detail": "Origem inválida."}, status_code=403)
    if path in {"/login", "/healthz", "/static/ghostforge.css", "/static/logo.jpg"}:
        return await call_next(request)
    if path.startswith("/api/") and bearer_matches(request, setting("GHOSTFORGE_API_TOKEN")):
        return await call_next(request)
    try:
        member = signer().unsign(request.cookies.get(COOKIE, ""), max_age=SESSION_TTL)
        if hmac.compare_digest(member, setting("GHOSTFORGE_EMAIL").casefold().encode()):
            response = await call_next(request)
            response.headers["Cache-Control"] = "private, no-store"
            return response
    except BadSignature:
        pass
    if "/api/" in path or path.startswith(("/input/", "/output/")):
        return JSONResponse({"detail": "Entre com a sua conta."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def login_page(error: str = "", status: int = 200):
    message = f'<p class="login-error" role="alert">{error}</p>' if error else ""
    return HTMLResponse(f"""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1"><title>Entrar · GhostForge</title>
    <style>
    body{{margin:0;display:grid;place-items:center;min-height:100vh;background:#101113;color:#f5f3f2;font:16px system-ui}}
    main{{box-sizing:border-box;width:min(420px,94vw);padding:2rem;border:1px solid #32343b;border-radius:14px;background:#18191d}}
    form,label{{display:grid;gap:.55rem}} form{{gap:1rem}} h1{{font-size:1.55rem}} input,button{{font:inherit;padding:.7rem;border-radius:7px}}
    input{{color:white;background:#101113;border:1px solid #484a52}} button{{background:#ff5a36;border:0;font-weight:700;cursor:pointer}}
    .login-error{{color:#ff9d88}}
    </style></head><body><main><h1>Entrar no GhostForge</h1>{message}
    <form method="post" action="/login"><label>E-mail<input type="email" name="email" autocomplete="username" required maxlength="254"></label>
    <label>Senha<input type="password" name="password" autocomplete="current-password" required maxlength="256"></label>
    <button>Entrar</button></form></main></body></html>""", status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/login")
def login():
    return login_page()


@router.post("/login")
def authenticate(email: str = Form(max_length=254), password: str = Form(max_length=256)):
    if not production():
        return RedirectResponse("/", 303)
    with _attempt_lock:
        now = time.monotonic()
        _attempts[:] = [stamp for stamp in _attempts if now - stamp < 60]
        if len(_attempts) >= 10:
            return login_page("Muitas tentativas. Aguarde um minuto.", 429)
        _attempts.append(now)
    if not valid_credentials(email, password):
        return login_page("E-mail ou senha inválidos.", 401)
    with _attempt_lock:
        _attempts.clear()
    response = RedirectResponse("/", 303)
    response.set_cookie(COOKIE, signer().sign(setting("GHOSTFORGE_EMAIL").casefold()).decode(),
                        max_age=SESSION_TTL, httponly=True, secure=True, samesite="lax")
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", 303)
    response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="lax")
    return response


if __name__ == "__main__":
    password = getpass("Senha do GhostForge: ")
    if len(password) < 12 or password != getpass("Confirme a senha: "):
        raise SystemExit("Digite a mesma senha duas vezes, com pelo menos 12 caracteres.")
    salt = secrets.token_bytes(16)
    print(salt.hex() + ":" + password_hash(password, salt))

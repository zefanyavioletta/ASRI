"""ASRI authentication & account management.

Fitur:
- Login / logout dengan sesi server-side (cookie HttpOnly) yang tersimpan di database SQLite yang sama
  dengan data deteksi (detection_history.db).
- Dua peran: ``admin`` dan ``operator``.
    * admin    : semua fitur + Tambah Akun / Kelola Akun + hapus laporan insiden.
    * operator : semua fitur pemantauan, TANPA membuat akun dan TANPA menghapus laporan insiden.
- Password disimpan sebagai hash scrypt (tidak pernah plaintext).
- Pembatasan percobaan login (anti brute-force) per IP + username.

Pemasangan di api/main.py cukup:  ``install_auth(api)``  (sebelum mount StaticFiles).
Tidak ada dependensi baru: hanya pustaka standar + FastAPI/Pydantic yang sudah dipakai.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import app as legacy  # modul lama: dipakai hanya untuk koneksi database yang sama

SESSION_COOKIE = "asri_session"
REMEMBER_DAYS = 14            # "Ingat saya" dicentang
SHORT_SESSION_HOURS = 12      # tidak dicentang (cookie sesi browser + batas server)

# Endpoint /api/* yang boleh diakses tanpa login.
PUBLIC_API = {"/api/auth/login", "/api/auth/logout"}

USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PASSWORD_MIN, PASSWORD_MAX = 8, 128

router = APIRouter()


# --------------------------------------------------------------------------- #
# Password hashing (scrypt, stdlib)
# --------------------------------------------------------------------------- #
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode("ascii")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


# Dipakai agar waktu respons sama baik user ada maupun tidak (mengurangi user enumeration).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(12))


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def init_auth_db() -> None:
    con = legacy.get_database_connection()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin','operator')),
                created_at TEXT NOT NULL,
                created_by INTEGER,
                last_login TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON auth_sessions(user_id)")
        con.commit()

        # Seed akun admin pertama bila belum ada admin sama sekali.
        has_admin = con.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone()
        if not has_admin:
            username = os.environ.get("ASRI_ADMIN_USERNAME", "admin").strip() or "admin"
            email = os.environ.get("ASRI_ADMIN_EMAIL", "admin@asri.local").strip() or "admin@asri.local"
            password = os.environ.get("ASRI_ADMIN_PASSWORD", "")
            generated = False
            if len(password) < PASSWORD_MIN:
                password, generated = secrets.token_urlsafe(9), True
            con.execute(
                "INSERT INTO users (username,email,password_hash,role,created_at) VALUES (?,?,?,?,?)",
                (username, email, hash_password(password), "admin", _now_iso()),
            )
            con.commit()
            print("=" * 62)
            print("AKUN ADMIN ASRI DIBUAT")
            print(f"  username : {username}")
            print(f"  email    : {email}")
            if generated:
                print(f"  password : {password}   <-- catat, hanya ditampilkan sekali")
                print("  (atur sendiri lewat env ASRI_ADMIN_PASSWORD sebelum run pertama)")
            else:
                print("  password : (dari env ASRI_ADMIN_PASSWORD)")
            print("=" * 62)
    finally:
        con.close()


def _public_user(row) -> dict:
    return {"id": row[0], "username": row[1], "email": row[2], "role": row[3],
            "created_at": row[4], "last_login": row[5]}


_USER_COLS = "id, username, email, role, created_at, last_login"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def user_from_token(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    con = legacy.get_database_connection()
    try:
        row = con.execute(
            "SELECT u.id,u.username,u.email,u.role,u.created_at,u.last_login "
            "FROM auth_sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token_hash=? AND s.expires_at>?",
            (_token_hash(token), int(time.time())),
        ).fetchone()
        return _public_user(row) if row else None
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Anti brute-force (in-memory)
# --------------------------------------------------------------------------- #
_MAX_FAILS, _LOCK_SECONDS = 5, 300
_fails: dict = {}
_fails_lock = threading.Lock()


def _lock_remaining(key) -> int:
    with _fails_lock:
        entry = _fails.get(key)
        if not entry:
            return 0
        count, until = entry
        if until and until > time.time():
            return int(until - time.time()) + 1
        if until:  # kunci sudah lewat -> reset
            _fails.pop(key, None)
        return 0


def _register_fail(key) -> None:
    with _fails_lock:
        count, _ = _fails.get(key, (0, 0))
        count += 1
        _fails[key] = (count, time.time() + _LOCK_SECONDS if count >= _MAX_FAILS else 0)


def _clear_fails(key) -> None:
    with _fails_lock:
        _fails.pop(key, None)


# --------------------------------------------------------------------------- #
# Helpers & schemas
# --------------------------------------------------------------------------- #
class LoginIn(BaseModel):
    username: str = Field(..., max_length=254)
    password: str = Field(..., max_length=256)
    remember: bool = False


class NewUserIn(BaseModel):
    username: str = Field(..., max_length=64)
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=256)


class PasswordIn(BaseModel):
    password: str = Field(..., max_length=256)


class OwnPasswordIn(BaseModel):
    current_password: str = Field(..., max_length=256)
    new_password: str = Field(..., max_length=256)


class EmailIn(BaseModel):
    email: str = Field(..., max_length=254)


def current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(401, "Sesi tidak valid atau sudah berakhir. Silakan masuk kembali.")
    return user


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(403, "Hanya administrator yang dapat melakukan tindakan ini.")
    return user


def _check_password(password: str) -> None:
    if not (PASSWORD_MIN <= len(password) <= PASSWORD_MAX):
        raise HTTPException(400, f"Password harus {PASSWORD_MIN}-{PASSWORD_MAX} karakter.")


def _check_email(email: str) -> str:
    email = email.strip()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise HTTPException(400, "Format email tidak valid.")
    return email


def _operator_or_404(con, user_id: int):
    row = con.execute(f"SELECT {_USER_COLS} FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Akun tidak ditemukan.")
    if row[3] != "operator":
        raise HTTPException(403, "Hanya akun operator yang dapat dikelola dari halaman ini.")
    return row


# --------------------------------------------------------------------------- #
# Auth endpoints
# --------------------------------------------------------------------------- #
@router.post("/api/auth/login")
def login(data: LoginIn, request: Request, response: Response):
    ident = data.username.strip()
    if not ident or not data.password:
        raise HTTPException(400, "Username dan password wajib diisi.")

    ip = request.client.host if request.client else "-"
    key = (ip, ident.lower())
    wait = _lock_remaining(key)
    if wait:
        raise HTTPException(429, f"Terlalu banyak percobaan login. Coba lagi dalam {wait} detik.")

    con = legacy.get_database_connection()
    try:
        row = con.execute(
            "SELECT id,username,email,role,created_at,last_login,password_hash FROM users "
            "WHERE username=? OR email=?", (ident, ident),
        ).fetchone()
        ok = verify_password(data.password, row[6] if row else _DUMMY_HASH)
        if not row or not ok:
            _register_fail(key)
            raise HTTPException(401, "Username/email atau password salah.")
        _clear_fails(key)

        token = secrets.token_urlsafe(32)
        ttl = REMEMBER_DAYS * 86400 if data.remember else SHORT_SESSION_HOURS * 3600
        now = int(time.time())
        con.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (now,))
        con.execute("INSERT INTO auth_sessions (token_hash,user_id,created_at,expires_at) VALUES (?,?,?,?)",
                    (_token_hash(token), row[0], _now_iso(), now + ttl))
        last_login = _now_iso()
        con.execute("UPDATE users SET last_login=? WHERE id=?", (last_login, row[0]))
        con.commit()
    finally:
        con.close()

    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=ttl if data.remember else None,
        httponly=True, samesite="lax", path="/",
        secure=request.url.scheme == "https",
    )
    user = _public_user(row[:6])
    user["last_login"] = last_login
    return {"user": user}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        con = legacy.get_database_connection()
        try:
            con.execute("DELETE FROM auth_sessions WHERE token_hash=?", (_token_hash(token),))
            con.commit()
        finally:
            con.close()
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"message": "Anda telah keluar."}


@router.get("/api/auth/me")
def me(request: Request):
    return {"user": current_user(request)}


@router.patch("/api/auth/password")
def change_own_password(data: OwnPasswordIn, request: Request):
    """Ganti password akun yang sedang login (admin maupun operator) — wajib password lama."""
    user = current_user(request)
    key = ("pw", user["id"])
    wait = _lock_remaining(key)
    if wait:
        raise HTTPException(429, f"Terlalu banyak percobaan. Coba lagi dalam {wait} detik.")
    if not data.current_password:
        raise HTTPException(400, "Password lama wajib diisi.")
    _check_password(data.new_password)

    con = legacy.get_database_connection()
    try:
        row = con.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
        if not row or not verify_password(data.current_password, row[0]):
            _register_fail(key)
            raise HTTPException(400, "Password lama salah.")
        if data.new_password == data.current_password:
            raise HTTPException(400, "Password baru harus berbeda dari password lama.")
        _clear_fails(key)
        con.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(data.new_password), user["id"]))
        # Akhiri sesi lain di perangkat lain; sesi yang sedang dipakai tetap aktif.
        token = getattr(request.state, "token", None)
        if token:
            con.execute("DELETE FROM auth_sessions WHERE user_id=? AND token_hash<>?", (user["id"], _token_hash(token)))
        else:
            con.execute("DELETE FROM auth_sessions WHERE user_id=?", (user["id"],))
        con.commit()
        return {"message": "Password berhasil diganti. Sesi di perangkat lain telah diakhiri."}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Manajemen akun (khusus admin)
# --------------------------------------------------------------------------- #
@router.get("/api/users")
def list_operators(request: Request):
    require_admin(request)
    con = legacy.get_database_connection()
    try:
        rows = con.execute(f"SELECT {_USER_COLS} FROM users WHERE role='operator' ORDER BY id DESC").fetchall()
        return {"users": [_public_user(r) for r in rows]}
    finally:
        con.close()


@router.post("/api/users", status_code=201)
def create_operator(data: NewUserIn, request: Request):
    admin = require_admin(request)
    username = data.username.strip()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "Username 3-32 karakter: huruf, angka, titik, garis bawah, atau strip.")
    email = _check_email(data.email)
    _check_password(data.password)

    con = legacy.get_database_connection()
    try:
        if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
            raise HTTPException(409, "Username sudah dipakai.")
        if con.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            raise HTTPException(409, "Email sudah terdaftar.")
        try:
            cur = con.execute(
                "INSERT INTO users (username,email,password_hash,role,created_at,created_by) VALUES (?,?,?,?,?,?)",
                (username, email, hash_password(data.password), "operator", _now_iso(), admin["id"]),
            )
            con.commit()
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Username atau email sudah terdaftar.")
        row = con.execute(f"SELECT {_USER_COLS} FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"user": _public_user(row), "message": f"Akun operator {email} berhasil dibuat."}
    finally:
        con.close()


@router.patch("/api/users/{user_id}/password")
def change_operator_password(user_id: int, data: PasswordIn, request: Request):
    require_admin(request)
    _check_password(data.password)
    con = legacy.get_database_connection()
    try:
        row = _operator_or_404(con, user_id)
        con.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(data.password), user_id))
        con.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))  # paksa login ulang
        con.commit()
        return {"message": f"Password {row[2]} berhasil diganti. Sesi aktif akun tersebut diakhiri."}
    finally:
        con.close()


@router.patch("/api/users/{user_id}/email")
def change_operator_email(user_id: int, data: EmailIn, request: Request):
    require_admin(request)
    email = _check_email(data.email)
    con = legacy.get_database_connection()
    try:
        _operator_or_404(con, user_id)
        if con.execute("SELECT 1 FROM users WHERE email=? AND id<>?", (email, user_id)).fetchone():
            raise HTTPException(409, "Email sudah dipakai akun lain.")
        con.execute("UPDATE users SET email=? WHERE id=?", (email, user_id))
        con.commit()
        return {"message": f"Email akun diubah menjadi {email}."}
    finally:
        con.close()


@router.delete("/api/users/{user_id}")
def delete_operator(user_id: int, request: Request):
    require_admin(request)
    con = legacy.get_database_connection()
    try:
        row = _operator_or_404(con, user_id)
        con.execute("DELETE FROM users WHERE id=?", (user_id,))  # sesi ikut terhapus (ON DELETE CASCADE)
        con.commit()
        return {"message": f"Akun {row[2]} berhasil dihapus."}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Pemasangan ke aplikasi FastAPI
# --------------------------------------------------------------------------- #
def install_auth(app: FastAPI) -> None:
    """Pasang router + penjaga akses untuk seluruh /api/*."""
    init_auth_db()
    app.include_router(router)

    @app.middleware("http")
    async def auth_guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in PUBLIC_API and not (path.startswith("/api/cameras/") and path.endswith("/frame")):
            token = request.cookies.get(SESSION_COOKIE)
            user = await run_in_threadpool(user_from_token, token)
            if not user:
                return JSONResponse(
                    {"detail": "Sesi tidak valid atau sudah berakhir. Silakan masuk kembali."}, status_code=401)
            admin_only = path.startswith("/api/users") or (
                request.method == "DELETE" and path.startswith("/api/incidents"))
            if admin_only and user["role"] != "admin":
                return JSONResponse(
                    {"detail": "Hanya administrator yang dapat melakukan tindakan ini."}, status_code=403)
            request.state.user = user
            request.state.token = token
        return await call_next(request)

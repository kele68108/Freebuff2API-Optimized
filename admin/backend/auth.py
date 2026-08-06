import os
import json
import time
import hashlib
from pathlib import Path
from typing import Optional
import bcrypt
import jwt

# R1: data dir defaults to ~/.freebuff2api (never /root) so a non-root
# service user works. Override with FREEBUFF2API_DATA_DIR.
DATA_DIR = Path(os.getenv("FREEBUFF2API_DATA_DIR", str(Path.home() / ".freebuff2api")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DATA_FILE = DATA_DIR / "admin_data.json"

JWT_SECRET = os.getenv("FREEBUFF2API_JWT_SECRET", "")
if not JWT_SECRET:
    JWT_SECRET_FILE = DATA_DIR / ".jwt_secret"
    if JWT_SECRET_FILE.exists():
        JWT_SECRET = JWT_SECRET_FILE.read_text().strip()
    else:
        JWT_SECRET = hashlib.sha256(os.urandom(32)).hexdigest()[:32]
        JWT_SECRET_FILE.write_text(JWT_SECRET)

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 days

def _load_data() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_data(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_initialized() -> bool:
    return bool(_load_data().get("password_hash"))

def setup_password(password: str) -> str:
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    data = _load_data()
    data["password_hash"] = hashed
    _save_data(data)
    return create_token()

def verify_password(password: str) -> bool:
    data = _load_data()
    phash = data.get("password_hash", "")
    if not phash:
        return False
    return bcrypt.checkpw(password.encode(), phash.encode())

def create_token() -> str:
    expire = time.time() + ACCESS_TOKEN_EXPIRE_MINUTES * 60
    payload = {"exp": expire, "iat": time.time()}
    return jwt.encode(payload, JWT_SECRET, algorithm=ALGORITHM)

def verify_token(token: str) -> bool:
    try:
        jwt.decode(token, JWT_SECRET, algorithms=[ALGORITHM])
        return True
    except Exception:
        return False

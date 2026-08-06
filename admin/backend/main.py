from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import auth
import config_manager

# ── Config ──
ADMIN_PORT = int(os.getenv("FREEBUFF2API_ADMIN_PORT", "20003"))
ADMIN_HOST = os.getenv("FREEBUFF2API_ADMIN_HOST", "0.0.0.0")
SERVICE_NAME = os.getenv("FREEBUFF2API_SERVICE_NAME", "freebuff2api")
ENV_PATH = str(config_manager._env_path())
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
# R1: data dir defaults to ~/.freebuff2api (never /root) so a non-root
# service user works. Override with FREEBUFF2API_DATA_DIR.
DATA_DIR = Path(os.getenv("FREEBUFF2API_DATA_DIR", str(Path.home() / ".freebuff2api")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
API_KEYS_FILE = DATA_DIR / "api_keys.json"

# R8: in-memory login throttle — 5 failed attempts per 5 min window from the
# same client IP get a 429, plus a fixed 0.5 s sleep on each failure to slow
# brute force. In-memory only (single admin process is the deployment model).
_LOGIN_MAX_FAILURES = 5
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_FAIL_SLEEP_SECONDS = 0.5
_login_failures: dict[str, list[float]] = {}
_login_lock = asyncio.Lock()


def _client_ip(request: Request) -> str:
    # Only trust X-Forwarded-For when a proxy is explicitly configured;
    # otherwise an attacker reaching the admin can rotate the header to reset
    # the failure budget. Direct clients use the TCP peer address.
    if os.getenv("FREEBUFF2API_ADMIN_TRUST_PROXY", "false").lower() in ("1", "true", "yes", "on"):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _check_login_throttle(request: Request) -> None:
    """R8: reject login attempts beyond the failure budget for this client."""
    ip = _client_ip(request)
    now = time.time()
    async with _login_lock:
        recent = [t for t in _login_failures.get(ip, []) if now - t < _LOGIN_WINDOW_SECONDS]
        _login_failures[ip] = recent
        if len(recent) >= _LOGIN_MAX_FAILURES:
            raise HTTPException(
                status_code=429,
                detail="Too many failed login attempts; try again later",
            )


async def _record_login_failure(request: Request) -> None:
    ip = _client_ip(request)
    now = time.time()
    async with _login_lock:
        _login_failures.setdefault(ip, []).append(now)
        # Bound the dict IN PLACE (never reassign the module global): drop
        # entries with no failures inside the window, and evict the oldest IPs
        # if the dict ever exceeds a sane cap (memory DoS guard against
        # spoofed-client spam).
        for key in list(_login_failures):
            _login_failures[key] = [
                t for t in _login_failures[key] if now - t < _LOGIN_WINDOW_SECONDS
            ]
            if not _login_failures[key]:
                del _login_failures[key]
        if len(_login_failures) > 1000:
            oldest = sorted(
                _login_failures,
                key=lambda k: _login_failures[k][-1],
            )[:500]
            for key in oldest:
                del _login_failures[key]
    # fixed delay so an attacker cannot hammer the bcrypt check
    await asyncio.sleep(_LOGIN_FAIL_SLEEP_SECONDS)

# Frontend static dir
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# ── External IP cache ──
_external_ip: str | None = None

def _get_external_ip() -> str:
    global _external_ip
    if _external_ip:
        return _external_ip
    try:
        import urllib.request
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as resp:
            _external_ip = resp.read().decode().strip()
    except Exception:
        _external_ip = "127.0.0.1"
    return _external_ip or "127.0.0.1"

# ── API Key helpers ──

def _load_api_keys() -> list[dict[str, Any]]:
    if not API_KEYS_FILE.exists():
        return []
    try:
        with open(API_KEYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def _save_api_keys(keys: list[dict[str, Any]]) -> None:
    with open(API_KEYS_FILE, "w", encoding="utf-8") as f:
        json.dump(keys, f, ensure_ascii=False, indent=2)

app = FastAPI(title="Freebuff2API Admin", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Auth helpers ──

async def require_auth(request: Request) -> None:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not token or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")

# ── Auth routes ──

@app.post("/api/auth/status")
async def auth_status() -> dict[str, Any]:
    return {"initialized": auth.is_initialized()}

@app.post("/api/auth/setup")
async def auth_setup(request: Request) -> dict[str, Any]:
    body = await request.json()
    password = body.get("password", "")
    if not password or len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    if auth.is_initialized():
        raise HTTPException(status_code=400, detail="Already initialized")
    token = auth.setup_password(password)
    return {"token": token}

@app.post("/api/auth/login")
async def auth_login(request: Request) -> dict[str, Any]:
    await _check_login_throttle(request)
    body = await request.json()
    password = body.get("password", "")
    if not auth.verify_password(password):
        await _record_login_failure(request)
        raise HTTPException(status_code=401, detail="Invalid password")
    # successful login clears this client's failure history
    async with _login_lock:
        _login_failures.pop(_client_ip(request), None)
    return {"token": auth.create_token()}

@app.post("/api/auth/change-password")
async def auth_change_password(request: Request) -> dict[str, Any]:
    await require_auth(request)
    body = await request.json()
    old = body.get("current_password", "")
    new = body.get("new_password", "")
    if not new or len(new) < 6:
        raise HTTPException(status_code=400, detail="New password must be at least 6 characters")
    if not auth.verify_password(old):
        raise HTTPException(status_code=401, detail="Current password incorrect")
    auth.setup_password(new)
    return {"ok": True}

# ── Status ──

def _load_account_stats() -> dict[str, Any] | None:
    stats_file = DATA_DIR / "account_stats.json"
    if not stats_file.exists():
        return None
    try:
        with open(stats_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

@app.get("/api/status")
async def api_status(request: Request) -> dict[str, Any]:
    await require_auth(request)

    # systemctl status
    service_active = False
    start_ts = None
    try:
        result = subprocess.run(
            ["systemctl", "show", "--property=ActiveState,ExecMainStartTimestamp", SERVICE_NAME],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.strip().splitlines():
            if line.startswith("ActiveState="):
                service_active = line.split("=", 1)[1].strip() == "active"
            elif line.startswith("ExecMainStartTimestamp="):
                ts = line.split("=", 1)[1].strip()
                if ts and ts != "n/a":
                    start_ts = ts
    except Exception:
        pass

    # calculate uptime string
    uptime = "N/A"
    if start_ts:
        try:
            # systemd timestamp format: Mon 2026-06-22 14:30:14 CST
            import datetime
            parts = start_ts.split()
            if len(parts) >= 4:
                dt_str = f"{parts[1]} {parts[2]}"
                dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                delta = datetime.datetime.now() - dt
                days = delta.days
                hours, rem = divmod(delta.seconds, 3600)
                mins, secs = divmod(rem, 60)
                parts_u = []
                if days > 0:
                    parts_u.append(f"{days}天")
                if hours > 0:
                    parts_u.append(f"{hours}小时")
                if mins > 0:
                    parts_u.append(f"{mins}分钟")
                if not parts_u:
                    parts_u.append(f"{secs}秒")
                uptime = "".join(parts_u)
        except Exception:
            uptime = start_ts

    env = config_manager.load_env()
    accounts = config_manager.parse_accounts(env)
    port = int(env.get("FREEBUFF_PORT", "20004"))
    external_ip = _get_external_ip()
    api_address = f"http://{external_ip}:{port}/v1"

    # api keys count
    api_keys = _load_api_keys()
    enabled_keys = [k for k in api_keys if k.get("enabled", True)]

    return {
        "service": {
            "active": service_active,
            "uptime": uptime,
            "start_time": start_ts or "N/A",
            "name": SERVICE_NAME,
        },
        "env_path": ENV_PATH,
        "accounts": {
            "count": len(accounts),
        },
        "account_stats": _load_account_stats(),
        "api_keys": {
            "count": len(api_keys),
            "enabled_count": len(enabled_keys),
        },
        "config": {
            "host": env.get("FREEBUFF_HOST", "0.0.0.0"),
            "port": port,
            "debug": env.get("FREEBUFF_DEBUG", "false").lower() == "true",
            "proxy_enabled": env.get("FREEBUFF_PROXY_ENABLED", "false").lower() == "true",
            "log_level": env.get("FREEBUFF_LOG_LEVEL", "INFO"),
            "api_address": api_address,
        },
    }

# ── Config ──

@app.get("/api/config")
async def api_config(request: Request) -> dict[str, Any]:
    await require_auth(request)
    env = config_manager.load_env()
    return {"env": env}

@app.post("/api/config")
async def api_config_save(request: Request) -> dict[str, Any]:
    await require_auth(request)
    body = await request.json()
    env_data = body.get("env", {})
    # ensure all values are strings
    env_data = {k: str(v) if v is not None else "" for k, v in env_data.items()}
    config_manager.save_env(env_data)
    return {"ok": True}

# ── Accounts ──

@app.get("/api/accounts")
async def api_accounts(request: Request) -> dict[str, Any]:
    await require_auth(request)
    env = config_manager.load_env()
    return {"accounts": config_manager.parse_accounts(env)}

@app.post("/api/accounts")
async def api_accounts_save(request: Request) -> dict[str, Any]:
    await require_auth(request)
    body = await request.json()
    accounts = body.get("accounts", [])
    new_token = config_manager.build_accounts_env(accounts)
    env = config_manager.load_env()
    env["FREEBUFF_TOKEN"] = new_token
    config_manager.save_env(env)
    return {"ok": True}

@app.delete("/api/accounts/{account_id}")
async def api_account_delete(request: Request, account_id: int) -> dict[str, Any]:
    await require_auth(request)
    env = config_manager.load_env()
    accounts = config_manager.parse_accounts(env)
    accounts = [a for a in accounts if a["id"] != account_id]
    env["FREEBUFF_TOKEN"] = config_manager.build_accounts_env(accounts)
    config_manager.save_env(env)
    return {"ok": True}

@app.post("/api/accounts/test")
async def api_account_test(request: Request) -> dict[str, Any]:
    await require_auth(request)
    body = await request.json()
    token = body.get("token", "")
    if not token:
        return {"valid": False, "error": "Empty token"}

    env = config_manager.load_env()
    base_url = env.get("FREEBUFF_API_BASE_URL", "https://www.codebuff.com")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{base_url}/api/v1/freebuff/session",
                headers={"Authorization": f"Bearer {token}", "Accept": "*/*"},
            )
            if resp.status_code in (401, 403):
                return {"valid": False, "error": f"Token rejected (HTTP {resp.status_code})"}
            if resp.status_code >= 400:
                return {"valid": False, "error": f"Upstream error (HTTP {resp.status_code})"}
            data = resp.json()
            return {"valid": True, "status": data.get("status"), "model": data.get("model")}
    except httpx.TimeoutException:
        return {"valid": False, "error": "Timeout connecting to upstream"}
    except Exception as e:
        return {"valid": False, "error": str(e)}

# ── Service control ──

@app.post("/api/control/{action}")
async def api_control(request: Request, action: str) -> dict[str, Any]:
    await require_auth(request)
    if action not in ("start", "stop", "restart"):
        raise HTTPException(status_code=400, detail="Invalid action")
    try:
        result = subprocess.run(
            ["systemctl", action, SERVICE_NAME],
            capture_output=True, text=True, timeout=30
        )
        ok = result.returncode == 0
        return {"ok": ok, "error": result.stderr if not ok else None}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ── Logs (SSE) ──

async def journalctl_lines() -> AsyncIterator[str]:
    proc = await asyncio.create_subprocess_exec(
        "journalctl", "-u", SERVICE_NAME, "-f", "-n", "200", "--no-pager",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            yield line.decode("utf-8", errors="replace")
    finally:
        proc.kill()
        await proc.wait()

@app.get("/api/logs/sse")
async def api_logs_sse(request: Request) -> StreamingResponse:
    token = request.query_params.get("token", "")
    if not token or not auth.verify_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    async def event_stream() -> AsyncIterator[bytes]:
        async for line in journalctl_lines():
            yield f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n".encode("utf-8")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )

# ── API Keys ──

import uuid

@app.get("/api/keys")
async def api_keys_list(request: Request) -> dict[str, Any]:
    await require_auth(request)
    keys = _load_api_keys()
    result = []
    for k in keys:
        result.append({
            "id": k.get("id"),
            "key": k.get("key", ""),
            "enabled": k.get("enabled", True),
            "label": k.get("label", ""),
            "created_at": k.get("created_at", ""),
        })
    return {"keys": result}

@app.post("/api/keys")
async def api_keys_create(request: Request) -> dict[str, Any]:
    await require_auth(request)
    body = await request.json()
    label = body.get("label", "")
    keys = _load_api_keys()
    new_key = f"sk-fb-{uuid.uuid4().hex[:24]}"
    keys.append({
        "id": str(uuid.uuid4()),
        "key": new_key,
        "enabled": True,
        "label": label,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    _save_api_keys(keys)
    return {"ok": True, "key": new_key}

@app.delete("/api/keys/{key_id}")
async def api_keys_delete(request: Request, key_id: str) -> dict[str, Any]:
    await require_auth(request)
    keys = _load_api_keys()
    keys = [k for k in keys if k.get("id") != key_id]
    _save_api_keys(keys)
    return {"ok": True}

@app.patch("/api/keys/{key_id}")
async def api_keys_toggle(request: Request, key_id: str) -> dict[str, Any]:
    await require_auth(request)
    body = await request.json()
    keys = _load_api_keys()
    for k in keys:
        if k.get("id") == key_id:
            if "enabled" in body:
                k["enabled"] = body.get("enabled", not k.get("enabled", True))
            if "label" in body:
                k["label"] = body["label"]
            break
    _save_api_keys(keys)
    return {"ok": True}

# ── Test proxy ──

@app.post("/api/test/chat")
async def test_chat(request: Request) -> dict[str, Any]:
    await require_auth(request)
    body = await request.json()
    env = config_manager.load_env()
    port = int(env.get("FREEBUFF_PORT", "20004"))
    # use first enabled key from api_keys.json
    keys = _load_api_keys()
    api_key = ""
    for k in keys:
        if k.get("enabled", True):
            api_key = k.get("key", "")
            break
    url = f"http://127.0.0.1:{port}/v1/chat/completions"

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(url, json={**body, "stream": False}, headers=headers)
            if resp.status_code >= 400:
                return {"error": f"HTTP {resp.status_code}: {resp.text[:500]}"}
            return resp.json()
    except httpx.TimeoutException:
        return {"error": "Timeout connecting to main service"}
    except Exception as e:
        return {"error": str(e)}

# ── Proxy test ──

@app.post("/api/test/proxy")
async def test_proxy(request: Request) -> dict[str, Any]:
    await require_auth(request)
    body = await request.json()
    proxy_url = body.get("proxy_url", "")
    env = config_manager.load_env()
    upstream = env.get("FREEBUFF_API_BASE_URL", "https://www.codebuff.com")
    target = f"{upstream}/api/healthz"

    if not proxy_url:
        return {"ok": False, "error": "代理地址为空", "latency_ms": None}

    try:
        start = time.time()
        async with httpx.AsyncClient(proxy=proxy_url, timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(target)
        latency_ms = round((time.time() - start) * 1000, 1)
        if resp.status_code < 500:
            return {
                "ok": True,
                "status_code": resp.status_code,
                "latency_ms": latency_ms,
                "target": target,
            }
        return {
            "ok": False,
            "error": f"HTTP {resp.status_code}",
            "latency_ms": latency_ms,
            "target": target,
        }
    except httpx.TimeoutException:
        return {"ok": False, "error": "连接超时", "latency_ms": None, "target": target}
    except Exception as e:
        return {"ok": False, "error": str(e), "latency_ms": None, "target": target}

# ── Static files ──

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")

@app.get("/{path:path}")
async def static_files(path: str) -> FileResponse:
    target = FRONTEND_DIR / path
    if target.exists() and target.is_file():
        return FileResponse(target)
    # fallback to index.html for SPA routing
    return FileResponse(FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=ADMIN_HOST, port=ADMIN_PORT)

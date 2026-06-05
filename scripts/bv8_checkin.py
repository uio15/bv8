#!/usr/bin/env python3
"""
BV8 / TAX API daily check-in script.

Primary auth flow:
  1. POST /api/user/login?turnstile= with username/password
  2. Keep Set-Cookie in requests.Session
  3. POST /api/user/checkin

Required GitHub Actions secrets for normal use:
  BV8_USERNAME
  BV8_PASSWORD

Optional environment variables:
  BV8_BASE_URL          default: https://api.bv8.my
  BV8_QUOTA_PER_DOLLAR  default: 500000, used only for display
  BV8_TURNSTILE_TOKEN   only needed if the site enables Turnstile later
  BV8_TOTP_SECRET       optional TOTP base32 secret if your account requires 2FA
  BV8_2FA_CODE          one-time 2FA/backup code, mainly for manual runs
  BV8_COOKIE            fallback only; not recommended for long-term scheduling
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import struct
import sys
import time
from typing import Any

import requests


BASE_URL = os.getenv("BV8_BASE_URL", "https://api.bv8.my").rstrip("/")
USERNAME = os.getenv("BV8_USERNAME", "").strip()
PASSWORD = os.getenv("BV8_PASSWORD", "").strip()
COOKIE = os.getenv("BV8_COOKIE", "").strip()
TURNSTILE_TOKEN = os.getenv("BV8_TURNSTILE_TOKEN", "").strip()
TOTP_SECRET = os.getenv("BV8_TOTP_SECRET", "").strip()
TWO_FA_CODE = os.getenv("BV8_2FA_CODE", "").strip()
QUOTA_PER_DOLLAR = int(os.getenv("BV8_QUOTA_PER_DOLLAR", "500000"))


def _pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


def _request_json(session: requests.Session, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    url = f"{BASE_URL}{path}"
    resp = session.request(method, url, timeout=30, **kwargs)
    text = resp.text
    try:
        data = resp.json()
    except Exception:
        data = {"raw": text[:1000]}

    if resp.status_code >= 400:
        print(f"HTTP {resp.status_code} {method} {url}", file=sys.stderr)
        print(_pretty(data), file=sys.stderr)
        resp.raise_for_status()

    return data


def _totp_now(secret: str, digits: int = 6, period: int = 30) -> str:
    """Generate RFC 6238 TOTP code without external dependencies."""
    normalized = secret.replace(" ", "").replace("-", "").upper()
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(normalized + padding)
    counter = int(time.time() // period)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)


def _login(session: requests.Session) -> None:
    if USERNAME and PASSWORD:
        login_path = f"/api/user/login?turnstile={TURNSTILE_TOKEN}"
        result = _request_json(session, "POST", login_path, json={"username": USERNAME, "password": PASSWORD})
        print("Login response:")
        print(_pretty({k: v for k, v in result.items() if k != "data"}))

        if not result.get("success"):
            raise RuntimeError(f"login failed: {result.get('message') or result}")

        data = result.get("data") or {}
        if isinstance(data, dict) and data.get("require_2fa"):
            code = TWO_FA_CODE or (_totp_now(TOTP_SECRET) if TOTP_SECRET else "")
            if not code:
                raise RuntimeError("account requires 2FA; set BV8_TOTP_SECRET or BV8_2FA_CODE")
            result_2fa = _request_json(session, "POST", "/api/user/login/2fa", json={"code": code})
            print("2FA response:")
            print(_pretty({k: v for k, v in result_2fa.items() if k != "data"}))
            if not result_2fa.get("success"):
                raise RuntimeError(f"2FA failed: {result_2fa.get('message') or result_2fa}")

        # Cheap verification. Do not dump user data to logs.
        try:
            me = _request_json(session, "GET", "/api/user/self")
            print(f"Self check success={bool(me.get('success'))}")
        except Exception as exc:
            print(f"WARN: self check failed after login: {exc}", file=sys.stderr)
        return

    if COOKIE:
        # Fallback mode only. Prefer username/password because cookies expire.
        session.headers.update({"Cookie": COOKIE})
        print("Using BV8_COOKIE fallback auth. Prefer BV8_USERNAME/BV8_PASSWORD for scheduled runs.")
        return

    print("ERROR: missing BV8_USERNAME/BV8_PASSWORD.", file=sys.stderr)
    print("Set GitHub Actions secrets BV8_USERNAME and BV8_PASSWORD.", file=sys.stderr)
    raise SystemExit(2)


def main() -> int:
    session = requests.Session()
    session.headers.update(
        {
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/console/personal",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0 Safari/537.36"
            ),
        }
    )

    _login(session)

    # Captured from browser: POST /api/user/checkin, no request body.
    result = _request_json(session, "POST", "/api/user/checkin")
    print("Check-in response:")
    print(_pretty(result))

    success = bool(result.get("success"))
    message = str(result.get("message", ""))
    quota_awarded = None
    if isinstance(result.get("data"), dict):
        quota_awarded = result["data"].get("quota_awarded")

    if quota_awarded is not None:
        try:
            usd = float(quota_awarded) / QUOTA_PER_DOLLAR
            print(f"Awarded quota: {quota_awarded} ≈ ${usd:.2f}")
        except Exception:
            print(f"Awarded quota: {quota_awarded}")

    # Optional confirmation request captured from browser:
    # GET /api/user/checkin?month=YYYY-MM
    month = _dt.date.today().strftime("%Y-%m")
    try:
        stats = _request_json(session, "GET", f"/api/user/checkin?month={month}")
        checked_today = (
            isinstance(stats.get("data"), dict)
            and isinstance(stats["data"].get("stats"), dict)
            and stats["data"]["stats"].get("checked_in_today")
        )
        print(f"checked_in_today={checked_today}")
    except Exception as exc:
        print(f"WARN: confirmation request failed: {exc}", file=sys.stderr)

    # Treat normal successful check-in or already-checked-in style messages as success.
    if success or "已" in message or "成功" in message:
        return 0

    print("ERROR: check-in did not report success.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
EQT Protocol MT5 Validation Service (FREE)

A tiny always-on HTTP service for the Windows VPS. Laravel calls it when a user
submits their MT5 credentials on the "Connect MT5" page. It performs a REAL
broker login with the official (free) MetaTrader5 Python package and returns the
account equity/balance so Laravel can verify the account and enforce the minimum
capital rule — no MetaApi subscription required.

Auth (must match Laravel admin settings / .env):
  Authorization: Bearer <agent_token>
  X-Arbitrage-Signature: HMAC-SHA256(raw_body, hmac_secret) hex

Run:
  python validator_server.py

Endpoint:
  POST /validate
  { "broker": "...", "server": "...", "login": "123456", "password": "..." }
  -> { "success": true, "equity": 2500.0, "balance": 2500.0, "message": "..." }

IMPORTANT: MT5 uses a single terminal session. Logging into a user account will
disconnect any other logged-in account. To avoid clashing with the scheduled
master sync/weekly jobs, run this validator against a SEPARATE MetaTrader 5
terminal installation (set "validator_mt5_terminal_path" in config.json). A lock
serializes concurrent validation requests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
import threading
from pathlib import Path

import MetaTrader5 as mt5
from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("validator")

CONFIG_PATH = Path(__file__).parent / "config.json"

app = Flask(__name__)
_mt5_lock = threading.Lock()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.error("config.json not found. Copy config.example.json to config.json")
        sys.exit(1)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()
AGENT_TOKEN = CONFIG.get("agent_token", "")
HMAC_SECRET = CONFIG.get("hmac_secret", "")
# Prefer a dedicated terminal for validation so we don't disrupt master jobs.
VALIDATOR_TERMINAL_PATH = (
    CONFIG.get("validator_mt5_terminal_path") or CONFIG.get("mt5_terminal_path")
)


def _authorized(raw_body: bytes) -> bool:
    if not AGENT_TOKEN or not HMAC_SECRET:
        logger.error("agent_token / hmac_secret not configured in config.json")
        return False

    bearer = request.headers.get("Authorization", "")
    if bearer != f"Bearer {AGENT_TOKEN}":
        return False

    signature = request.headers.get("X-Arbitrage-Signature", "")
    if not signature:
        return False

    expected = hmac.new(HMAC_SECRET.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _validate_credentials(login: str, password: str, server: str) -> dict:
    kwargs = {}
    if VALIDATOR_TERMINAL_PATH:
        kwargs["path"] = VALIDATOR_TERMINAL_PATH

    if not mt5.initialize(**kwargs):
        err = mt5.last_error()
        logger.error("MT5 initialize failed: %s", err)
        return {"success": False, "message": "MT5 terminal could not be initialized on the server."}

    try:
        ok = mt5.login(int(login), password=password, server=server)
        if not ok:
            code, desc = mt5.last_error()
            logger.info("Login failed for %s@%s: (%s) %s", login, server, code, desc)
            return {
                "success": False,
                "message": "Invalid MT5 credentials or server. Please check login, password and server name.",
            }

        info = mt5.account_info()
        if not info:
            return {"success": False, "message": "Connected but could not read account information."}

        return {
            "success": True,
            "equity": round(float(info.equity), 2),
            "balance": round(float(info.balance), 2),
            "message": "Connection verified successfully.",
        }
    except ValueError:
        return {"success": False, "message": "Login must be a numeric MT5 account number."}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Validation error: %s", exc)
        return {"success": False, "message": "Unexpected error while validating the account."}
    finally:
        mt5.shutdown()


@app.post("/validate")
def validate():
    raw_body = request.get_data() or b""

    if not _authorized(raw_body):
        return jsonify({"success": False, "message": "Unauthorized."}), 401

    try:
        data = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return jsonify({"success": False, "message": "Invalid JSON body."}), 400

    login = str(data.get("login", "")).strip()
    password = str(data.get("password", ""))
    server = str(data.get("server", "")).strip()

    if not login or not password or not server:
        return jsonify({"success": False, "message": "login, password and server are required."}), 422

    # MT5 is single-session; serialize validations.
    with _mt5_lock:
        result = _validate_credentials(login, password, server)

    status = 200 if result.get("success") else 200  # business errors still return 200 with success=false
    return jsonify(result), status


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    host = CONFIG.get("validator_host", "0.0.0.0")
    port = int(CONFIG.get("validator_port", 8787))
    logger.info("Starting MT5 validation service on %s:%s", host, port)
    app.run(host=host, port=port)

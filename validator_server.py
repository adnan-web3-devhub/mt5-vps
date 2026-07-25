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

The terminal is a GUI application: it must run in an interactive desktop session
(a logged-in user, e.g. via autologon + Task Scheduler "run only when user is
logged on"). Started from a Windows service in session 0 it never finishes
booting and every mt5.initialize() call ends in (-10005, 'IPC timeout').
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import MetaTrader5 as mt5
import win_diagnostics
from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("validator")

# MT5_VALIDATOR_CONFIG lets tests and second instances point elsewhere, so nothing
# ever has to write over the real config.json.
CONFIG_PATH = Path(os.environ.get("MT5_VALIDATOR_CONFIG") or Path(__file__).parent / "config.json")

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

# Laravel gives up after 60s, so the whole retry budget has to fit well below that:
# warmup + (attempts x init_timeout) + delays.
INIT_ATTEMPTS = max(1, int(CONFIG.get("validator_init_attempts", 2)))
INIT_TIMEOUT_MS = max(5_000, int(CONFIG.get("validator_init_timeout_ms", 15_000)))
INIT_RETRY_DELAY = float(CONFIG.get("validator_init_retry_seconds", 2))
TERMINAL_WARMUP_SECONDS = float(CONFIG.get("validator_terminal_warmup_seconds", 10))
TERMINAL_RELAUNCH_COOLDOWN = 60.0

IPC_TIMEOUT = -10005
AUTH_FAILED = -6

_terminal_proc: subprocess.Popen | None = None
_terminal_launched_at: float | None = None


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


def _package_version() -> str | None:
    return getattr(mt5, "__version__", None)


def _ensure_terminal_running() -> None:
    """Start the validator terminal ourselves so initialize() only has to attach.

    Letting mt5.initialize() cold-start the terminal is the usual source of
    'IPC timeout': the call has to boot a GUI app, let it reconnect to the broker
    and answer on the named pipe, all inside one timeout window.
    """
    global _terminal_proc, _terminal_launched_at

    if not VALIDATOR_TERMINAL_PATH:
        return
    if _terminal_proc is not None and _terminal_proc.poll() is None:
        return

    now = time.monotonic()
    if _terminal_launched_at is not None and now - _terminal_launched_at < TERMINAL_RELAUNCH_COOLDOWN:
        # A terminal for this installation is already running (our Popen handle
        # exits immediately when MT5 hands over to the existing instance).
        return

    exe = Path(VALIDATOR_TERMINAL_PATH)
    if not exe.is_file():
        logger.error("Validator terminal not found at %s - check validator_mt5_terminal_path", exe)
        return

    try:
        _terminal_proc = subprocess.Popen(
            [str(exe)],
            cwd=str(exe.parent),
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
    except OSError as exc:
        logger.error("Could not launch MT5 terminal %s: %s", exe, exc)
        return

    _terminal_launched_at = now
    logger.info("Launched MT5 terminal %s, waiting %.0fs for it to boot", exe, TERMINAL_WARMUP_SECONDS)
    time.sleep(TERMINAL_WARMUP_SECONDS)


def _initialize_terminal(
    login: int | None = None, password: str | None = None, server: str | None = None
) -> tuple[bool, tuple]:
    """Connect to the validator terminal, logging in as part of the same call.

    Passing the credentials to initialize() rather than calling login() afterwards
    is what makes this work on a terminal with no saved account: without them the
    terminal never completes its connection and the handshake ends in IPC timeout.
    """
    kwargs: dict = {"timeout": INIT_TIMEOUT_MS}
    if VALIDATOR_TERMINAL_PATH:
        kwargs["path"] = VALIDATOR_TERMINAL_PATH
    if login is not None:
        kwargs.update(login=login, password=password, server=server)

    last_error: tuple = (0, "unknown")
    for attempt in range(1, INIT_ATTEMPTS + 1):
        _ensure_terminal_running()

        if mt5.initialize(**kwargs):
            info = mt5.terminal_info()
            if info is not None:
                logger.info(
                    "MT5 connected (build %s, connected=%s, path=%s)",
                    getattr(info, "build", "?"),
                    getattr(info, "connected", "?"),
                    getattr(info, "path", "?"),
                )
            return True, (0, "")

        last_error = mt5.last_error()
        logger.error(
            "MT5 initialize attempt %s/%s failed: %s", attempt, INIT_ATTEMPTS, last_error
        )
        mt5.shutdown()

        # Bad credentials will never succeed on retry, and the user is waiting.
        if _error_code(last_error) == AUTH_FAILED:
            break
        if attempt < INIT_ATTEMPTS:
            time.sleep(INIT_RETRY_DELAY)

    return False, last_error


def _error_code(err) -> int:
    return err[0] if isinstance(err, (tuple, list)) and err else 0


def _initialize_failure_message(code: int) -> str:
    if code == IPC_TIMEOUT:
        return (
            "The MT5 terminal on the validation server is not responding. "
            "Please try again in a minute or contact support."
        )
    return "MT5 terminal could not be initialized on the server."


INVALID_CREDENTIALS_MESSAGE = (
    "Invalid MT5 credentials or server. Please check login, password and server name."
)


def _validate_credentials(login: str, password: str, server: str) -> dict:
    try:
        login_id = int(login)
    except ValueError:
        return {"success": False, "message": "Login must be a numeric MT5 account number."}

    ok, err = _initialize_terminal(login_id, password, server)
    if not ok:
        code = _error_code(err)
        if code == AUTH_FAILED:
            logger.info("Login rejected for %s@%s: %s", login_id, server, err)
            return {"success": False, "message": INVALID_CREDENTIALS_MESSAGE, "error_code": code}
        return {
            "success": False,
            "message": _initialize_failure_message(code),
            "error_code": code,
            "error_detail": str(err),
        }

    try:
        info = mt5.account_info()

        # The terminal is shared, so make sure we are reading the account we were
        # asked about and not whichever one happened to be logged in already.
        if info is None or int(info.login) != login_id:
            if not mt5.login(login_id, password=password, server=server):
                err = mt5.last_error()
                logger.info("Login failed for %s@%s: %s", login_id, server, err)
                return {
                    "success": False,
                    "message": INVALID_CREDENTIALS_MESSAGE,
                    "error_code": _error_code(err),
                }
            info = mt5.account_info()

        if info is None:
            return {"success": False, "message": "Connected but could not read account information."}
        if int(info.login) != login_id:
            logger.error("Terminal reported account %s while validating %s", info.login, login_id)
            return {
                "success": False,
                "message": "Could not verify this account on the server. Please try again.",
            }

        return {
            "success": True,
            "equity": round(float(info.equity), 2),
            "balance": round(float(info.balance), 2),
            "message": "Connection verified successfully.",
        }
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


@app.get("/diagnose")
def diagnose():
    """Local-only readiness check for the terminal, without needing credentials.

    Run on the VPS: curl http://127.0.0.1:8787/diagnose

    Note it deliberately does not call initialize(): a terminal with no saved
    account only completes the handshake when credentials are supplied, so a
    credential-less attempt would fail here even when validation works fine.
    Use probe_initialize.py when you need to test the call itself.
    """
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"success": False, "message": "Diagnostics are available from localhost only."}), 403

    _ensure_terminal_running()

    report = {
        "python_64bit": sys.maxsize > 2**32,
        "mt5_package_version": _package_version(),
        "terminal_path_exists": bool(VALIDATOR_TERMINAL_PATH) and Path(VALIDATOR_TERMINAL_PATH).is_file(),
        **win_diagnostics.report(VALIDATOR_TERMINAL_PATH),
    }
    report["terminal_ready"] = any(
        probe.get("opened") or probe.get("exists_but_busy")
        for probe in report.get("mt5_pipe_probes", [])
    )
    if not report["terminal_ready"]:
        report["hints"] = win_diagnostics.ipc_timeout_hints(
            VALIDATOR_TERMINAL_PATH, _package_version()
        )

    return jsonify(report)


PLACEHOLDER_SECRETS = {"t", "s", "your-agent-token", "your-hmac-secret", "changeme"}
MIN_SECRET_LENGTH = 16


def _check_credentials_configured() -> None:
    """Catch a placeholder config here, not as unexplained 401s from Laravel."""
    for name, value in (("agent_token", AGENT_TOKEN), ("hmac_secret", HMAC_SECRET)):
        if not value:
            logger.error("%s is missing from %s. Every request will be rejected.", name, CONFIG_PATH)
        elif value.lower() in PLACEHOLDER_SECRETS or len(value) < MIN_SECRET_LENGTH:
            logger.error(
                "%s in %s looks like a placeholder (%d characters). It must match the value in "
                "Laravel (Admin > Site Settings > Arbitrage), otherwise Laravel gets 401 on every "
                "validation.",
                name,
                CONFIG_PATH,
                len(value),
            )


def _startup_checks() -> None:
    _check_credentials_configured()

    if sys.maxsize <= 2**32:
        logger.error(
            "This is 32-bit Python. The MetaTrader5 package needs 64-bit Python to talk to "
            "the 64-bit terminal; initialize() will always fail with IPC timeout."
        )

    if not VALIDATOR_TERMINAL_PATH:
        logger.warning(
            "No validator_mt5_terminal_path / mt5_terminal_path in config.json; "
            "MT5 will guess which terminal to use."
        )
    elif not Path(VALIDATOR_TERMINAL_PATH).is_file():
        logger.error("Configured terminal does not exist: %s", VALIDATOR_TERMINAL_PATH)

    session = win_diagnostics.session_info()
    logger.info(
        "Running as %s in Windows session %s (elevated=%s)",
        session.get("user"),
        session.get("session_id"),
        session.get("elevated"),
    )
    if session.get("session_id") == 0:
        logger.error(
            "Session 0 has no desktop, so terminal64.exe cannot finish starting and every "
            "initialize() will time out. Run the validator from an interactive login instead "
            "of as a plain Windows service."
        )

    # Boot the terminal now so the first user validation isn't the one paying for it.
    threading.Thread(target=_warm_up, name="mt5-warmup", daemon=True).start()


def _warm_up() -> None:
    """Start the terminal and confirm it is reachable.

    Readiness is checked via the terminal's named pipe rather than initialize(),
    because a validator terminal has no saved account and only completes the
    handshake once a user's credentials are supplied.
    """
    with _mt5_lock:
        _ensure_terminal_running()
        try:
            probes = [win_diagnostics.probe_pipe(p) for p in win_diagnostics.mt5_pipes()]
        except win_diagnostics.DiagnosticsError as exc:
            logger.warning("Could not check terminal readiness: %s", exc)
            return

    if any(p.get("opened") or p.get("exists_but_busy") for p in probes):
        logger.info("Warm-up successful: the MT5 terminal is running and reachable.")
        return

    logger.error("Warm-up failed: the MT5 terminal is not reachable.")
    for hint in win_diagnostics.ipc_timeout_hints(VALIDATOR_TERMINAL_PATH, _package_version()):
        logger.error("  - %s", hint)


if __name__ == "__main__":
    host = CONFIG.get("validator_host", "0.0.0.0")
    port = int(CONFIG.get("validator_port", 8787))
    logger.info("Starting MT5 validation service on %s:%s", host, port)
    _startup_checks()
    app.run(host=host, port=port)

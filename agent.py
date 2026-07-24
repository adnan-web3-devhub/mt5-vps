#!/usr/bin/env python3
"""
EQT Protocol MT5 VPS Agent
Run on Windows VPS with MetaTrader 5 terminal installed.

Usage:
  python agent.py sync          # daily equity/balance sync
  python agent.py weekly        # weekly report push
  python agent.py weekly --days 7
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from mt5_reader import (
    compute_weekly_breakdown,
    fetch_account_snapshot,
    initialize_mt5,
    login_account,
    shutdown_mt5,
)
from pusher import LaravelPusher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("agent")

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.error("config.json not found. Copy config.example.json to config.json")
        sys.exit(1)
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_accounts(config: dict, pusher: LaravelPusher) -> list[dict]:
    if config.get("use_laravel_accounts", True):
        return pusher.fetch_master_accounts()
    return config.get("local_accounts", [])


def process_account(account: dict, config: dict, mode: str, week_start=None, week_end=None) -> bool:
    account_id = account.get("id")
    login = account.get("login")
    password = account.get("master_password")
    server = account.get("server")

    if not all([login, password, server]):
        logger.error("Account %s missing login/password/server", account_id or login)
        return False

    if not login_account(login, password, server):
        return False

    pusher = LaravelPusher(
        config["laravel_api_url"],
        config["agent_token"],
        config["hmac_secret"],
    )

    try:
        if mode == "sync":
            snap = fetch_account_snapshot()
            if account_id:
                pusher.push_sync(account_id, snap["equity"], snap["balance"])
            logger.info("Synced account %s: equity=%s balance=%s", login, snap["equity"], snap["balance"])
        elif mode == "weekly":
            keywords = config.get("commission_comment_keywords", [])
            breakdown = compute_weekly_breakdown(week_start, week_end, keywords)
            payload = {
                "master_account_id": account_id,
                "week_start": week_start.strftime("%Y-%m-%d"),
                "week_end": week_end.strftime("%Y-%m-%d"),
                "currency": account.get("currency", "USD"),
                **breakdown,
            }
            result = pusher.push_weekly_report(payload)
            logger.info(
                "Pushed weekly report for account %s: pool=%s report_id=%s",
                login,
                result.get("distributable_pool"),
                result.get("report_id"),
            )
        return True
    except Exception as exc:
        logger.exception("Failed processing account %s: %s", login, exc)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="EQT MT5 VPS Agent")
    parser.add_argument("mode", choices=["sync", "weekly"], help="Run mode")
    parser.add_argument("--days", type=int, default=7, help="Weekly lookback days")
    args = parser.parse_args()

    config = load_config()
    pusher = LaravelPusher(
        config["laravel_api_url"],
        config["agent_token"],
        config["hmac_secret"],
    )

    if not initialize_mt5(config.get("mt5_terminal_path")):
        sys.exit(1)

    week_end = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = week_end - timedelta(days=args.days)

    accounts = get_accounts(config, pusher)
    if not accounts:
        logger.warning("No master accounts to process")
        shutdown_mt5()
        sys.exit(0)

    success = 0
    failed = 0

    for account in accounts:
        try:
            if process_account(account, config, args.mode, week_start, week_end):
                success += 1
            else:
                failed += 1
        except Exception:
            failed += 1
        finally:
            # ensure next account gets a clean session
            pass

    shutdown_mt5()
    logger.info("Done. success=%d failed=%d", success, failed)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()

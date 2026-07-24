"""Read MT5 deal history and compute weekly breakdown."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import MetaTrader5 as mt5

logger = logging.getLogger(__name__)

# MT5 deal types (subset)
DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1
DEAL_TYPE_BALANCE = 2
DEAL_TYPE_CREDIT = 3
DEAL_TYPE_CHARGE = 4
DEAL_TYPE_CORRECTION = 5
DEAL_TYPE_BONUS = 6
DEAL_TYPE_COMMISSION = 7
DEAL_TYPE_COMMISSION_DAILY = 8
DEAL_TYPE_COMMISSION_MONTHLY = 9
DEAL_TYPE_COMMISSION_AGENT_DAILY = 10
DEAL_TYPE_COMMISSION_AGENT_MONTHLY = 11
DEAL_TYPE_INTEREST = 12

TRADE_DEAL_TYPES = {
    DEAL_TYPE_BUY,
    DEAL_TYPE_SELL,
}


def initialize_mt5(terminal_path: str | None = None) -> bool:
    kwargs = {}
    if terminal_path:
        kwargs["path"] = terminal_path
    if not mt5.initialize(**kwargs):
        logger.error("MT5 initialize failed: %s", mt5.last_error())
        return False
    return True


def shutdown_mt5() -> None:
    mt5.shutdown()


def login_account(login: int | str, password: str, server: str) -> bool:
    ok = mt5.login(int(login), password=password, server=server)
    if not ok:
        logger.error("MT5 login failed for %s@%s: %s", login, server, mt5.last_error())
    return ok


def _comment_matches(comment: str, keywords: list[str]) -> bool:
    lower = (comment or "").lower()
    return any(kw.lower() in lower for kw in keywords if kw)


def compute_weekly_breakdown(
    week_start: datetime,
    week_end: datetime,
    commission_keywords: list[str],
) -> dict[str, Any]:
    deals = mt5.history_deals_get(week_start, week_end)
    if deals is None:
        err = mt5.last_error()
        raise RuntimeError(f"history_deals_get failed: {err}")

    gross_profit = 0.0
    deal_commission = 0.0
    swap = 0.0
    deposits = 0.0
    withdrawals = 0.0
    broker_commission_pool = 0.0
    raw_deals: list[dict[str, Any]] = []

    for deal in deals:
        deal_type = deal.type
        profit = float(deal.profit or 0)
        commission = float(deal.commission or 0)
        storage = float(deal.swap or 0)
        comment = deal.comment or ""

        raw_deals.append(
            {
                "ticket": deal.ticket,
                "type": deal_type,
                "profit": profit,
                "commission": commission,
                "swap": storage,
                "comment": comment,
                "time": deal.time,
            }
        )

        if deal_type in TRADE_DEAL_TYPES:
            gross_profit += profit
            deal_commission += commission
            swap += storage
        elif deal_type in (DEAL_TYPE_BALANCE, DEAL_TYPE_CREDIT, DEAL_TYPE_CORRECTION, DEAL_TYPE_BONUS):
            if profit >= 0:
                deposits += profit
            else:
                withdrawals += abs(profit)
            if _comment_matches(comment, commission_keywords):
                broker_commission_pool += abs(profit)
        elif deal_type in (
            DEAL_TYPE_COMMISSION,
            DEAL_TYPE_COMMISSION_DAILY,
            DEAL_TYPE_COMMISSION_MONTHLY,
            DEAL_TYPE_COMMISSION_AGENT_DAILY,
            DEAL_TYPE_COMMISSION_AGENT_MONTHLY,
            DEAL_TYPE_CHARGE,
        ):
            deal_commission += profit
            if _comment_matches(comment, commission_keywords):
                broker_commission_pool += abs(profit)

    # net = gross + commission + swap (costs are typically negative; losses stay negative)
    net_profit_platform = gross_profit + deal_commission + swap

    info = mt5.account_info()
    equity = float(info.equity) if info else 0.0
    balance = float(info.balance) if info else 0.0

    return {
        "gross_profit": round(gross_profit, 2),
        "deal_commission": round(deal_commission, 2),
        "swap": round(swap, 2),
        "deposits": round(deposits, 2),
        "withdrawals": round(withdrawals, 2),
        "broker_commission_pool": round(broker_commission_pool, 2),
        "net_profit_platform": round(net_profit_platform, 2),
        "equity": round(equity, 2),
        "balance": round(balance, 2),
        "raw_payload": {"deals": raw_deals},
    }


def fetch_account_snapshot() -> dict[str, float]:
    info = mt5.account_info()
    if not info:
        raise RuntimeError(f"account_info failed: {mt5.last_error()}")
    return {"equity": round(float(info.equity), 2), "balance": round(float(info.balance), 2)}

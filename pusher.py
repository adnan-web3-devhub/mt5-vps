"""Push data to Laravel API with HMAC authentication."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)


class LaravelPusher:
    def __init__(self, api_url: str, token: str, hmac_secret: str):
        self.api_url = api_url.rstrip("/")
        self.token = token
        self.hmac_secret = hmac_secret

    def _headers(self, body: str) -> dict[str, str]:
        signature = hmac.new(
            self.hmac_secret.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-Arbitrage-Signature": signature,
        }

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":"), default=str)
        url = f"{self.api_url}/{path.lstrip('/')}"
        response = requests.post(url, data=body, headers=self._headers(body), timeout=120)
        response.raise_for_status()
        return response.json()

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self.api_url}/{path.lstrip('/')}"
        # GET requests: sign empty body
        response = requests.get(url, headers=self._headers(""), timeout=60)
        response.raise_for_status()
        return response.json()

    def fetch_master_accounts(self) -> list[dict[str, Any]]:
        data = self._get("master-accounts")
        return data.get("accounts", [])

    def push_sync(self, master_account_id: int, equity: float, balance: float) -> None:
        self._post("mt5-sync", {
            "master_account_id": master_account_id,
            "equity": equity,
            "balance": balance,
        })

    def push_weekly_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("mt5-report", payload)

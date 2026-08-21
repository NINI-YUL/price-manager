"""ExchangeRate-API free endpoint adapter."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.request import Request, urlopen

from src.models import ProviderRateBundle

EXCHANGE_RATE_API_URL = "https://open.er-api.com/v6/latest/USD"


class ExchangeRateApiProvider:
    def __init__(
        self,
        *,
        fetch_json: Callable[[], Mapping[str, Any]] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._fetch_json = fetch_json or self._request_json
        self._timeout_seconds = timeout_seconds

    def fetch_latest(self) -> ProviderRateBundle:
        payload = self._fetch_json()
        if payload.get("result") != "success":
            error_type = payload.get("error-type", "unknown")
            raise RuntimeError(f"供应商返回失败：{error_type}")
        if payload.get("base_code") != "USD":
            raise ValueError("供应商响应基准币种不是 USD")
        raw_rates = payload.get("rates")
        if not isinstance(raw_rates, Mapping):
            raise TypeError("供应商响应缺少 rates")
        try:
            updated_at = datetime.fromtimestamp(
                int(payload["time_last_update_unix"]),
                UTC,
            )
            next_update_at = datetime.fromtimestamp(
                int(payload["time_next_update_unix"]),
                UTC,
            )
            rates = {str(code): Decimal(str(value)) for code, value in raw_rates.items()}
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"供应商响应字段无效：{error}") from error
        return ProviderRateBundle(
            provider="EXCHANGE_RATE_API",
            base_currency="USD",
            updated_at=updated_at,
            next_update_at=next_update_at,
            rates=rates,
        )

    def _request_json(self) -> Mapping[str, Any]:
        request = Request(
            EXCHANGE_RATE_API_URL,
            headers={
                "Accept": "application/json",
                "User-Agent": "price-manager/0.1",
            },
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:
            payload = response.read().decode("utf-8")
        parsed = json.loads(payload, parse_float=Decimal)
        if not isinstance(parsed, Mapping):
            raise TypeError("供应商响应不是 JSON 对象")
        return parsed

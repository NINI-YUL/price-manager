from datetime import UTC, datetime
from decimal import Decimal

import pytest

from src.adapters.exchange_rate_api import ExchangeRateApiProvider


def test_provider_parses_exchange_rate_api_payload_without_float_loss() -> None:
    payload = {
        "result": "success",
        "provider": "https://www.exchangerate-api.com",
        "time_last_update_unix": 1787184151,
        "time_next_update_unix": 1787271101,
        "base_code": "USD",
        "rates": {
            "USD": "1",
            "JPY": "147.123456",
            "EUR": "0.867",
        },
    }

    bundle = ExchangeRateApiProvider(fetch_json=lambda: payload).fetch_latest()

    assert bundle.provider == "EXCHANGE_RATE_API"
    assert bundle.base_currency == "USD"
    assert bundle.updated_at == datetime.fromtimestamp(1787184151, UTC)
    assert bundle.next_update_at == datetime.fromtimestamp(1787271101, UTC)
    assert bundle.rates["JPY"] == Decimal("147.123456")


@pytest.mark.parametrize(
    ("payload", "error", "message"),
    [
        (
            {"result": "error", "error-type": "quota-reached"},
            RuntimeError,
            "quota-reached",
        ),
        (
            {"result": "success", "base_code": "EUR", "rates": {}},
            ValueError,
            "不是 USD",
        ),
        (
            {"result": "success", "base_code": "USD"},
            TypeError,
            "缺少 rates",
        ),
        (
            {
                "result": "success",
                "base_code": "USD",
                "rates": {"USD": "1"},
                "time_last_update_unix": "invalid",
                "time_next_update_unix": 1787271101,
            },
            ValueError,
            "字段无效",
        ),
    ],
)
def test_provider_rejects_unsuccessful_or_malformed_payloads(
    payload,
    error: type[Exception],
    message: str,
) -> None:
    provider = ExchangeRateApiProvider(fetch_json=lambda: payload)

    with pytest.raises(error, match=message):
        provider.fetch_latest()

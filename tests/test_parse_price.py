"""
Тесты для price_monitor.py.

parse_price_text тестируется напрямую (без HTML и сети). Отдельно —
проверка, что send_telegram_message не печатает токен бота при ошибке
отправки (текст исключения requests содержит URL с токеном, наружу он
попадать не должен).
"""

import sys
from pathlib import Path

import pytest
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import price_monitor  # noqa: E402


# =====================================================================
# parse_price_text — одно число (регрессия: как было раньше)
# =====================================================================

@pytest.mark.parametrize("text, expected_price", [
    ("1 990 ₽", 1990.0),
    ("1\xa0990 ₽", 1990.0),
    ("1 990,50 ₽", 1990.5),
    ("1990", 1990.0),
    ("от 1 990 ₽", 1990.0),
])
def test_parse_price_text_single_number(text, expected_price):
    price, error = price_monitor.parse_price_text(text)
    assert price == expected_price
    assert error is None


# =====================================================================
# parse_price_text — несколько чисел: ошибка, а не склейка
# =====================================================================

@pytest.mark.parametrize("text", [
    "1 990 ₽ 2 500 ₽",
    "1990 руб. (было 2490)",
])
def test_parse_price_text_multiple_numbers_is_error(text):
    price, error = price_monitor.parse_price_text(text)
    assert price is None
    assert "несколько чисел" in error


# =====================================================================
# parse_price_text — чисел нет
# =====================================================================

def test_parse_price_text_no_number_is_error():
    price, error = price_monitor.parse_price_text("Нет в наличии")
    assert price is None
    assert "не удалось распознать" in error


# =====================================================================
# send_telegram_message — токен не должен попадать в вывод при ошибке
# =====================================================================

def test_send_telegram_message_does_not_leak_token(monkeypatch, capsys):
    fake_token = "123456789:AAFAKEtokenFAKE"
    url_with_token = f"https://api.telegram.org/bot{fake_token}/sendMessage"

    class FakeResponse:
        status_code = 403

    def fake_post(*args, **kwargs):
        raise requests.exceptions.HTTPError(
            f"403 Client Error: Forbidden for url: {url_with_token}",
            response=FakeResponse(),
        )

    monkeypatch.setattr(price_monitor.requests, "post", fake_post)

    result = price_monitor.send_telegram_message(fake_token, "12345", "текст сообщения")

    assert result is False
    captured = capsys.readouterr()
    assert fake_token not in captured.out
    assert url_with_token not in captured.out
    assert "HTTP 403" in captured.out

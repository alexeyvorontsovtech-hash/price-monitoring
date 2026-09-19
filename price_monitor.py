#!/usr/bin/env python3
"""
Мониторинг цены на сайте с уведомлением в Telegram при изменении.

Скрипт запускается один раз за вызов (без встроенного планировщика — расписание
задаётся снаружи, через cron или Планировщик заданий Windows). При каждом
запуске он:
  1. скачивает страницу и достаёт цену(ы) товара(ов) по CSS-селектору;
  2. сравнивает с последней ценой из истории (history.csv);
  3. если цена изменилась — отправляет уведомление в Telegram;
  4. в любом случае дописывает новую строку в историю.

Пример запуска:
    python price_monitor.py
    python price_monitor.py --config config.json
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

import pandas as pd
import requests
from bs4 import BeautifulSoup

TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
REQUEST_TIMEOUT = 10


# =====================================================================
# Аргументы командной строки и конфиг
# =====================================================================

def parse_args():
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description="Проверяет цену на странице и уведомляет в Telegram об изменении.",
    )
    parser.add_argument(
        "--config", "-c", default="config.json",
        help="путь к JSON-конфигу (по умолчанию config.json)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="не отправлять в Telegram, а печатать то же сообщение в консоль; "
             "токен и chat_id для этого режима не нужны",
    )
    return parser.parse_args()


def load_config(path):
    """Читает и проверяет JSON-конфиг.

    Ожидаемая структура:
        {
          "url": "...",
          "products": [{"name": "...", "selector": "..."}, ...],
          "telegram_chat_id": "...",
          "history_file": "history.csv",
          "log_file": "price_monitor.log",
          "notify_on_errors": true
        }
    """
    if not os.path.exists(path):
        sys.exit(f"Ошибка: файл конфига не найден: {path}")

    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(
            f"Ошибка: конфиг {path} — некорректный JSON "
            f"(строка {e.lineno}, символ {e.colno}): {e.msg}"
        )

    required = ["url", "products", "telegram_chat_id"]
    missing = [key for key in required if key not in config]
    if missing:
        sys.exit(f"Ошибка: в конфиге {path} не хватает полей: {', '.join(missing)}")

    if not isinstance(config["products"], list) or not config["products"]:
        sys.exit(f"Ошибка: в конфиге {path} поле \"products\" должно быть непустым списком")

    for product in config["products"]:
        if "name" not in product or "selector" not in product:
            sys.exit(
                f"Ошибка: в конфиге {path} у каждого товара в \"products\" "
                "должны быть поля \"name\" и \"selector\""
            )

    config.setdefault("history_file", "history.csv")
    config.setdefault("log_file", "price_monitor.log")
    config.setdefault("notify_on_errors", True)
    return config


def get_bot_token():
    """Читает токен Telegram-бота из переменной окружения."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit(
            "Ошибка: не задана переменная окружения TELEGRAM_BOT_TOKEN.\n"
            "       Установите её перед запуском (см. README)."
        )
    return token


# =====================================================================
# Получение цены со страницы
# =====================================================================

def download_page(url):
    """Скачивает HTML страницы. Возвращает (html, ошибка) — один из них всегда None.

    Если url не начинается с http:// или https://, он считается локальным путём
    (обычным или в виде file://) — страница в этом случае читается с диска,
    без обращения к интернету (удобно для проверки на testpage/index.html).
    """
    if not re.match(r"^https?://", url):
        path = url[len("file://"):] if url.startswith("file://") else url
        try:
            with open(path, encoding="utf-8") as f:
                return f.read(), None
        except FileNotFoundError:
            return None, f"локальный файл не найден: {path}"
        except OSError as e:
            return None, f"не удалось прочитать локальный файл: {e}"

    try:
        response = requests.get(
            url, timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "price-monitor-bot/1.0"},
        )
        response.raise_for_status()
        return response.text, None
    except requests.exceptions.Timeout:
        return None, "превышено время ожидания ответа от сайта"
    except requests.exceptions.ConnectionError:
        return None, "не удалось подключиться к сайту (проверьте адрес и интернет-соединение)"
    except requests.exceptions.HTTPError as e:
        return None, f"сайт вернул ошибку: {e}"
    except requests.exceptions.RequestException as e:
        return None, f"ошибка запроса: {e}"


def parse_price_text(text):
    """Превращает текст цены ("1 990 ₽", "1990", "1 990,50 ₽") в число.

    Возвращает (цена, ошибка) — один из них всегда None. Ищет в тексте все
    числа (пробел, \xa0 и другие похожие на пробел символы — разделитель
    тысяч по 3 цифры, , или . — десятичный разделитель). Если число одно —
    оно и есть цена. Если чисел нет — цена не распознана. Если чисел
    несколько (например, в блоке рядом старая и новая цена) — селектор
    захватывает лишнее, гадать нельзя, это тоже ошибка.
    """
    normalized = re.sub(r"\s+", " ", text)
    matches = re.findall(r"\d{1,3}(?:[ ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?", normalized)

    if not matches:
        return None, f"не удалось распознать цену в тексте '{text}'"

    if len(matches) > 1:
        return None, f"в элементе несколько чисел ('{text}'), уточните CSS-селектор"

    cleaned = matches[0].replace(" ", "").replace(",", ".")
    return float(cleaned), None


def extract_price(html, selector):
    """Ищет цену на странице по CSS-селектору. Возвращает (цена, ошибка)."""
    soup = BeautifulSoup(html, "html.parser")
    element = soup.select_one(selector)
    if element is None:
        return None, f"элемент по селектору '{selector}' не найден (возможно, изменилась структура сайта)"

    text = element.get_text(" ", strip=True)
    return parse_price_text(text)


# =====================================================================
# История цен (CSV)
# =====================================================================

def read_history(history_path):
    """Читает CSV-историю. Если файла ещё нет — возвращает пустую таблицу."""
    if not os.path.exists(history_path):
        return pd.DataFrame(columns=["timestamp", "product", "price"])
    return pd.read_csv(history_path)


def get_last_price(history_path, product_name):
    """Возвращает последнюю известную цену товара из истории или None, если записей ещё нет."""
    history = read_history(history_path)
    product_rows = history[history["product"] == product_name]
    if product_rows.empty:
        return None
    return float(product_rows.iloc[-1]["price"])


def append_history_row(history_path, timestamp, product_name, price):
    """Дописывает одну строку в CSV-историю (создаёт файл с заголовком при первом запуске)."""
    row = pd.DataFrame([{"timestamp": timestamp, "product": product_name, "price": price}])
    file_exists = os.path.exists(history_path)
    row.to_csv(history_path, mode="a", header=not file_exists, index=False, encoding="utf-8")


# =====================================================================
# Уведомления в Telegram
# =====================================================================

def format_price(value):
    """Форматирует число как цену: '1 990 ₽' или '1 990.50 ₽'."""
    if value == int(value):
        formatted = f"{int(value):,}".replace(",", " ")
    else:
        formatted = f"{value:,.2f}".replace(",", " ")
    return f"{formatted} ₽"


def build_change_message(product_name, old_price, new_price):
    """Собирает текст уведомления об изменении цены."""
    diff = new_price - old_price
    sign = "+" if diff > 0 else ""
    return (
        f"🔔 Изменилась цена товара «{product_name}»\n"
        f"Было: {format_price(old_price)}\n"
        f"Стало: {format_price(new_price)}\n"
        f"Разница: {sign}{format_price(diff)}"
    )


def send_telegram_message(token, chat_id, text):
    """Отправляет сообщение в Telegram через Bot API. Возвращает True/False (успех).

    Текст исключения requests не печатается и не логируется: он содержит
    полный URL запроса вместе с токеном бота. Наружу идёт только код ответа
    (для HTTP-ошибок) или имя класса исключения — без токена.
    """
    url = TELEGRAM_API_URL.format(token=token)
    try:
        response = requests.post(
            url, data={"chat_id": chat_id, "text": text}, timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        return True
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        print(f"Ошибка: не удалось отправить уведомление в Telegram (HTTP {status})")
        return False
    except requests.exceptions.RequestException as e:
        print(f"Ошибка: не удалось отправить уведомление в Telegram ({type(e).__name__})")
        return False


def notify(token, chat_id, text, dry_run):
    """Уведомляет об изменении: в --dry-run печатает текст в консоль, иначе шлёт в Telegram."""
    if dry_run:
        print(f"[DRY-RUN] Сообщение в Telegram:\n{text}")
        return True
    return send_telegram_message(token, chat_id, text)


# =====================================================================
# Лог ошибок
# =====================================================================

def log_error(log_path, message):
    """Печатает сообщение об ошибке и дописывает его в лог-файл с меткой времени."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass  # если лог не пишется, это не повод ронять сам мониторинг


# =====================================================================
# Обработка одного товара
# =====================================================================

def check_product(product, html, config, token, timestamp, dry_run):
    """Проверяет цену одного товара: сравнивает с историей, уведомляет, пишет строку в CSV."""
    name = product["name"]
    selector = product["selector"]
    history_path = config["history_file"]

    price, error = extract_price(html, selector)
    if error:
        message = f"Не удалось проверить цену «{name}»: {error}"
        log_error(config["log_file"], message)
        if config["notify_on_errors"]:
            notify(token, config["telegram_chat_id"], f"⚠️ {message}", dry_run)
        return

    last_price = get_last_price(history_path, name)
    append_history_row(history_path, timestamp, name, price)

    if last_price is None:
        print(f"{name}: первая проверка, цена зафиксирована — {format_price(price)}")
        return

    if price == last_price:
        print(f"{name}: цена не изменилась ({format_price(price)})")
        return

    message = build_change_message(name, last_price, price)
    notify(token, config["telegram_chat_id"], message, dry_run)
    if not dry_run:
        print(message)


# =====================================================================
# Главная функция
# =====================================================================

def main():
    args = parse_args()
    config = load_config(args.config)
    token = None if args.dry_run else get_bot_token()

    html, error = download_page(config["url"])
    if error:
        message = f"Не удалось проверить цены: страница недоступна ({config['url']}) — {error}"
        log_error(config["log_file"], message)
        if config["notify_on_errors"]:
            notify(token, config["telegram_chat_id"], f"⚠️ {message}", args.dry_run)
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for product in config["products"]:
        check_product(product, html, config, token, timestamp, args.dry_run)


if __name__ == "__main__":
    main()

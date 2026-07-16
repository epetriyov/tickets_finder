#!/usr/bin/env python3
"""Мониторинг билетов на Басту (29.08.2026, 19:00, БСА «Лужники»).

Опрашивает API виджета Яндекс Билетов (см. afisha_api.py) и присылает в
Telegram уведомление, когда находит >= SEATS_NEEDED соседних свободных мест
в одном ряду по цене не дороже MAX_PRICE_PER_TICKET рублей каждое.

Флаги:
  --test   тестовое сообщение в Telegram (проверка токена и chat_id)
  --force  симулирует найденную пару (ряд 23, места 5-6 по 12000₽) и шлёт
           реальное уведомление — проверка форматирования end-to-end
  --check  разовая проверка: печатает найденные варианты в консоль,
           НЕ шлёт в Telegram и НЕ трогает state.json
  --dump   сохраняет сырой ответ hallplan-API в hallplan_dump.json и выходит
"""

import argparse
import json
import logging
import os
import random
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # позволяет гонять --check/--dump без установленного dotenv
    def load_dotenv(*_args, **_kwargs):
        return False

import afisha_api
import telegram_notify

log = logging.getLogger("watcher")

BASE_DIR = Path(__file__).resolve().parent
# В Docker DATA_DIR=/data (см. docker-compose.yml) — state и логи живут в volume
# и переживают пересборку контейнера; без Docker всё пишется рядом со скриптом.
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
STATE_FILE = DATA_DIR / "state.json"
LOG_FILE = DATA_DIR / "watcher.log"
DUMP_FILE = DATA_DIR / "hallplan_dump.json"

# Ключ сеанса 29.08.2026 19:00 = base64("2966|732357|3292147|1788019200000").
# Для 30.08 последний блок был бы 1788105600000 (ключ ...MTA1NjAwMDAw).
DEFAULT_SESSION_KEY = "Mjk2Nnw3MzIzNTd8MzI5MjE0N3wxNzg4MDE5MjAwMDAw"
# clientKey, с которым официальный виджет ходит в собственный API
# (подсмотрен в DevTools 15.07.2026; если перестанет работать — см. README).
DEFAULT_CLIENT_KEY = "f6dc63f9-18ab-471b-89ff-eb9773910840"
DEFAULT_TARGET_URL = "https://afisha.yandex.ru/moscow/concert/basta-2026-08-29"

MAX_RUNS_PER_MESSAGE = 12  # больше вариантов в одно сообщение не влезает читабельно
ERRORS_BEFORE_ALERT = 20   # столько ошибок подряд -> предупреждение в Telegram


def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)


def load_config():
    load_dotenv(BASE_DIR / ".env")
    sectors_raw = os.getenv("SECTORS", "")
    try:
        sectors = afisha_api.parse_sectors(sectors_raw)
    except ValueError as exc:
        raise SystemExit("Ошибка в SECTORS: {}".format(exc))
    return {
        "sectors_raw": sectors_raw or "все",
        "sectors": sectors,
        "token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        "session_key": os.getenv("SESSION_KEY", DEFAULT_SESSION_KEY),
        "client_key": os.getenv("CLIENT_KEY", DEFAULT_CLIENT_KEY),
        "target_url": os.getenv("TARGET_URL", DEFAULT_TARGET_URL),
        "interval": int(os.getenv("CHECK_INTERVAL_SECONDS", "30")),
        "max_price": int(os.getenv("MAX_PRICE_PER_TICKET", "15000")),
        "seats_needed": int(os.getenv("SEATS_NEEDED", "2")),
        # 1 = не уведомлять о секторах "(ограниченная видимость)"
        "ignore_limited_view": os.getenv("IGNORE_LIMITED_VIEW", "0") == "1",
    }


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        return {"notified_seats": set(state.get("notified_seats", []))}
    except (OSError, ValueError):
        return {"notified_seats": set()}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"notified_seats": sorted(state["notified_seats"])}, f,
                  ensure_ascii=False, indent=1)
    tmp.replace(STATE_FILE)


def format_run_line(run):
    places = run["places"]
    if run["price_min"] == run["price_max"]:
        price = "по {}₽".format(run["price_min"])
    else:
        price = "{}–{}₽".format(run["price_min"], run["price_max"])
    total = ("(со сбором {}₽)".format(run["total_min"])
             if run["total_min"] == run["total_max"]
             else "(со сбором {}–{}₽)".format(run["total_min"], run["total_max"]))
    return "• {}, ряд {}: места {}–{} ({} шт.) {} {}".format(
        run["level"], run["row"], places[0], places[-1], len(places), price, total
    )


def format_message(runs, cfg):
    lines = ["🎟 Баста 29.08, БСА «Лужники» — есть места рядом (≤{}₽)!".format(cfg["max_price"]), ""]
    for run in runs[:MAX_RUNS_PER_MESSAGE]:
        lines.append(format_run_line(run))
    if len(runs) > MAX_RUNS_PER_MESSAGE:
        lines.append("…и ещё {} вариантов".format(len(runs) - MAX_RUNS_PER_MESSAGE))
    lines += ["", "Покупать здесь: " + cfg["target_url"]]
    return "\n".join(lines)


def check_once(cfg):
    """Одна проверка API. Возвращает список цепочек подходящих мест."""
    hallplan = afisha_api.fetch_hallplan(cfg["session_key"], cfg["client_key"])
    locked = afisha_api.fetch_locked_seat_ids(cfg["session_key"], cfg["client_key"])
    seats = afisha_api.extract_seats(
        hallplan, locked_ids=locked,
        ignore_limited_view=cfg["ignore_limited_view"],
        allowed_sectors=cfg["sectors"],
    )
    runs = afisha_api.find_runs(seats, cfg["max_price"], cfg["seats_needed"])
    log.info(
        "проверка: свободных мест в зале %s, из них подходящих цепочек: %s",
        hallplan.get("availableSeatCount"), len(runs),
    )
    return runs


HELP_TEXT = (
    "Команды:\n"
    "/check — что доступно прямо сейчас (по настроенным критериям)\n"
    "/check all — то же по всему залу (без фильтра секторов и видимости)"
)


def handle_command(cfg, text):
    """Обрабатывает команду из Telegram. Возвращает текст ответа или None."""
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]  # '/check@БотИмя' -> '/check'

    if cmd in ("/start", "/help"):
        return HELP_TEXT

    if cmd == "/check":
        cfg_check = dict(cfg)
        if len(parts) > 1 and parts[1].lower() in ("all", "все", "всё"):
            cfg_check["ignore_limited_view"] = False
            cfg_check["sectors"] = None  # весь зал
        try:
            runs = check_once(cfg_check)
        except Exception as exc:  # noqa: BLE001 — ошибку показываем пользователю
            log.exception("ручная проверка не удалась")
            return "Ошибка проверки: {}".format(exc)
        if not runs:
            return "Подходящих мест сейчас нет (≤{}₽, {} рядом, сектора: {}{}).".format(
                cfg_check["max_price"], cfg_check["seats_needed"],
                cfg["sectors_raw"] if cfg_check["sectors"] else "все",
                ", без ограниченной видимости" if cfg_check["ignore_limited_view"] else "",
            )
        lines = ["Сейчас доступно ({} вариантов):".format(len(runs)), ""]
        for run in runs[:MAX_RUNS_PER_MESSAGE]:
            lines.append(format_run_line(run))
        if len(runs) > MAX_RUNS_PER_MESSAGE:
            lines.append("…и ещё {}".format(len(runs) - MAX_RUNS_PER_MESSAGE))
        lines += ["", cfg["target_url"]]
        return "\n".join(lines)

    return None  # незнакомые сообщения молча игнорируем


def bot_command_loop(cfg):
    """Фоновый поток: long polling входящих команд бота.

    Если у бота активен webhook (занят другим сервисом) — getUpdates невозможен;
    тогда команды отключаются, но мониторинг продолжает работать.
    """
    offset = None
    while True:
        try:
            updates = telegram_notify.get_updates(cfg["token"], offset=offset)
        except telegram_notify.WebhookConflict:
            log.warning(
                "бот-команды: у бота активен webhook, /check недоступен. "
                "Нужен отдельный бот для вотчера (см. README). Повтор через 10 мин."
            )
            time.sleep(600)
            continue
        except telegram_notify.TelegramError as exc:
            log.warning("бот-команды: getUpdates не удался: %s", exc)
            time.sleep(30)
            continue

        for upd in updates:
            offset = upd["update_id"] + 1
            try:
                msg = upd.get("message") or {}
                text = (msg.get("text") or "").strip()
                chat = (msg.get("chat") or {}).get("id")
                if not text or str(chat) != str(cfg["chat_id"]):
                    continue  # реагируем только на свой чат
                log.info("бот-команды: получено %r", text.split()[0])
                reply = handle_command(cfg, text)
                if reply:
                    telegram_notify.send_message(cfg["token"], cfg["chat_id"], reply)
            except Exception:  # noqa: BLE001 — поток команд не должен умирать
                log.exception("бот-команды: ошибка обработки update")


def watch_loop(cfg):
    state = load_state()
    consecutive_errors = 0
    error_alert_sent = False

    log.info(
        "старт мониторинга: интервал %sс, максимум %s₽/билет, мест рядом: %s, "
        "сектора: %s, игнорировать ограниченную видимость: %s",
        cfg["interval"], cfg["max_price"], cfg["seats_needed"],
        cfg["sectors_raw"], cfg["ignore_limited_view"],
    )

    threading.Thread(target=bot_command_loop, args=(cfg,), daemon=True,
                     name="bot-commands").start()
    log.info("бот-команды: поток long polling запущен (/check)")

    while True:
        try:
            runs = check_once(cfg)
            consecutive_errors = 0
            error_alert_sent = False

            # Уведомляем только о цепочках, где есть хоть одно ещё не виденное место.
            # Если цепочка лишь укоротилась (часть мест раскупили) — это не новость.
            new_runs = [
                r for r in runs
                if not set(r["seat_keys"]) <= state["notified_seats"]
            ]
            if new_runs:
                text = format_message(new_runs, cfg)
                if telegram_notify.send_message(cfg["token"], cfg["chat_id"], text):
                    for r in new_runs:
                        state["notified_seats"].update(r["seat_keys"])
                    save_state(state)
                # если отправка не удалась — state не трогаем,
                # попробуем уведомить на следующей итерации
        except afisha_api.HallplanError as exc:
            consecutive_errors += 1
            log.warning("проверка не удалась (%s подряд): %s", consecutive_errors, exc)
            if consecutive_errors >= ERRORS_BEFORE_ALERT and not error_alert_sent:
                error_alert_sent = telegram_notify.send_message(
                    cfg["token"], cfg["chat_id"],
                    "⚠️ basta-watcher: {} проверок подряд не удались "
                    "(последняя ошибка: {}). Проверь сервер/логи.".format(consecutive_errors, exc),
                )
        except Exception:  # noqa: BLE001 — процесс не должен падать ни при каких условиях
            consecutive_errors += 1
            log.exception("неожиданная ошибка (%s подряд)", consecutive_errors)

        # лёгкий джиттер, чтобы запросы не шли строго раз в N секунд
        time.sleep(cfg["interval"] * random.uniform(0.85, 1.15))


def cmd_test(cfg):
    ok = telegram_notify.send_message(
        cfg["token"], cfg["chat_id"],
        "✅ basta-watcher на связи. Токен и chat_id работают.",
    )
    print("Отправлено." if ok else "НЕ отправлено — смотри watcher.log")
    return 0 if ok else 1


def cmd_force(cfg):
    fake_run = {
        "level": "Сектор B105", "row": "23", "places": [5, 6],
        "price_min": 12000, "price_max": 12000,
        "total_min": 13200, "total_max": 13200,
        "seat_keys": [],
    }
    ok = telegram_notify.send_message(cfg["token"], cfg["chat_id"],
                                      format_message([fake_run], cfg))
    print("Отправлено." if ok else "НЕ отправлено — смотри watcher.log")
    return 0 if ok else 1


def cmd_check(cfg):
    runs = check_once(cfg)
    if not runs:
        print("Подходящих цепочек мест не найдено "
              "(≤{}₽, {} рядом).".format(cfg["max_price"], cfg["seats_needed"]))
        return 0
    print("Найдено цепочек: {}".format(len(runs)))
    for run in runs:
        print(" " + format_run_line(run))
    return 0


def cmd_dump(cfg):
    hallplan = afisha_api.fetch_hallplan(cfg["session_key"], cfg["client_key"])
    with open(DUMP_FILE, "w", encoding="utf-8") as f:
        json.dump(hallplan, f, ensure_ascii=False, indent=1)
    print("Сырой hallplan сохранён в {}".format(DUMP_FILE))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--test", action="store_true", help="тестовое сообщение в Telegram")
    group.add_argument("--force", action="store_true", help="симуляция найденной пары мест")
    group.add_argument("--check", action="store_true", help="разовая проверка без Telegram")
    group.add_argument("--dump", action="store_true", help="сохранить сырой ответ API и выйти")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()

    if not (args.check or args.dump) and (not cfg["token"] or not cfg["chat_id"]):
        print("Заполни TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env "
              "(см. .env.example и README.md)", file=sys.stderr)
        return 1

    if args.test:
        return cmd_test(cfg)
    if args.force:
        return cmd_force(cfg)
    if args.check:
        return cmd_check(cfg)
    if args.dump:
        return cmd_dump(cfg)

    watch_loop(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())

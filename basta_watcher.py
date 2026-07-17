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
import html
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
# Обновляется после каждой итерации; по нему работает Docker healthcheck
HEALTH_FILE = DATA_DIR / "health.json"

# Ключ сеанса 29.08.2026 19:00 = base64("2966|732357|3292147|1788019200000").
# Для 30.08 последний блок был бы 1788105600000 (ключ ...MTA1NjAwMDAw).
DEFAULT_SESSION_KEY = "Mjk2Nnw3MzIzNTd8MzI5MjE0N3wxNzg4MDE5MjAwMDAw"
# clientKey, с которым официальный виджет ходит в собственный API
# (подсмотрен в DevTools 15.07.2026; если перестанет работать — см. README).
DEFAULT_CLIENT_KEY = "f6dc63f9-18ab-471b-89ff-eb9773910840"
DEFAULT_TARGET_URL = "https://afisha.yandex.ru/moscow/concert/basta-2026-08-29"

MAX_RUNS_PER_MESSAGE = 12       # больше вариантов в одно сообщение не влезает читабельно
ERROR_ALERT_COOLDOWN = 3600     # повторный алерт об ошибках не чаще раза в час


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
        # мониторинг: heartbeat раз в N часов, алерт после N ошибок подряд
        "heartbeat_hours": float(os.getenv("HEARTBEAT_HOURS", "12")),
        "error_alert_threshold": int(os.getenv("ERROR_ALERT_THRESHOLD", "5")),
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


def format_run_line_html(run, cfg):
    """Строка-ссылка: клик открывает виджет с уже добавленными в корзину местами."""
    link = afisha_api.buy_link(cfg["session_key"], run, cfg["seats_needed"])
    return '• <a href="{}">{}</a>'.format(html.escape(link, quote=True),
                                          html.escape(format_run_line(run)[2:]))


def format_message(runs, cfg):
    """HTML-сообщение: каждая строка — прямая ссылка на покупку этих мест."""
    lines = ["🎟 Баста 29.08, БСА «Лужники» — есть места рядом (≤{}₽)!".format(cfg["max_price"]),
             "Клик по варианту сразу кладёт места в корзину:", ""]
    for run in runs[:MAX_RUNS_PER_MESSAGE]:
        lines.append(format_run_line_html(run, cfg))
    if len(runs) > MAX_RUNS_PER_MESSAGE:
        lines.append("…и ещё {} вариантов".format(len(runs) - MAX_RUNS_PER_MESSAGE))
    lines += ["", 'Вся схема: {}'.format(html.escape(cfg["target_url"]))]
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
        lines = ["Сейчас доступно ({} вариантов), клик кладёт места в корзину:".format(len(runs)), ""]
        for run in runs[:MAX_RUNS_PER_MESSAGE]:
            lines.append(format_run_line_html(run, cfg))
        if len(runs) > MAX_RUNS_PER_MESSAGE:
            lines.append("…и ещё {}".format(len(runs) - MAX_RUNS_PER_MESSAGE))
        lines += ["", html.escape(cfg["target_url"])]
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
                    telegram_notify.send_message(cfg["token"], cfg["chat_id"], reply,
                                                 parse_mode="HTML")
            except Exception:  # noqa: BLE001 — поток команд не должен умирать
                log.exception("бот-команды: ошибка обработки update")


def write_health(ok, consecutive_errors, last_error=None):
    """Файл-маячок для Docker healthcheck: свежесть = живость цикла."""
    try:
        tmp = HEALTH_FILE.with_suffix(".htmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "ts": time.time(),
                "ok": ok,
                "consecutive_errors": consecutive_errors,
                "last_error": str(last_error) if last_error else None,
            }, f, ensure_ascii=False)
        tmp.replace(HEALTH_FILE)
    except OSError as exc:
        log.warning("не удалось записать %s: %s", HEALTH_FILE, exc)


def describe_criteria(cfg):
    return "сектора {}, ≤{}₽, {} рядом, интервал {}с".format(
        cfg["sectors_raw"], cfg["max_price"], cfg["seats_needed"], cfg["interval"]
    )


def format_heartbeat(cfg, stats, consecutive_errors):
    hours = (time.time() - stats["since"]) / 3600
    lines = [
        "💓 basta-watcher работает ({})".format(describe_criteria(cfg)),
        "За последние {:.1f} ч: проверок {}, ошибок {}.".format(
            hours, stats["checks"], stats["errors"]),
    ]
    if stats["last_runs"]:
        lines.append("Сейчас подходящих вариантов: {}.".format(stats["last_runs"]))
    else:
        lines.append("Подходящих мест в целевых секторах пока нет — жду.")
    if consecutive_errors:
        lines.append("⚠️ Текущая серия ошибок: {} подряд.".format(consecutive_errors))
    return "\n".join(lines)


def watch_loop(cfg):
    state = load_state()
    consecutive_errors = 0
    last_error = None
    error_alerted = False   # активен ли сейчас алерт о серии ошибок
    last_alert_ts = 0.0
    stats = {"since": time.time(), "checks": 0, "errors": 0, "last_runs": 0}
    last_heartbeat = time.time()

    log.info("старт мониторинга: %s, игнорировать ограниченную видимость: %s",
             describe_criteria(cfg), cfg["ignore_limited_view"])

    telegram_notify.send_message(
        cfg["token"], cfg["chat_id"],
        "🚀 basta-watcher запущен: {}.\nHeartbeat каждые {:g} ч.".format(
            describe_criteria(cfg), cfg["heartbeat_hours"]),
    )

    threading.Thread(target=bot_command_loop, args=(cfg,), daemon=True,
                     name="bot-commands").start()
    log.info("бот-команды: поток long polling запущен (/check)")

    while True:
        ok = True
        try:
            runs = check_once(cfg)
            stats["checks"] += 1
            stats["last_runs"] = len(runs)
            if error_alerted:
                telegram_notify.send_message(
                    cfg["token"], cfg["chat_id"],
                    "✅ basta-watcher: Афиша снова отвечает "
                    "(было {} ошибок подряд).".format(consecutive_errors),
                )
            consecutive_errors = 0
            last_error = None
            error_alerted = False

            # Уведомляем только о цепочках, где есть хоть одно ещё не виденное место.
            # Если цепочка лишь укоротилась (часть мест раскупили) — это не новость.
            new_runs = [
                r for r in runs
                if not set(r["seat_keys"]) <= state["notified_seats"]
            ]
            if new_runs:
                text = format_message(new_runs, cfg)
                if telegram_notify.send_message(cfg["token"], cfg["chat_id"], text,
                                                parse_mode="HTML"):
                    for r in new_runs:
                        state["notified_seats"].update(r["seat_keys"])
                    save_state(state)
                # если отправка не удалась — state не трогаем,
                # попробуем уведомить на следующей итерации
        except afisha_api.HallplanError as exc:
            ok = False
            consecutive_errors += 1
            stats["errors"] += 1
            last_error = exc
            log.warning("проверка не удалась (%s подряд): %s", consecutive_errors, exc)
        except Exception as exc:  # noqa: BLE001 — процесс не должен падать ни при каких условиях
            ok = False
            consecutive_errors += 1
            stats["errors"] += 1
            last_error = exc
            log.exception("неожиданная ошибка (%s подряд)", consecutive_errors)

        # Алерт о серии ошибок: быстро (после error_alert_threshold подряд),
        # повторно — не чаще ERROR_ALERT_COOLDOWN, пока серия продолжается.
        if (consecutive_errors >= cfg["error_alert_threshold"]
                and time.time() - last_alert_ts >= ERROR_ALERT_COOLDOWN):
            sent = telegram_notify.send_message(
                cfg["token"], cfg["chat_id"],
                "⚠️ basta-watcher: {} проверок подряд не удались.\n"
                "Последняя ошибка: {}\n"
                "Логи: docker compose logs watcher".format(consecutive_errors, last_error),
            )
            if sent:
                error_alerted = True
                last_alert_ts = time.time()

        write_health(ok, consecutive_errors, last_error)

        if time.time() - last_heartbeat >= cfg["heartbeat_hours"] * 3600:
            telegram_notify.send_message(
                cfg["token"], cfg["chat_id"],
                format_heartbeat(cfg, stats, consecutive_errors),
            )
            last_heartbeat = time.time()
            stats = {"since": time.time(), "checks": 0, "errors": 0,
                     "last_runs": stats["last_runs"]}

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
        "level": "Сектор B105", "level_id": 1, "row": "23", "places": [5, 6],
        "seats": [
            {"level_id": 1, "row": "23", "place": 5, "price": 12000, "total": 13200},
            {"level_id": 1, "row": "23", "place": 6, "price": 12000, "total": 13200},
        ],
        "price_min": 12000, "price_max": 12000,
        "total_min": 13200, "total_max": 13200,
        "seat_keys": [],
    }
    ok = telegram_notify.send_message(cfg["token"], cfg["chat_id"],
                                      format_message([fake_run], cfg),
                                      parse_mode="HTML")
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
        print("   купить: " + afisha_api.buy_link(cfg["session_key"], run, cfg["seats_needed"]))
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

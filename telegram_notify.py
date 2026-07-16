"""Отправка уведомлений в Telegram через Bot API."""

import logging

import requests

log = logging.getLogger("watcher.telegram")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"


class TelegramError(Exception):
    """Ошибка Telegram API (сетевая или прикладная)."""


class WebhookConflict(TelegramError):
    """У бота активен webhook — getUpdates недоступен (нужен отдельный бот
    для вотчера или deleteWebhook, см. README)."""


def send_message(token, chat_id, text, timeout=30):
    """Шлёт сообщение. Возвращает True при успехе, исключений не бросает."""
    if not token or not chat_id:
        log.error("Telegram: не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
        return False
    try:
        resp = requests.post(
            API_URL.format(token=token),
            json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=timeout,
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.error("Telegram: не удалось отправить сообщение: %s", exc)
        return False

    if not data.get("ok"):
        log.error("Telegram API вернул ошибку: %s", data)
        return False

    log.info("Telegram: сообщение отправлено")
    return True


def get_updates(token, offset=None, timeout=50):
    """Long polling входящих сообщений. Возвращает список update-объектов.

    Бросает WebhookConflict, если у бота настроен webhook (тогда команды
    вотчера работать не могут), и TelegramError при остальных проблемах.
    """
    params = {"timeout": timeout, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset
    try:
        resp = requests.get(
            UPDATES_URL.format(token=token), params=params, timeout=timeout + 15
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise TelegramError(str(exc)) from exc

    if not data.get("ok"):
        desc = str(data.get("description", data))
        if "webhook is active" in desc:
            raise WebhookConflict(desc)
        raise TelegramError(desc)
    return data["result"]

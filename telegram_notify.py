"""Отправка уведомлений в Telegram через Bot API."""

import logging

import requests

log = logging.getLogger("watcher.telegram")

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


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

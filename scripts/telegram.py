"""Telegram Bot API helpers."""

import json
import requests

TELEGRAM_API = ""


def init(token: str):
    """Set the API base URL from bot token."""
    global TELEGRAM_API
    TELEGRAM_API = f"https://api.telegram.org/bot{token}"


def get_updates(offset: int) -> list:
    """Fetch new messages and callbacks from Telegram Bot API."""
    resp = requests.get(
        f"{TELEGRAM_API}/getUpdates",
        params={
            "offset": offset,
            "limit": 100,
            "timeout": 5,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        },
    )

    if resp.status_code != 200:
        print(f"Error fetching updates: HTTP {resp.status_code}")
        return []

    data = resp.json()
    if not data.get("ok"):
        print(f"Telegram API error: {data}")
        return []

    return data.get("result", [])


def send_message(chat_id: int, thread_id: int, text: str) -> bool:
    """Send a text message to a specific thread. Returns True on success."""
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "text": text,
            "disable_notification": False,
        },
    )

    if resp.status_code == 200 and resp.json().get("ok"):
        return True
    else:
        print(f"Failed to send message: {resp.text}")
        return False


def send_message_with_buttons(
    chat_id: int, thread_id: int, text: str, buttons: list
) -> int | None:
    """Send a message with inline keyboard buttons. Returns message_id or None."""
    keyboard = {"inline_keyboard": [buttons]}
    resp = requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "text": text,
            "disable_notification": False,
            "reply_markup": keyboard,
        },
    )
    if resp.status_code == 200 and resp.json().get("ok"):
        return resp.json()["result"]["message_id"]
    else:
        print(f"Failed to send button message: {resp.text}")
        return None


def edit_message(chat_id: int, message_id: int, text: str, parse_mode: str = None) -> bool:
    """Edit an existing message, removing inline keyboard."""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    resp = requests.post(
        f"{TELEGRAM_API}/editMessageText",
        json=payload,
    )
    if resp.status_code == 200 and resp.json().get("ok"):
        return True
    else:
        print(f"Failed to edit message: {resp.text}")
        return False


def answer_callback(callback_id: str, text: str = "") -> bool:
    """Answer a callback query to dismiss the loading spinner."""
    resp = requests.post(
        f"{TELEGRAM_API}/answerCallbackQuery",
        json={
            "callback_query_id": callback_id,
            "text": text,
        },
    )
    return resp.status_code == 200

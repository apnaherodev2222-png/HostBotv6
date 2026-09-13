"""Telegram Bot API 10.3 Rich Message helpers.

All HTTP failures are normalized to RichMessageError so callers can safely
fall back to the existing python-telegram-bot rendering path.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Sequence

import requests


class RichMessageError(Exception):
    """Raised when Telegram rejects or cannot receive a rich-message call."""


TELEGRAM_API = os.getenv("TELEGRAM_API", "https://api.telegram.org")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
_TIMEOUT = 20


def _call_api(method: str, payload: Dict[str, Any], token: str = None) -> Dict[str, Any]:
    """POST a Bot API request using an optional per-call bot token."""
    # FIX_RICH_1: Allow approval_bot.py to route rich API calls through its own token.
    tk = token or BOT_TOKEN
    if not tk:
        raise RichMessageError("BOT_TOKEN is not configured")
    url = f"{TELEGRAM_API.rstrip('/')}/bot{tk}/{method}"
    try:
        response = requests.post(url, json=payload, timeout=_TIMEOUT)
        try:
            data = response.json()
        except Exception as exc:
            raise RichMessageError(
                f"{method} returned invalid JSON (HTTP {response.status_code}): {exc}"
            ) from exc
    except RichMessageError:
        raise
    except requests.RequestException as exc:
        raise RichMessageError(f"{method} request failed: {exc}") from exc
    except Exception as exc:
        raise RichMessageError(f"{method} request failed: {type(exc).__name__}: {exc}") from exc

    if not isinstance(data, dict) or not data.get("ok"):
        description = data.get("description") if isinstance(data, dict) else None
        raise RichMessageError(description or f"{method} failed")
    result = data.get("result")
    return result if isinstance(result, dict) else {"result": result}


def heading(text: str, size: int = 2) -> Dict[str, Any]:
    return {"type": "heading", "text": str(text), "size": int(size)}


def paragraph(text: str) -> Dict[str, Any]:
    return {"type": "paragraph", "text": str(text)}


def video(url: str) -> Dict[str, Any]:
    return {"type": "video", "video": {"type": "video", "media": str(url)}}


def compact_table(header_row: Sequence[str], data_rows: Sequence[Sequence[str]]) -> Dict[str, Any]:
    def cell(text: Any, is_header: bool = False) -> Dict[str, Any]:
        item: Dict[str, Any] = {
            "text": str(text),
            "align": "left",
            "valign": "middle",
        }
        if is_header:
            item["is_header"] = True
        return item

    cells: List[List[Dict[str, Any]]] = [[cell(value, True) for value in header_row]]
    cells.extend([[cell(value) for value in row] for row in data_rows])
    return {"type": "table", "is_compact": True, "cells": cells}


def button_row(buttons: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Build one in-message button row.

    Supported button fields are text + callback_data or url, and optional style.
    """
    out: List[Dict[str, Any]] = []
    for button in buttons:
        item: Dict[str, Any] = {"text": str(button["text"])}
        if button.get("callback_data") is not None:
            item["callback_data"] = str(button["callback_data"])
        elif button.get("url") is not None:
            item["url"] = str(button["url"])
        if button.get("style") is not None:
            item["style"] = str(button["style"])
        out.append(item)
    return {"type": "buttons", "buttons": out}


def send_blocks(chat_id: int, blocks: Sequence[Dict[str, Any]], token: str = None) -> Dict[str, Any]:
    # FIX_RICH_1: token is optional so existing hosting-bot callers remain backwards compatible.
    return _call_api(
        "sendRichMessage",
        {"chat_id": chat_id, "rich_message": {"blocks": list(blocks)}},
        token=token,
    )


def edit_blocks(chat_id: int, message_id: int, blocks: Sequence[Dict[str, Any]], token: str = None) -> Dict[str, Any]:
    # FIX_RICH_1: token is optional so existing hosting-bot callers keep using BOT_TOKEN.
    return _call_api(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "rich_message": {"blocks": list(blocks)},
        },
        token=token,
    )

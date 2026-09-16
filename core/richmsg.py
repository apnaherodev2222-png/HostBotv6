"""richmsg — Portable Telegram Rich Message client.

MIGRATION NOTE: this file replaces the previous bare-bones core/rich_message.py
(free functions _call_api/send_blocks/edit_blocks/delete_message, no retry,
no circuit breaker, no validation). All block-builder functions
(heading/paragraph/compact_table/button_row/etc.) keep the exact same
signatures, so every existing `rm.heading(...)`-style call site in
hosting_panel_bot.py and approval_bot.py needed ZERO changes.

The transport layer changed shape: sending/editing/deleting now live on a
RichClient INSTANCE (constructed once per bot process with that bot's own
token) instead of free functions that took a `token` argument per call.
RichClient's send/replace/delete methods are already `async def` internally
(they do their own asyncio.to_thread for the blocking HTTP part) — callers
must `await client.send(...)` directly, never wrap them in another
asyncio.to_thread, or the call silently does nothing (an unawaited coroutine).

Zero project dependencies. Only stdlib + requests.

Flow: rich sends use placeholder → rich edit. replace() uses three stages:
rich edit → plain edit → send new + delete old. Async primitives are loop-affine
and are rebuilt when a RichClient is reused across event loops.

Quick start:
    from richmsg import RichClient, heading, paragraph, success_card

    client = RichClient(token="YOUR_BOT_TOKEN")

    async def my_handler(update, context):
        async def fallback():
            return await update.message.reply_text("Done")

        await client.send_or(
            chat_id=update.effective_chat.id,
            blocks=success_card("Done", "Kaam ho gaya"),
            fallback=fallback,
        )
"""

from __future__ import annotations

import os
import asyncio
import logging
import time
import threading
from typing import Any, Callable, Dict, List, Optional, Sequence
from weakref import WeakValueDictionary

import requests

logger = logging.getLogger("richmsg")


class RichMessageError(Exception):
    """Raised when Telegram rejects or cannot receive a rich-message call."""

    def __init__(self, message: str, *, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RichMessageValidationError(RichMessageError):
    """Raised when blocks fail validation — caller's fault, not transport."""


_VALID_BLOCK_TYPES = frozenset({
    "heading", "paragraph", "table", "video", "photo", "buttons",
    "divider", "spacer", "quote", "code", "list", "checklist",
    "details", "spoiler", "markdown", "animation", "audio", "document",
})

_TRANSIENT_MARKERS = (
    "connection reset", "connection aborted", "temporarily unavailable",
    "timed out", "timeout", "bad gateway", "gateway timeout",
    "service unavailable", "too many requests", "http 5", "http 429",
)

_STRIPPABLE_BLOCK_TYPES = frozenset(("video", "photo", "animation", "audio"))


def _require_text(value: Any, name: str) -> str:
    """Require a real string instead of silently converting None/other types."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str, got {type(value).__name__}")
    return value


def _require_int(value: Any, name: str) -> int:
    """Require an integer (bool is rejected because it is a subclass of int)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int, got {type(value).__name__}")
    return value


def heading(text: str, size: int = 2) -> Dict[str, Any]:
    """Build a heading block."""
    size = _require_int(size, "size")
    if size not in (1, 2):
        raise ValueError("size must be 1 or 2")
    return {"type": "heading", "text": _require_text(text, "text"), "size": size}


def paragraph(text: str) -> Dict[str, Any]:
    """Build a paragraph block."""
    return {"type": "paragraph", "text": _require_text(text, "text")}


def video(url: str) -> Dict[str, Any]:
    """Build a video block."""
    return {"type": "video", "video": {"type": "video", "media": _require_text(url, "url")}}


def compact_table(
    header_row: Sequence[str], data_rows: Sequence[Sequence[str]]
) -> Dict[str, Any]:
    """Build the compact table shape used by the source project."""
    def cell(text: Any, is_header: bool = False) -> Dict[str, Any]:
        item: Dict[str, Any] = {"text": str(text), "align": "left", "valign": "middle"}
        if is_header:
            item["is_header"] = True
        return item

    cells: List[List[Dict[str, Any]]] = [[cell(value, True) for value in header_row]]
    cells.extend([[cell(value) for value in row] for row in data_rows])
    return {"type": "table", "is_compact": True, "cells": cells}


def button_row(buttons: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Build one in-message button row."""
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


def divider() -> Dict[str, Any]:
    """Build a full-width divider block."""
    return {"type": "divider"}


def spacer(height: int = 8) -> Dict[str, Any]:
    """Build a vertical spacer block."""
    height = _require_int(height, "height")
    if not 0 <= height <= 200:
        raise ValueError("height must be between 0 and 200")
    return {"type": "spacer", "height": height}


def quote(text: str, author: Optional[str] = None) -> Dict[str, Any]:
    """Build a quote block."""
    block: Dict[str, Any] = {"type": "quote", "text": _require_text(text, "text")}
    if author:
        block["author"] = _require_text(author, "author")
    return block


def code(text: str, language: Optional[str] = None) -> Dict[str, Any]:
    """Build a code block."""
    block: Dict[str, Any] = {"type": "code", "text": _require_text(text, "text")}
    if language:
        block["language"] = _require_text(language, "language")
    return block


def markdown(text: str) -> Dict[str, Any]:
    """Build a markdown block."""
    return {"type": "markdown", "text": _require_text(text, "text")}


def bullet_list(items: Sequence[str]) -> Dict[str, Any]:
    """Build a list block."""
    return {"type": "list", "items": [str(item) for item in items]}


def checklist(items: Sequence[Sequence[Any]]) -> Dict[str, Any]:
    """Build a checklist from (text, checked) pairs."""
    return {
        "type": "checklist",
        "items": [{"text": str(item[0]), "checked": bool(item[1])} for item in items],
    }


def photo(url: str, caption: Optional[str] = None) -> Dict[str, Any]:
    """Build a photo block."""
    block: Dict[str, Any] = {
        "type": "photo",
        "photo": {"type": "photo", "media": _require_text(url, "url")},
    }
    if caption:
        block["caption"] = _require_text(caption, "caption")
    return block


def animation(url: str, caption: Optional[str] = None) -> Dict[str, Any]:
    """Build an animation block."""
    block: Dict[str, Any] = {
        "type": "animation",
        "animation": {"type": "animation", "media": _require_text(url, "url")},
    }
    if caption:
        block["caption"] = _require_text(caption, "caption")
    return block


def audio(url: str, caption: Optional[str] = None) -> Dict[str, Any]:
    """Build an audio block."""
    # FIX_PM_RV2_10: add the same portable media-builder shape for audio.
    block: Dict[str, Any] = {
        "type": "audio",
        "audio": {"type": "audio", "media": _require_text(url, "url")},
    }
    if caption:
        block["caption"] = _require_text(caption, "caption")
    return block


def document(url: str, caption: Optional[str] = None) -> Dict[str, Any]:
    """Build a document block."""
    # FIX_PM_RV2_10: add the same portable media-builder shape for documents.
    block: Dict[str, Any] = {
        "type": "document",
        "document": {"type": "document", "media": _require_text(url, "url")},
    }
    if caption:
        block["caption"] = _require_text(caption, "caption")
    return block


def details(summary: str, body: str) -> Dict[str, Any]:
    """Build a details block."""
    return {"type": "details", "summary": _require_text(summary, "summary"), "text": _require_text(body, "body")}


def spoiler(text: str) -> Dict[str, Any]:
    """Build a spoiler block."""
    return {"type": "spoiler", "text": _require_text(text, "text")}


def button_grid(
    rows: Sequence[Sequence[Dict[str, Any]]],
    widths: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    """Build a flattened buttons block with optional row widths."""
    flat: List[Dict[str, Any]] = []
    for row in rows:
        for btn in row:
            item: Dict[str, Any] = {"text": str(btn["text"])}
            if btn.get("callback_data") is not None:
                item["callback_data"] = str(btn["callback_data"])
            elif btn.get("url") is not None:
                item["url"] = str(btn["url"])
            if btn.get("style") is not None:
                item["style"] = str(btn["style"])
            flat.append(item)
    block: Dict[str, Any] = {"type": "buttons", "buttons": flat}
    if widths:
        block["widths"] = [int(width) for width in widths]
    return block


def rich_callback_button(text: str, callback_data: str, style: str = "primary") -> Dict[str, Any]:
    """Build a rich callback button and enforce Telegram's 64-byte limit."""
    data = str(callback_data)
    if len(data.encode("utf-8")) > 64:
        raise ValueError("callback_data exceeds Telegram's 64-byte limit")
    return {"text": str(text), "callback_data": data, "style": str(style)}


def rich_url_button(text: str, url: str, style: str = "primary") -> Dict[str, Any]:
    """Build a rich URL button."""
    return {"text": str(text), "url": str(url), "style": str(style)}


# Ergonomic aliases.
hr = divider
br = button_row
p = paragraph
h = heading


def validate_blocks(blocks: Sequence[Any]) -> List[str]:
    """Validate block shapes that can be checked locally without Telegram."""
    errors: List[str] = []

    def require_string(mapping: Dict[str, Any], key: str, path: str) -> None:
        if key not in mapping:
            errors.append(f"{path}: missing {key}")
        elif not isinstance(mapping[key], str):
            errors.append(f"{path}.{key}: expected str")

    def validate_buttons(value: Any, path: str) -> None:
        if not isinstance(value, list) or not value:
            errors.append(f"{path}: expected non-empty list")
            return
        for button_index, button in enumerate(value):
            button_path = f"{path}[{button_index}]"
            if not isinstance(button, dict):
                errors.append(f"{button_path}: expected dict")
                continue
            require_string(button, "text", button_path)
            callback_data = button.get("callback_data")
            url = button.get("url")
            if callback_data is not None:
                if not isinstance(callback_data, str):
                    errors.append(f"{button_path}.callback_data: expected str")
                elif not callback_data:
                    errors.append(f"{button_path}.callback_data: must not be empty")
                elif len(callback_data.encode("utf-8")) > 64:
                    errors.append(f"{button_path}.callback_data: exceeds 64-byte limit")
            if url is not None and not isinstance(url, str):
                errors.append(f"{button_path}.url: expected str")
            if url is not None and isinstance(url, str) and not url:
                errors.append(f"{button_path}.url: must not be empty")
            if callback_data is None and url is None:
                errors.append(f"{button_path}: requires callback_data or url")

    for index, block in enumerate(blocks):
        path = f"block[{index}]"
        if not isinstance(block, dict):
            errors.append(f"{path}: not a dict ({type(block).__name__})")
            continue

        block_type = block.get("type")
        if not isinstance(block_type, str):
            errors.append(f"{path}: missing/!str type")
            continue
        if block_type not in _VALID_BLOCK_TYPES:
            errors.append(f"{path}: unknown type {block_type!r}")
            continue

        if block_type in {"heading", "paragraph", "quote", "code", "markdown", "spoiler"}:
            require_string(block, "text", path)
            # FIX_PM_RV2_9: validator now mirrors heading builder constraints.
            if block_type == "heading" and "size" in block:
                if isinstance(block["size"], bool) or not isinstance(block["size"], int):
                    errors.append(f"{path}.size: expected int")
                elif block["size"] not in (1, 2):
                    errors.append(f"{path}.size: must be 1 or 2")
        elif block_type == "details":
            require_string(block, "summary", path)
            require_string(block, "text", path)
        elif block_type in _STRIPPABLE_BLOCK_TYPES:
            media = block.get(block_type)
            if not isinstance(media, dict):
                errors.append(f"{path}.{block_type}: expected dict")
            else:
                if media.get("type") != block_type:
                    errors.append(f"{path}.{block_type}.type: expected {block_type!r}")
                require_string(media, "media", f"{path}.{block_type}")
        elif block_type == "buttons":
            validate_buttons(block.get("buttons"), f"{path}.buttons")
            if "widths" in block:
                # FIX_PM_RV2_9: widths must match the builder's positive-integer contract.
                widths = block["widths"]
                if not isinstance(widths, list) or any(
                    isinstance(width, bool) or not isinstance(width, int) or width <= 0
                    for width in widths
                ):
                    errors.append(f"{path}.widths: expected list of positive ints")
        elif block_type == "list":
            items = block.get("items")
            if not isinstance(items, list):
                errors.append(f"{path}.items: expected list")
            elif any(not isinstance(item, str) for item in items):
                errors.append(f"{path}.items: every item must be str")
        elif block_type == "checklist":
            items = block.get("items")
            if not isinstance(items, list):
                errors.append(f"{path}.items: expected list")
            else:
                for item_index, item in enumerate(items):
                    item_path = f"{path}.items[{item_index}]"
                    if not isinstance(item, dict):
                        errors.append(f"{item_path}: expected dict")
                        continue
                    require_string(item, "text", item_path)
                    if not isinstance(item.get("checked"), bool):
                        errors.append(f"{item_path}.checked: expected bool")
        elif block_type == "table":
            cells = block.get("cells")
            if not isinstance(cells, list) or not cells:
                errors.append(f"{path}.cells: expected non-empty list")
            else:
                for row_index, row in enumerate(cells):
                    row_path = f"{path}.cells[{row_index}]"
                    if not isinstance(row, list) or not row:
                        errors.append(f"{row_path}: expected non-empty list")
                        continue
                    for cell_index, cell in enumerate(row):
                        cell_path = f"{row_path}[{cell_index}]"
                        if not isinstance(cell, dict):
                            errors.append(f"{cell_path}: expected dict")
                        else:
                            require_string(cell, "text", cell_path)
        elif block_type == "spacer":
            height = block.get("height")
            if isinstance(height, bool) or not isinstance(height, int):
                errors.append(f"{path}.height: expected int")
            elif not 0 <= height <= 200:
                # FIX_PM_RV2_9: enforce the builder's maximum spacer height.
                errors.append(f"{path}.height: must be between 0 and 200")

    return errors


def _is_transient(exc: BaseException) -> bool:
    """True if exc is likely transient (retry-worthy)."""
    if isinstance(exc, RichMessageValidationError):
        # FIX_PM_RV2_14: caller validation errors are never retry-worthy transport failures.
        return False
    if isinstance(exc, RichMessageError):
        message = str(exc).lower()
        return any(marker in message for marker in _TRANSIENT_MARKERS)
    if isinstance(exc, (requests.RequestException, ConnectionError, TimeoutError, OSError)):
        return True
    return False


def _normalize_buttons(buttons: Any) -> List[Dict[str, Any]]:
    """Accept button_row blocks, bare button dicts, tuple forms, or lists of tuples.

    Unknown entries raise TypeError.
    """
    # FIX_PM_RV3_6: document bare-dict support and TypeError behavior in the public helper contract.
    if buttons is None or buttons == []:
        return []
    if isinstance(buttons, tuple) and buttons and isinstance(buttons[0], str):
        if len(buttons) < 2:
            raise ValueError("button tuple must contain at least label and callback_data")
        buttons = [buttons]

    out: List[Dict[str, Any]] = []
    for entry in buttons:
        if isinstance(entry, dict) and entry.get("type") == "buttons":
            out.append(entry)
            # FIX_PM_RV2_1: bare button dicts become a real one-button row instead of disappearing.
            continue
        elif isinstance(entry, dict):
            out.append(button_row([entry]))
            continue
        if isinstance(entry, (list, tuple)):
            row: List[Dict[str, Any]] = []
            if len(entry) >= 2 and not isinstance(entry[0], (dict, list, tuple)):
                label, data = entry[0], entry[1]
                style = entry[2] if len(entry) > 2 else "primary"
                row.append(rich_callback_button(label, data, style))
            else:
                for item in entry:
                    if isinstance(item, dict):
                        row.append(item)
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        label, data = item[0], item[1]
                        style = item[2] if len(item) > 2 else "primary"
                        row.append(rich_callback_button(label, data, style))
            if row:
                out.append(button_row(row))
            else:
                raise TypeError(f"unsupported button entry: {type(entry).__name__}")
        else:
            raise TypeError(f"unsupported button entry: {type(entry).__name__}")
    return out


def success_card(title: str, body: str, buttons: Any = None, footer: Optional[str] = None) -> List[Dict[str, Any]]:
    """Build a success card."""
    blocks: List[Dict[str, Any]] = [heading(f"✅ {title}"), paragraph(str(body))]
    if footer:
        blocks.append(paragraph(str(footer)))
    if buttons:
        blocks.extend(_normalize_buttons(buttons))
    return blocks


def error_card(title: str, body: str, hint: Optional[str] = None, buttons: Any = None, retry: Any = None) -> List[Dict[str, Any]]:
    """Build an error card, optionally with a retry callback button."""
    blocks: List[Dict[str, Any]] = [heading(f"❌ {title}"), paragraph(str(body))]
    if hint:
        blocks.append(paragraph(f"💡 {hint}"))
    if retry is not None:
        if isinstance(retry, dict):
            if "text" not in retry or "callback_data" not in retry:
                raise ValueError("retry dict must contain 'text' and 'callback_data'")
            label, data = retry["text"], retry["callback_data"]
            style = retry.get("style") or "primary"
        elif isinstance(retry, (tuple, list)):
            if len(retry) < 2:
                raise ValueError("retry tuple/list must contain label and callback_data")
            label, data = retry[0], retry[1]
            style = retry[2] if len(retry) > 2 and retry[2] else "primary"
        else:
            raise TypeError("retry must be a dict or tuple/list")
        blocks.append(button_row([rich_callback_button(label, data, style)]))
    if buttons:
        blocks.extend(_normalize_buttons(buttons))
    return blocks


def progress_card(title: str, rows: Sequence[Sequence[Any]], buttons: Any = None) -> List[Dict[str, Any]]:
    """Build a progress card."""
    blocks: List[Dict[str, Any]] = [heading(f"⏳ {title}"), compact_table(["Field", "Value"], rows)]
    if buttons:
        blocks.extend(_normalize_buttons(buttons))
    return blocks


def stats_card(title: str, rows: Sequence[Sequence[Any]], buttons: Any = None) -> List[Dict[str, Any]]:
    """Build a statistics card."""
    blocks: List[Dict[str, Any]] = [heading(title), compact_table(["Metric", "Value"], rows)]
    if buttons:
        blocks.extend(_normalize_buttons(buttons))
    return blocks


def info_card(title: str, body: str, bullets: Optional[Sequence[str]] = None, buttons: Any = None) -> List[Dict[str, Any]]:
    """Build an information card."""
    blocks: List[Dict[str, Any]] = [heading(title), paragraph(str(body))]
    if bullets:
        blocks.append(bullet_list(bullets))
    if buttons:
        blocks.extend(_normalize_buttons(buttons))
    return blocks


def confirm_card(title: str, body: str, yes_label: str, yes_data: str, no_label: str = "❌ Cancel", no_data: str = "task_cancel") -> List[Dict[str, Any]]:
    """Build a yes/no confirmation card."""
    return [
        heading(title), paragraph(str(body)),
        button_row([
            rich_callback_button(yes_label, yes_data, "success"),
            rich_callback_button(no_label, no_data, "danger"),
        ]),
    ]


def wizard_card(step: int, total: int, title: str, body: str, buttons: Any = None, progress: Any = None) -> List[Dict[str, Any]]:
    """Build a wizard-step card."""
    blocks: List[Dict[str, Any]] = [heading(title), paragraph(f"Step {step}/{total} · {body}")]
    if progress is not None:
        blocks.append(compact_table(["Field", "Value"], [["Progress", f"{step}/{total}"], ["Status", str(progress)]]))
    if buttons:
        blocks.extend(_normalize_buttons(buttons))
    return blocks


def section_card(title: str, description: str, markup_or_buttons: Any, tail: Optional[str] = None) -> List[Dict[str, Any]]:
    """Build a section card from PTB-style markup or portable buttons."""
    blocks: List[Dict[str, Any]] = [heading(title), divider(), paragraph(str(description))]
    if tail:
        blocks.append(paragraph(str(tail)))
    if markup_or_buttons is not None:
        if hasattr(markup_or_buttons, "inline_keyboard"):
            for row in markup_or_buttons.inline_keyboard:
                row_buttons: List[Dict[str, Any]] = []
                for button in row:
                    button_url = getattr(button, "url", None)
                    if button_url:
                        row_buttons.append(rich_url_button(button.text, button_url, "primary"))
                    else:
                        callback_data = getattr(button, "callback_data", None)
                        if callback_data is None:
                            # FIX_PM_RV3_7: callers must now provide url/callback_data; old "menu" fallback is gone.
                            # FIX_PM_RV2_13: never invent a "menu" callback for malformed PTB buttons.
                            raise ValueError("PTB button requires url or callback_data")
                        row_buttons.append(rich_callback_button(button.text, callback_data, "primary"))
                if row_buttons:
                    blocks.append(button_row(row_buttons))
        else:
            blocks.extend(_normalize_buttons(markup_or_buttons))
    return blocks


def _escape_markdown_text(text: str) -> str:
    """Escape Telegram Markdown v1 special characters in plain fallback text."""
    # FIX_PM_RV3_3: escape literal backslashes before Markdown punctuation.
    text = str(text).replace("\\", "\\\\")
    return "".join("\\" + ch if ch in r"_*[]`" else ch for ch in text)


def blocks_to_markdown_text(blocks: Sequence[Dict[str, Any]]) -> str:
    """Render blocks using the legacy Markdown formatting behavior."""
    parts: List[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "heading":
            parts.append(f"*{block.get('text', '')}*")
        elif block_type == "paragraph":
            parts.append(str(block.get("text", "")))
        elif block_type == "divider":
            parts.append("―" * 12)
        elif block_type == "spacer":
            continue
        elif block_type == "quote":
            author = block.get("author")
            parts.append(f"> {block.get('text', '')}" + (f" — {author}" if author else ""))
        elif block_type == "code":
            language = block.get("language")
            text = str(block.get("text", ""))
            parts.append(f"```{language or ''}\n{text}\n```")
        elif block_type == "markdown":
            parts.append(str(block.get("text", "")))
        elif block_type == "list":
            parts.extend(f"• {item}" for item in block.get("items", []) or [])
        elif block_type == "checklist":
            for item in block.get("items", []) or []:
                if isinstance(item, dict):
                    mark = "☑" if item.get("checked") else "☐"
                    parts.append(f"{mark} {item.get('text', '')}")
        elif block_type == "table":
            for row in block.get("cells", []) or []:
                line = " | ".join(str(cell.get("text", "")) for cell in row if isinstance(cell, dict))
                if line.strip():
                    parts.append(line)
        elif block_type == "details":
            parts.append(f"▸ {block.get('summary', '')}")
            parts.append(str(block.get("text", "")))
        elif block_type == "spoiler":
            parts.append("(spoiler)")
        elif block_type == "video":
            parts.append("🎬 (video)")
        elif block_type == "photo":
            parts.append("🖼️ (photo)")
        elif block_type == "animation":
            parts.append("🎞️ (animation)")
        elif block_type == "audio":
            parts.append("🔊 (audio)")
        elif block_type == "document":
            parts.append("📄 (document)")
        elif block_type == "buttons":
            continue
        elif "text" in block:
            parts.append(str(block["text"]))
    return "\n".join(parts).strip()


# FIX_PM_RV2_8: keep legacy Markdown rendering separate from the safe no-parse fallback.
def blocks_to_plain_text(
    blocks: Sequence[Dict[str, Any]], *, escape_markdown: bool = True
) -> str:
    """Render blocks structurally as plain text without Markdown formatting.

    By default, escape_markdown=True escapes Telegram Markdown v1 characters for
    callers that will feed the output through a Markdown parser. No-parse_mode
    callers, such as replace() stage 2, must pass escape_markdown=False.
    """
    # FIX_PM_RV3_4: render block types directly so user punctuation is never mistaken for renderer formatting.
    parts: List[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "heading":
            parts.append(str(block.get("text", "")))
        elif block_type == "paragraph":
            parts.append(str(block.get("text", "")))
        elif block_type == "divider":
            parts.append("―" * 12)
        elif block_type == "spacer":
            continue
        elif block_type == "quote":
            author = block.get("author")
            text = f"> {block.get('text', '')}"
            if author:
                text += f" — {author}"
            parts.append(text)
        elif block_type == "code":
            parts.append(str(block.get("text", "")))
        elif block_type == "markdown":
            parts.append(str(block.get("text", "")))
        elif block_type == "list":
            parts.extend(f"• {item}" for item in block.get("items", []) or [])
        elif block_type == "checklist":
            for item in block.get("items", []) or []:
                if isinstance(item, dict):
                    mark = "☑" if item.get("checked") else "☐"
                    parts.append(f"{mark} {item.get('text', '')}")
        elif block_type == "table":
            for row in block.get("cells", []) or []:
                line = " | ".join(str(cell.get("text", "")) for cell in row if isinstance(cell, dict))
                if line.strip():
                    parts.append(line)
        elif block_type == "details":
            parts.append(f"▸ {block.get('summary', '')}")
            parts.append(str(block.get("text", "")))
        elif block_type == "spoiler":
            parts.append("(spoiler)")
        elif block_type == "video":
            parts.append("🎬 (video)")
        elif block_type == "photo":
            parts.append("🖼️ (photo)")
        elif block_type == "animation":
            parts.append("🎞️ (animation)")
        elif block_type == "audio":
            parts.append("🔊 (audio)")
        elif block_type == "document":
            parts.append("📄 (document)")
        elif block_type == "buttons":
            continue
        elif "text" in block:
            parts.append(str(block["text"]))
    text = "\n".join(parts).strip()
    return _escape_markdown_text(text) if escape_markdown else text


def blocks_to_inline_keyboard(blocks: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Extract inline_keyboard from 'buttons' blocks."""
    rows: List[List[Dict[str, Any]]] = []
    for block in blocks or []:
        if not isinstance(block, dict) or block.get("type") != "buttons":
            continue
        row: List[Dict[str, Any]] = []
        for button in block.get("buttons", []) or []:
            if not isinstance(button, dict):
                continue
            item: Dict[str, Any] = {"text": str(button.get("text", ""))}
            if button.get("callback_data") is not None:
                item["callback_data"] = str(button["callback_data"])
            elif button.get("url") is not None:
                item["url"] = str(button["url"])
            row.append(item)
        if row:
            rows.append(row)
    return {"inline_keyboard": rows} if rows else None


def extract_message_id(resp: Any) -> Optional[int]:
    """Extract message_id from either an API response dict or PTB Message object."""
    if isinstance(resp, dict):
        inner = resp.get("result", resp)
        if isinstance(inner, dict):
            value = inner.get("message_id")
            try:
                return int(value) if value is not None else None
            except (TypeError, ValueError):
                return None
        return None
    value = getattr(resp, "message_id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def build_photo_payload(
    chat_id: Any, photo_url: str, caption: Optional[str] = None,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a standard sendPhoto payload."""
    payload: Dict[str, Any] = {"chat_id": chat_id, "photo": str(photo_url)}
    if caption is not None:
        payload["caption"] = str(caption)
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return payload


def build_document_payload(
    chat_id: Any, document_url: str, caption: Optional[str] = None,
    reply_markup: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a standard sendDocument payload."""
    payload: Dict[str, Any] = {"chat_id": chat_id, "document": str(document_url)}
    if caption is not None:
        payload["caption"] = str(caption)
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return payload


class _CircuitBreaker:
    """Per-client circuit breaker."""

    def __init__(self, threshold: int, cooldown: int) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown = max(1, int(cooldown))
        self.fails = 0
        self.opened_at = 0.0

    def is_open(self) -> bool:
        # NOTE: opened_at=0.0 is the "not open" sentinel; this mutates state without a
        # lock, which is acceptable because extra half-open probes are harmless.
        if self.fails < self.threshold:
            return False
        if time.monotonic() - self.opened_at > self.cooldown:
            # BUGFIX: also re-arm opened_at (not just fails). record_failure()
            # only ever sets opened_at when it's the sentinel 0.0, so leaving
            # it at its original timestamp meant the "cooldown expired" check
            # above stayed permanently true after the first cooldown window —
            # the breaker could reset to a half-open probe but could never
            # fully re-open again during a sustained outage, since the very
            # next failure had nowhere to record a fresh open timestamp.
            self.fails = self.threshold - 1
            self.opened_at = 0.0
            return False
        return True

    def record_success(self) -> None:
        self.fails = 0
        self.opened_at = 0.0

    def record_failure(self) -> None:
        self.fails += 1
        if self.fails >= self.threshold and self.opened_at == 0.0:
            self.opened_at = time.monotonic()


class RichClient:
    """Portable rich-message client with all mutable state per instance."""

    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://api.telegram.org",
        timeout: int = 15,
        max_attempts: int = 3,
        backoff_base: float = 0.4,
        backoff_max: float = 4.0,
        max_concurrent: int = 8,
        circuit_threshold: int = 5,
        circuit_cooldown: int = 60,
        debug: bool = False,
    ) -> None:
        """Create a client. Numeric parameters are clamped to safe minimums."""
        if not token:
            raise ValueError("RichClient: token is required")
        self.token = str(token)
        self.api_base = str(api_base).rstrip("/")
        self.timeout = max(1, int(timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_base = max(0.0, float(backoff_base))
        self.backoff_max = max(self.backoff_base, float(backoff_max))
        self.max_concurrent = max(1, int(max_concurrent))
        self.circuit_threshold = max(1, int(circuit_threshold))
        self.circuit_cooldown = max(1, int(circuit_cooldown))
        self.debug = bool(debug)

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "richmsg/1.2"})
        # FIX_PM_6: one lock keeps the shared requests.Session safe; simpler, but serializes HTTP.
        self._session_lock = threading.Lock()
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16, max_retries=0)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        self._breaker = _CircuitBreaker(self.circuit_threshold, self.circuit_cooldown)
        self._chat_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self._chat_locks_guard: Optional[asyncio.Lock] = None
        self._chat_locks_loop: Any = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._semaphore_loop: Any = None
        self._metrics: Dict[str, int] = {
            "sent": 0,
            "failed": 0,
            "fallback": 0,
            "circuit_skips": 0,
            "retry_attempts": 0,
            "stage_retries": 0,
        }

    def _get_semaphore(self) -> asyncio.Semaphore:
        """Return a semaphore bound to the active loop, rebuilding after loop changes."""
        loop = asyncio.get_running_loop()
        # FIX_PM_5: loop affinity prevents reuse across separate asyncio.run() calls.
        if self._semaphore is None or self._semaphore_loop is not loop:
            self._semaphore = asyncio.Semaphore(self.max_concurrent)
            # FIX_PM_RV3_8: retain the loop strongly to avoid id(loop) address reuse.
            self._semaphore_loop = loop
        return self._semaphore

    async def _get_chat_locks_guard(self) -> asyncio.Lock:
        """Return a loop-affine guard; clear per-chat locks when the event loop changes."""
        loop = asyncio.get_running_loop()
        # FIX_PM_5: guard and weak locks cannot safely cross event-loop boundaries.
        if self._chat_locks_guard is None or self._chat_locks_loop is not loop:
            self._chat_locks_guard = asyncio.Lock()
            # FIX_PM_RV3_8: strong loop reference lasts until client discard or loop replacement.
            self._chat_locks_loop = loop
            self._chat_locks = WeakValueDictionary()
        return self._chat_locks_guard

    def _call_multipart(self, method: str, payload: Dict[str, Any], file_field: str, file_obj: Any) -> Dict[str, Any]:
        """POST a Telegram Bot API request using multipart/form-data for local files."""
        url = f"{self.api_base}/bot{self.token}/{method}"
        response: Any = None
        try:
            if self.debug:
                logger.debug("MULTIPART POST %s field=%s payload=%r", self._redact(url), file_field, self._redact_payload(payload))
            with self._session_lock:
                response = self._session.post(url, data=payload, files={file_field: file_obj}, timeout=self.timeout)
            try:
                data = response.json()
            except Exception as exc:
                raise RichMessageError(f"{method} returned invalid JSON (HTTP {response.status_code}): {exc}") from exc
        except RichMessageError:
            raise
        except requests.RequestException as exc:
            raise RichMessageError(f"{method} request failed: {type(exc).__name__}: {exc}") from exc
        except Exception as exc:
            # FIX_PM_7: normalize non-requests transport failures so retry classification can run.
            raise RichMessageError(f"{method} request failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            description = data.get("description") if isinstance(data, dict) else None
            retry_after = None
            if response.status_code == 429:
                try: retry_after = float(response.headers.get("Retry-After", 0)) or None
                except (TypeError, ValueError): pass
            raise RichMessageError(f"{description or f'{method} failed'} (HTTP {response.status_code})", retry_after=retry_after)
        result = data.get("result")
        return result if isinstance(result, dict) else {"result": result}

    async def send_local_file(self, chat_id: int, file_path: str, *, method: str = "sendDocument", field_name: Optional[str] = None, caption: Optional[str] = None, reply_to_message_id: Optional[int] = None, disable_notification: bool = False) -> Dict[str, Any]:
        """Send a local file directly to Telegram using multipart upload."""
        if not isinstance(file_path, str) or not file_path: raise TypeError("file_path must be a non-empty str")
        if method not in {"sendDocument", "sendPhoto", "sendVideo", "sendAnimation", "sendAudio"}: raise ValueError("unsupported local-file method")
        if field_name is None:
            field_name = {
                "sendDocument": "document",
                "sendPhoto": "photo",
                "sendVideo": "video",
                "sendAnimation": "animation",
                "sendAudio": "audio",
            }[method]
        if not os.path.isfile(file_path): raise FileNotFoundError(file_path)
        if caption is not None and not isinstance(caption, str): raise TypeError("caption must be str or None")
        if reply_to_message_id is not None and (isinstance(reply_to_message_id, bool) or not isinstance(reply_to_message_id, int)): raise TypeError("reply_to_message_id must be int or None")
        if self._circuit_open(): self._metrics["circuit_skips"] += 1; raise RichMessageError("rich circuit open")
        payload: Dict[str, Any] = {"chat_id": chat_id}
        if caption is not None: payload["caption"] = caption
        if reply_to_message_id is not None: payload["reply_to_message_id"] = reply_to_message_id
        if disable_notification: payload["disable_notification"] = True
        delay = self.backoff_base
        for attempt in range(self.max_attempts):
            try:
                # FIX_PM_RV3_2: multipart retries also release the semaphore before backoff.
                async with self._get_semaphore():
                    with open(file_path, "rb") as fh:
                        response = await asyncio.to_thread(self._call_multipart, method, payload, field_name, fh)
                    self._metrics["sent"] += 1; self._record_success(); return response
            except RichMessageError as exc:
                if not _is_transient(exc) or attempt == self.max_attempts - 1:
                    self._metrics["failed"] += 1; self._record_failure(); raise
                self._metrics["retry_attempts"] += 1
                retry_after = getattr(exc, "retry_after", None)
                sleep_for = max(delay, float(retry_after)) if retry_after else delay
                if sleep_for > 0: await asyncio.sleep(sleep_for)
                delay = min(delay * 2.0, self.backoff_max)
        raise RichMessageError("local file send failed")

    async def send_document_file(self, chat_id: int, file_path: str, **kwargs: Any) -> Dict[str, Any]:
        """Convenience wrapper for sending a local document/PDF."""
        return await self.send_local_file(chat_id, file_path, method="sendDocument", field_name="document", **kwargs)

    async def send_photo_file(self, chat_id: int, file_path: str, **kwargs: Any) -> Dict[str, Any]:
        """Convenience wrapper for sending a local photo."""
        return await self.send_local_file(chat_id, file_path, method="sendPhoto", field_name="photo", **kwargs)

    def _call_api(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST to Telegram Bot API and return its result dictionary."""
        url = f"{self.api_base}/bot{self.token}/{method}"
        response: Any = None
        try:
            if self.debug:
                logger.debug("POST %s payload=%r", self._redact(url), self._redact_payload(payload))
            with self._session_lock:
                response = self._session.post(url, json=payload, timeout=self.timeout)
            try:
                data = response.json()
            except Exception as exc:
                if self.debug:
                    logger.debug("raw response body=%r", getattr(response, "text", None))
                raise RichMessageError(
                    f"{method} returned invalid JSON (HTTP {response.status_code}): {exc}"
                ) from exc
        except RichMessageError:
            raise
        except requests.RequestException as exc:
            if self.debug:
                logger.debug("raw response body=%r", getattr(response, "text", None))
            raise RichMessageError(
                f"{method} request failed: {type(exc).__name__}: {exc}"
            ) from exc
        except Exception as exc:
            if self.debug:
                logger.debug("raw response body=%r", getattr(response, "text", None))
            raise RichMessageError(
                f"{method} request failed: {type(exc).__name__}: {exc}"
            ) from exc

        if self.debug:
            logger.debug("response HTTP %s JSON=%r", response.status_code, self._redact_payload(data) if isinstance(data, dict) else data)

        if not isinstance(data, dict) or not data.get("ok"):
            description = data.get("description") if isinstance(data, dict) else None
            retry_after: Optional[float] = None
            if response.status_code == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", 0)) or None
                except (TypeError, ValueError):
                    retry_after = None
            raise RichMessageError(
                f"{description or f'{method} failed'} (HTTP {response.status_code})",
                retry_after=retry_after,
            )

        result = data.get("result")
        return result if isinstance(result, dict) else {"result": result}

    def _redact_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Redact chat_id and truncate text-like fields for debug logging."""
        # FIX_PM_RV2_15: keep debug logs useful without exposing chat ids or huge text fields.
        def clean(value: Any, key: str = "") -> Any:
            if isinstance(value, dict):
                return {k: clean(v, str(k)) for k, v in value.items()}
            if isinstance(value, list):
                return [clean(v, key) for v in value]
            if key == "chat_id":
                return "<CHAT_ID>"
            if isinstance(value, str) and key.lower() in {"text", "caption", "callback_data", "url"}:
                return value[:160] + ("…" if len(value) > 160 else "")
            return value
        return clean(payload)

    def _redact(self, url: str) -> str:
        """Replace this client's token with <TOKEN> in a URL."""
        return str(url).replace(self.token, "<TOKEN>")

    def _circuit_open(self) -> bool:
        return self._breaker.is_open()

    def _record_success(self) -> None:
        self._breaker.record_success()

    def _record_failure(self) -> None:
        self._breaker.record_failure()

    async def _chat_lock(self, chat_id: int) -> asyncio.Lock:
        """Return an instance-local weak per-chat lock.

        Weak-lock semantics mean disjoint operations may obtain different lock objects
        for the same chat; serialization is guaranteed only for overlapping operations.
        """
        guard = await self._get_chat_locks_guard()
        async with guard:
            lock = self._chat_locks.get(chat_id)
            if lock is None:
                lock = asyncio.Lock()
                self._chat_locks[chat_id] = lock
            # FIX_PM_RV2_16: weak locks only serialize overlapping operations sharing the same live lock.
            return lock

    async def _send_rich_via_placeholder(self, chat_id: int, blocks: List[Dict[str, Any]], *, reply_to_message_id: Optional[int] = None, disable_notification: bool = False) -> Dict[str, Any]:
        """Send a rich card via placeholder message followed by rich edit."""
        placeholder_payload: Dict[str, Any] = {"chat_id": chat_id, "text": "\u2063"}
        if reply_to_message_id is not None:
            placeholder_payload["reply_to_message_id"] = int(reply_to_message_id)
        if disable_notification:
            placeholder_payload["disable_notification"] = True
        placeholder = await asyncio.to_thread(self._call_api, "sendMessage", placeholder_payload)
        message_id = extract_message_id(placeholder)
        if not message_id:
            raise RichMessageError("placeholder send returned no message_id")
        try:
            return await asyncio.to_thread(self._call_api, "editMessageText", {
                "chat_id": chat_id, "message_id": message_id, "rich_message": {"blocks": blocks}
            })
        except Exception:
            # The placeholder is already visible if the rich edit fails. Best-effort
            # cleanup prevents failed rich attempts from leaving invisible messages.
            try:
                await asyncio.to_thread(
                    self._call_api,
                    "deleteMessage",
                    {"chat_id": chat_id, "message_id": message_id},
                )
            except Exception:
                logger.debug("placeholder cleanup skipped for message_id=%s", message_id)
            raise

    async def _send_with_retry(
        self,
        chat_id: int,
        blocks: List[Dict[str, Any]],
        *,
        count_failure: bool = True,
        reply_to_message_id: Optional[int] = None,
        disable_notification: bool = False,
    ) -> Dict[str, Any]:
        """Retry transient rich sends using placeholder + rich edit."""
        delay = self.backoff_base
        last_exc: Optional[RichMessageError] = None
        for attempt in range(self.max_attempts):
            try:
                # FIX_PM_RV2_12: acquire a concurrency slot per attempt so backoff does not hold it.
                async with self._get_semaphore():
                    response = await self._send_rich_via_placeholder(
                        chat_id, blocks,
                        reply_to_message_id=reply_to_message_id,
                        disable_notification=disable_notification,
                    )
                self._metrics["sent"] += 1
                self._record_success()
                return response
            except RichMessageError as exc:
                last_exc = exc
                final = not _is_transient(exc) or attempt == self.max_attempts - 1
                if final:
                    if count_failure:
                        self._metrics["failed"] += 1
                        # FIX_PM_2: opt-out now skips both failure metrics and breaker state.
                        self._record_failure()
                    raise
                self._metrics["retry_attempts"] += 1
                retry_after = getattr(exc, "retry_after", None)
                sleep_for = max(delay, float(retry_after)) if retry_after else delay
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                delay = min(delay * 2.0, self.backoff_max)
        raise last_exc or RichMessageError("rich send failed")

    async def _send_with_media_strip(
        self,
        chat_id: int,
        blocks: List[Dict[str, Any]],
        *,
        reply_to_message_id: Optional[int] = None,
        disable_notification: bool = False,
    ) -> Dict[str, Any]:
        """Retry once without media after a transient send failure.

        reply_to_message_id and disable_notification are forwarded to every stage.
        """
        if reply_to_message_id is not None:
            _require_int(reply_to_message_id, "reply_to_message_id")  # FIX_PM_RV3_9: defense in depth; public callers already validate.
        if not isinstance(disable_notification, bool):
            raise TypeError("disable_notification must be bool")  # FIX_PM_RV3_9: defense in depth; public callers already validate.
        try:
            return await self._send_with_retry(
                chat_id, blocks, count_failure=False,
                reply_to_message_id=reply_to_message_id,
                disable_notification=disable_notification,
            )
        except RichMessageError as exc:
            if not _is_transient(exc):
                self._metrics["failed"] += 1
                raise
            stripped = [
                block for block in blocks
                if not (isinstance(block, dict) and block.get("type") in _STRIPPABLE_BLOCK_TYPES)
            ]
            if len(stripped) == len(blocks):
                self._metrics["failed"] += 1
                raise
            self._metrics["stage_retries"] += 1
            logger.warning("retrying without %d media block(s) after transient", len(blocks) - len(stripped))
            return await self._send_with_retry(
                chat_id, stripped, count_failure=True,
                reply_to_message_id=reply_to_message_id,
                disable_notification=disable_notification,
            )

    async def _request_with_retry(
        self,
        method: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Retry a standard Bot API request without holding a semaphore during backoff."""
        delay = self.backoff_base
        for attempt in range(self.max_attempts):
            try:
                # FIX_PM_RV3_2: acquire the concurrency slot only around each network attempt.
                async with self._get_semaphore():
                    response = await asyncio.to_thread(self._call_api, method, payload)
                self._metrics["sent"] += 1
                self._record_success()
                return response
            except RichMessageError as exc:
                if not _is_transient(exc) or attempt == self.max_attempts - 1:
                    self._metrics["failed"] += 1
                    self._record_failure()
                    raise
                self._metrics["retry_attempts"] += 1
                retry_after = getattr(exc, "retry_after", None)
                sleep_for = max(delay, float(retry_after)) if retry_after else delay
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                delay = min(delay * 2.0, self.backoff_max)
        raise RichMessageError(f"{method} failed")

    async def send(
        self,
        chat_id: int,
        blocks: Sequence[Dict[str, Any]],
        *,
        reply_to_message_id: Optional[int] = None,
        disable_notification: bool = False,
    ) -> Dict[str, Any]:
        """Validate and send with retry.

        reply_to_message_id replies to that Telegram message; disable_notification
        suppresses notification delivery. The return value is the editMessageText
        result, not the placeholder sendMessage result; use tracked_send_or or
        extract_message_id when the visible message id is needed.
        """
        # FIX_PM_RV2_11: callers receive the rich edit result, not the placeholder response.
        block_list = list(blocks)
        if not block_list:
            # FIX_PM_RV2_9: reject empty block lists at public entry points.
            raise RichMessageValidationError("invalid blocks: blocks must be non-empty")
        if reply_to_message_id is not None:
            _require_int(reply_to_message_id, "reply_to_message_id")
        if not isinstance(disable_notification, bool):
            raise TypeError("disable_notification must be bool")
        # FIX_PM_RV2_4: validate and forward the newly reachable reply/notification controls.
        errors = validate_blocks(block_list)
        if errors:
            raise RichMessageValidationError(f"invalid blocks: {errors[0]}")
        if self._circuit_open():
            self._metrics["circuit_skips"] += 1
            raise RichMessageError("rich circuit open")
        # FIX_PM_RV3_5: send() stays unlocked; placeholder/rich flow has no ordering requirement.
        return await self._send_with_media_strip(
            chat_id, block_list,
            reply_to_message_id=reply_to_message_id,
            disable_notification=disable_notification,
        )

    async def replace(
        self,
        chat_id: int,
        message_id: int,
        blocks: Sequence[Dict[str, Any]],
        *,
        reply_to_message_id: Optional[int] = None,
        disable_notification: bool = False,
    ) -> Dict[str, Any]:
        """3-stage replace: rich edit -> plain edit -> send new + delete old.

        reply_to_message_id and disable_notification are forwarded to the stage-3
        new-message send. Stage 1/2 edit the existing message and therefore do not
        use reply_to_message_id.
        """
        # FIX_PM_RV2_4: stage 3 forwards reply_to_message_id and disable_notification.
        block_list = list(blocks)
        if not block_list:
            # FIX_PM_RV2_9: reject empty block lists at public entry points.
            raise RichMessageValidationError("invalid blocks: blocks must be non-empty")
        if reply_to_message_id is not None:
            _require_int(reply_to_message_id, "reply_to_message_id")
        if not isinstance(disable_notification, bool):
            raise TypeError("disable_notification must be bool")
        errors = validate_blocks(block_list)
        if errors:
            raise RichMessageValidationError(f"invalid blocks: {errors[0]}")
        if self._circuit_open():
            self._metrics["circuit_skips"] += 1
            raise RichMessageError("rich circuit open")

        lock = await self._chat_lock(chat_id)
        async with lock:
            try:
                # FIX_PM_RV2_12: each replace network stage releases its slot before any later work.
                async with self._get_semaphore():
                    response = await asyncio.to_thread(
                        self._call_api,
                        "editMessageText",
                        {"chat_id": chat_id, "message_id": message_id, "rich_message": {"blocks": block_list}},
                    )
                self._metrics["sent"] += 1
                self._record_success()
                return response
            except Exception as exc:
                self._metrics["stage_retries"] += 1
                logger.debug("rich edit failed (%s)", str(exc)[:120])

            try:
                reply_markup = blocks_to_inline_keyboard(block_list) or {"inline_keyboard": []}
                async with self._get_semaphore():
                    response = await asyncio.to_thread(
                        self._call_api,
                        "editMessageText",
                        {
                            "chat_id": chat_id,
                            "message_id": message_id,
                            # FIX_PM_RV3_1: no parse_mode means do not emit Markdown escape characters.
                            "text": blocks_to_plain_text(block_list, escape_markdown=False),
                            "reply_markup": reply_markup,
                        },
                    )
                self._metrics["sent"] += 1
                self._record_success()
                return response
            except Exception as exc:
                self._metrics["stage_retries"] += 1
                logger.debug("plain edit failed (%s)", str(exc)[:120])

            # FIX_PM_3: direct call removes a dead try/except Exception: raise wrapper.
            new_response = await self._send_with_media_strip(
                chat_id, block_list,
                reply_to_message_id=reply_to_message_id,
                disable_notification=disable_notification,
            )
            try:
                async with self._get_semaphore():
                    await asyncio.to_thread(
                        self._call_api, "deleteMessage",
                        {"chat_id": chat_id, "message_id": message_id},
                    )
            except Exception:
                logger.debug("old message delete skipped")
            return new_response

    async def send_standard(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """POST to any Telegram Bot API method with retry."""
        if not method or not isinstance(method, str):
            raise ValueError("method is required")
        if self._circuit_open():
            self._metrics["circuit_skips"] += 1
            raise RichMessageError("rich circuit open")
        # FIX_PM_RV3_2: _request_with_retry owns per-attempt semaphore acquisition.
        return await self._request_with_retry(method, dict(payload))

    async def edit_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: str = "Markdown",
    ) -> Dict[str, Any]:
        """Edit an existing message with plain text."""
        if self._circuit_open():
            self._metrics["circuit_skips"] += 1
            raise RichMessageError("rich circuit open")
        payload: Dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": str(text), "parse_mode": parse_mode}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        lock = await self._chat_lock(chat_id)
        async with lock:
            # FIX_PM_RV3_2: retry backoff occurs outside the semaphore.
            return await self._request_with_retry("editMessageText", payload)

    async def delete(self, chat_id: int, message_id: int) -> Dict[str, Any]:
        """Delete a message with retry + circuit breaker."""
        if self._circuit_open():
            self._metrics["circuit_skips"] += 1
            raise RichMessageError("rich circuit open")
        lock = await self._chat_lock(chat_id)
        async with lock:
            # FIX_PM_RV3_2: retry backoff occurs outside the semaphore.
            return await self._request_with_retry("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    async def send_or(
        self,
        chat_id: int,
        blocks: Sequence[Dict[str, Any]],
        fallback: Callable[..., Any],
        *,
        return_fallback_flag: bool = False,
    ) -> Any:
        """Try send; validation errors raise, all other errors use fallback."""
        try:
            response = await self.send(chat_id, blocks)
            return (response, False) if return_fallback_flag else response
        except RichMessageValidationError:
            logger.error("richmsg validation error — not falling back")
            raise
        except Exception as exc:
            self._metrics["fallback"] += 1
            if isinstance(exc, RichMessageError):
                logger.warning("rich send failed: %s", exc)
            else:
                logger.exception("rich send crashed")
            try:
                response = await fallback()
            except Exception:
                logger.exception("rich send fallback crashed")
                response = None
            return (response, True) if return_fallback_flag else response

    async def replace_or(
        self,
        chat_id: int,
        message_id: int,
        blocks: Sequence[Dict[str, Any]],
        fallback: Callable[..., Any],
        *,
        return_fallback_flag: bool = False,
    ) -> Any:
        """Try replace; validation errors raise, all other errors use fallback."""
        try:
            response = await self.replace(chat_id, message_id, blocks)
            return (response, False) if return_fallback_flag else response
        except RichMessageValidationError:
            logger.error("richmsg validation error — not falling back")
            raise
        except Exception as exc:
            self._metrics["fallback"] += 1
            if isinstance(exc, RichMessageError):
                logger.warning("rich replace failed: %s", exc)
            else:
                logger.exception("rich replace crashed")
            try:
                response = await fallback()
            except Exception:
                logger.exception("rich replace fallback crashed")
                response = None
            return (response, True) if return_fallback_flag else response

    async def tracked_send_or(
        self,
        context: Any,
        chat_id: int,
        blocks: Sequence[Dict[str, Any]],
        fallback: Callable[..., Any],
        track_key: str,
    ) -> Any:
        """send_or + store message_id in context.user_data[track_key]."""
        result = await self.send_or(chat_id, blocks, fallback)
        response = result[0] if isinstance(result, tuple) and len(result) == 2 else result
        message_id = extract_message_id(response)
        if message_id is not None and hasattr(context, "user_data"):
            context.user_data[track_key] = message_id
        return result

    async def tracked_replace_or(
        self,
        context: Any,
        chat_id: int,
        message_id: int,
        blocks: Sequence[Dict[str, Any]],
        fallback: Callable[..., Any],
        track_key: str,
    ) -> Any:
        """replace_or + store message_id in context.user_data[track_key]."""
        result = await self.replace_or(chat_id, message_id, blocks, fallback)
        response = result[0] if isinstance(result, tuple) and len(result) == 2 else result
        new_id = extract_message_id(response)
        if new_id is not None and hasattr(context, "user_data"):
            context.user_data[track_key] = new_id
        return result

    async def __aenter__(self) -> "RichClient":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the HTTP session."""
        try:
            self._session.close()
        except Exception:
            pass

    def __repr__(self) -> str:
        return (
            f"RichClient(token=<TOKEN>..., "
            f"sent={self._metrics['sent']}, "
            f"failed={self._metrics['failed']}, "
            f"circuit={'open' if self._circuit_open() else 'closed'})"
        )

    def metrics(self) -> Dict[str, int]:
        """Return a snapshot: sent/failed are final operations; retry_attempts counts extra attempts; stage_retries counts rich→plain→send transitions; fallback counts *_or fallback calls; circuit_skips counts calls rejected by an open circuit."""
        return dict(self._metrics)


__all__ = [
    "RichMessageError", "RichMessageValidationError",
    "heading", "paragraph", "video", "compact_table", "button_row",
    "divider", "spacer", "quote", "code", "markdown", "bullet_list",
    "checklist", "photo", "animation", "audio", "document", "details", "spoiler", "button_grid",
    "rich_callback_button", "rich_url_button",
    "validate_blocks",
    "success_card", "error_card", "progress_card", "stats_card", "info_card",
    "confirm_card", "wizard_card", "section_card",
    "blocks_to_plain_text", "blocks_to_markdown_text", "blocks_to_inline_keyboard", "extract_message_id",
    "build_photo_payload", "build_document_payload",
    "hr", "br", "p", "h",
    "RichClient",
]

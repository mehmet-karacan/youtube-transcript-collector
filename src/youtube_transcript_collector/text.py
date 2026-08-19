from __future__ import annotations

import html
import re
import unicodedata

INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
TIMING = re.compile(r"^\s*(?:\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+")
TAG = re.compile(r"<[^>]+>")


def truncate_utf8(value: str, budget: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= budget:
        return value
    return encoded[:budget].decode("utf-8", "ignore")


def safe_title(value: str, *, byte_budget: int = 140) -> str:
    value = unicodedata.normalize("NFC", value)
    value = "".join(character for character in value if unicodedata.category(character) != "Cf")
    value = re.sub(r"\s+", " ", value).strip()
    value = INVALID.sub("_", value).rstrip(" .") or "untitled"
    if value.upper() in RESERVED:
        value = f"_{value}"
    value = truncate_utf8(value, byte_budget).rstrip(" .") or "untitled"
    return value


def transcript_filename(date: str | None, video_id: str, title: str, *, budget: int = 220) -> str:
    date_part = date or "unknown-date"
    prefix = f"{date_part}_{video_id}_"
    title_budget = max(1, budget - len(prefix.encode()) - len(".txt"))
    return f"{prefix}{safe_title(title, byte_budget=title_budget)}.txt"


def vtt_to_text(raw: str) -> str:
    lines: list[str] = []
    previous = ""
    for source in raw.replace("\ufeff", "").splitlines():
        line = source.strip()
        if not line or line == "WEBVTT" or line.startswith(("NOTE", "Kind:", "Language:")):
            continue
        if TIMING.match(line) or line.isdigit():
            continue
        line = html.unescape(TAG.sub("", re.sub(r"<\d{2}:\d{2}:\d{2}[.,]\d{3}>", "", line))).strip()
        if line and line != previous:
            lines.append(line)
            previous = line
    return "\n".join(lines).strip() + ("\n" if lines else "")

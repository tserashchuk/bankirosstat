"""Постобработка отчёта: кликабельные ссылки на задачи YouTrack по ID в тексте."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_ISSUE_ID_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,15}-\d+)\b")


@dataclass(frozen=True)
class _YouTrackLinkRule:
    prefix: str
    base_url: str


def _normalize_base_url(url: str) -> str:
    return (url or "").strip().rstrip("/")


def build_youtrack_link_rules(snapshot: dict[str, Any] | None) -> list[_YouTrackLinkRule]:
    """Правила по префиксу ID (MOB, MYFIN) и базовому URL инстанса из snapshot."""
    if not snapshot:
        return []
    seen: set[tuple[str, str]] = set()
    rules: list[_YouTrackLinkRule] = []
    for block in snapshot.get("youtrack") or []:
        if not isinstance(block, dict):
            continue
        base = _normalize_base_url(str(block.get("instance_url") or ""))
        if not base:
            continue
        prefixes: set[str] = set()
        for issue in block.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            raw_id = str(issue.get("id") or "").strip()
            if not raw_id or "-" not in raw_id:
                continue
            if not _ISSUE_ID_RE.fullmatch(raw_id):
                continue
            prefixes.add(raw_id.split("-", 1)[0].upper())
        for prefix in sorted(prefixes, key=len, reverse=True):
            key = (prefix, base)
            if key in seen:
                continue
            seen.add(key)
            rules.append(_YouTrackLinkRule(prefix=prefix, base_url=base))
    return rules


def merge_youtrack_link_rules(*snapshots: dict[str, Any] | None) -> list[_YouTrackLinkRule]:
    """Объединяет правила из нескольких snapshot (портфель)."""
    seen: set[tuple[str, str]] = set()
    merged: list[_YouTrackLinkRule] = []
    for snapshot in snapshots:
        for rule in build_youtrack_link_rules(snapshot):
            key = (rule.prefix, rule.base_url)
            if key in seen:
                continue
            seen.add(key)
            merged.append(rule)
    return sorted(merged, key=lambda r: len(r.prefix), reverse=True)


def _issue_url(base_url: str, issue_id: str) -> str:
    return f"{_normalize_base_url(base_url)}/issue/{issue_id}"


def _linkify_segment(segment: str, rules: list[_YouTrackLinkRule]) -> str:
    if not segment or not rules:
        return segment

    by_prefix = {r.prefix: r for r in rules}

    def repl(match: re.Match[str]) -> str:
        issue_id = match.group(1)
        prefix = issue_id.split("-", 1)[0].upper()
        rule = by_prefix.get(prefix)
        if not rule:
            return match.group(0)
        return f"[{issue_id}]({_issue_url(rule.base_url, issue_id)})"

    return _ISSUE_ID_RE.sub(repl, segment)


def linkify_youtrack_ids(
    text: str,
    rules: list[_YouTrackLinkRule] | None = None,
    *,
    snapshot: dict[str, Any] | None = None,
) -> str:
    """
    Заменяет PROJ-42 на [PROJ-42](https://host/issue/PROJ-42).
    Не трогает фрагменты, уже оформленные как markdown-ссылки.
    """
    if not text:
        return text
    effective_rules = rules if rules is not None else build_youtrack_link_rules(snapshot)
    if not effective_rules:
        return text

    parts = re.split(r"(\[[^\]]*\]\([^)]*\))", text)
    out: list[str] = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            out.append(part)
        else:
            out.append(_linkify_segment(part, effective_rules))
    return "".join(out)

"""Small helpers for reading Eventor's XML with the standard library."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime

from eventor_client.exceptions import EventorParseError


def parse_document(body: bytes, expected_root: str | None = None) -> ET.Element:
    """Parse ``body`` and optionally check the root element's local name."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        snippet = body[:120].decode("utf-8", "replace").strip().replace("\n", " ")
        raise EventorParseError(f"response is not well-formed XML ({exc}): {snippet!r}") from exc
    if expected_root is not None and local_name(root.tag) != expected_root:
        raise EventorParseError(
            f"expected <{expected_root}> root element, got <{local_name(root.tag)}>"
        )
    return root


def local_name(tag: str) -> str:
    """Strip an XML namespace prefix (``{ns}Name`` -> ``Name``)."""
    return tag.rsplit("}", 1)[-1]


def child(el: ET.Element | None, name: str) -> ET.Element | None:
    """Return the first direct child called ``name`` (namespace-insensitive)."""
    if el is None:
        return None
    for c in el:
        if local_name(c.tag) == name:
            return c
    return None


def children(el: ET.Element | None, name: str) -> list[ET.Element]:
    if el is None:
        return []
    return [c for c in el if local_name(c.tag) == name]


def text(el: ET.Element | None, name: str | None = None) -> str | None:
    """Stripped text of ``el`` (or of its child ``name``); ``None`` when empty."""
    target = el if name is None else child(el, name)
    if target is None or target.text is None:
        return None
    value = target.text.strip()
    return value or None


def int_text(el: ET.Element | None, name: str | None = None) -> int | None:
    value = text(el, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def attr(el: ET.Element | None, name: str) -> str | None:
    if el is None:
        return None
    value = el.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def bool_attr(el: ET.Element | None, name: str, default: bool = False) -> bool:
    value = attr(el, name)
    if value is None:
        return default
    return value.lower() in {"true", "1", "yes"}


def date_clock(el: ET.Element | None) -> datetime | None:
    """Parse an element containing ``<Date>`` and optional ``<Clock>`` children.

    Eventor uses this shape for ``StartDate``, ``EntryDate``, ``ModifyDate``,
    ``RaceDate`` and friends. Times are in the instance's local time zone and are
    returned as naive datetimes.
    """
    if el is None:
        return None
    day = text(el, "Date")
    if day is None:
        return None
    clock = text(el, "Clock") or "00:00:00"
    try:
        return datetime.strptime(f"{day} {clock}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            return datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return None


def iso_datetime(value: str | None) -> datetime | None:
    """Parse an ``xs:dateTime`` string (as used by ``/memberships``)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None

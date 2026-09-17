"""HTTP client for the Eventor API.

Design notes
------------
* One ``httpx.Client`` per :class:`EventorClient`; use it as a context manager
  or call :meth:`EventorClient.close`.
* Every request carries the ``ApiKey`` header. Responses are XML.
* Eventor publishes no rate limit, so the client is deliberately gentle: an
  optional on-disk cache, exponential back-off with jitter on 5xx/429 and
  transport errors, and an optional minimum interval between requests.
* Only the endpoints a club typically needs are wrapped as methods. Anything
  else can be reached through :meth:`EventorClient.get_xml`, which returns the
  parsed root element; see the community spec for the full endpoint list.
"""

from __future__ import annotations

import hashlib
import logging
import random
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from datetime import date, datetime
from types import TracebackType
from typing import Self

import httpx

from eventor_client._xml import parse_document
from eventor_client.cache import ResponseCache
from eventor_client.exceptions import EventorAuthError, EventorError, EventorHTTPError
from eventor_client.models import Entry, Event, Membership, Organisation, Person, StartList
from eventor_client.parsing import (
    parse_entries,
    parse_events,
    parse_memberships,
    parse_organisation,
    parse_persons,
    parse_start_list,
)

AU_BASE_URL = "https://eventor.orienteering.asn.au/api"
SE_BASE_URL = "https://eventor.orientering.se/api"
NO_BASE_URL = "https://eventor.orientering.no/api"
IOF_BASE_URL = "https://eventor.orienteering.sport/api"

DEFAULT_USER_AGENT = (
    "eventor-client/0.1 (+https://github.com/JustinStafford/eventor-mailchimp-sync)"
)
# 520-524 are Cloudflare's transient origin errors; Eventor sits behind Cloudflare.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 520, 521, 522, 523, 524})

log = logging.getLogger(__name__)


def format_eventor_datetime(value: datetime | date | str) -> str:
    """Format a date/datetime the way Eventor query parameters expect.

    Eventor wants ``yyyy-mm-dd hh:mm:ss``. A plain :class:`date` becomes
    midnight; strings are passed through untouched so callers can supply an
    already formatted value.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return f"{value.isoformat()} 00:00:00"


def _ids(values: Iterable[int | str] | None) -> str | None:
    if values is None:
        return None
    joined = ",".join(str(v) for v in values)
    return joined or None


def _bool(value: bool) -> str:
    return "true" if value else "false"


class EventorClient:
    """Typed access to the Eventor API.

    Parameters
    ----------
    base_url:
        Instance base URL including ``/api`` (see the ``*_BASE_URL`` constants).
    api_key:
        The organisation's API key, sent as the ``ApiKey`` header.
    cache:
        Optional :class:`ResponseCache`; when given, successful responses are
        cached and served from disk until they expire.
    timeout:
        Per-request timeout in seconds.
    max_retries:
        Attempts after the first on 5xx/429/transport errors.
    backoff_base:
        First back-off delay in seconds; doubles each retry, with jitter.
    min_interval:
        Minimum seconds between consecutive requests from this client.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        cache: ResponseCache | None = None,
        timeout: float = 60.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        min_interval: float = 0.0,
        user_agent: str = DEFAULT_USER_AGENT,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise EventorError("an Eventor API key is required")
        self.base_url = base_url.rstrip("/")
        self.cache = cache
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.min_interval = min_interval
        self._key_namespace = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
        self._last_request_at = 0.0
        self._sleep = time.sleep
        self._http = httpx.Client(
            base_url=self.base_url,
            headers={
                "ApiKey": api_key,
                "Accept": "application/xml",
                "User-Agent": user_agent,
            },
            timeout=timeout,
            transport=transport,
        )

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- transport -----------------------------------------------------------

    def get_bytes(self, path: str, params: dict[str, str | None] | None = None) -> bytes:
        """Fetch a raw response body, honouring the cache and retry policy."""
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        path = "/" + path.lstrip("/")
        key = None
        if self.cache is not None:
            key = ResponseCache.make_key(self.base_url + path, clean, self._key_namespace)
            cached = self.cache.get(key)
            if cached is not None:
                log.debug("cache hit for %s %s", path, clean)
                return cached
        body = self._request_with_retries(path, clean)
        if self.cache is not None and key is not None:
            self.cache.put(key, body)
        return body

    def get_xml(self, path: str, params: dict[str, str | None] | None = None) -> ET.Element:
        """Fetch ``path`` and return the parsed XML root element.

        This is the extension point for endpoints that have no dedicated
        method: ``client.get_xml("/results/event", {"eventId": "123"})``.
        """
        return parse_document(self.get_bytes(path, params))

    def _request_with_retries(self, path: str, params: dict[str, str]) -> bytes:
        attempt = 0
        while True:
            self._respect_min_interval()
            try:
                response = self._http.get(path, params=params)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise EventorError(f"request to {path} failed: {exc}") from exc
                self._backoff(attempt, None, f"transport error {exc!r}")
                attempt += 1
                continue

            if response.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self._backoff(attempt, response, f"HTTP {response.status_code}")
                attempt += 1
                continue
            return self._check(response)

    def _respect_min_interval(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            self._sleep(self.min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _backoff(self, attempt: int, response: httpx.Response | None, why: str) -> None:
        delay = self.backoff_base * (2**attempt) + random.uniform(0, self.backoff_base)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = max(delay, float(retry_after))
        log.warning(
            "%s; retrying in %.1fs (attempt %d/%d)", why, delay, attempt + 1, self.max_retries
        )
        self._sleep(delay)

    @staticmethod
    def _check(response: httpx.Response) -> bytes:
        status = response.status_code
        if status in (401, 403):
            raise EventorAuthError(status, str(response.url), response.text)
        if status >= 400:
            raise EventorHTTPError(status, str(response.url), response.text)
        body = response.content
        content_type = response.headers.get("Content-Type", "")
        if "xml" not in content_type and not body.lstrip().startswith(b"<"):
            raise EventorHTTPError(status, str(response.url), response.text[:200])
        return body

    # -- endpoints -----------------------------------------------------------

    def organisation_for_api_key(self) -> Organisation:
        """``GET /organisation/apiKey``: the organisation this key belongs to."""
        root = parse_document(self.get_bytes("/organisation/apiKey"), "Organisation")
        return parse_organisation(root)

    def organisation(self, organisation_id: int) -> Organisation:
        """``GET /organisation/{id}``."""
        root = parse_document(self.get_bytes(f"/organisation/{organisation_id}"), "Organisation")
        return parse_organisation(root)

    def memberships(
        self,
        organisation_id: int,
        year: int,
        *,
        include_contact_details: bool = True,
        include_child_organisations: bool = False,
    ) -> list[Membership]:
        """``GET /memberships`` (Australian instance only).

        Returns one :class:`Membership` per person for ``year``. The
        organisation ID must be the key owner's own organisation.
        """
        root = parse_document(
            self.get_bytes(
                "/memberships",
                {
                    "organisationId": str(organisation_id),
                    "year": str(year),
                    "includeContactDetails": _bool(include_contact_details),
                    "includeChildOrganisations": _bool(include_child_organisations),
                },
            ),
            "OrganisationMembershipList",
        )
        return parse_memberships(root)

    def persons_in_organisation(
        self,
        organisation_id: int,
        *,
        include_contact_details: bool = True,
        include_person_properties: bool = False,
        include_person_identifiers: bool = False,
    ) -> list[Person]:
        """``GET /persons/organisations/{organisationId}``.

        Everyone currently attached to the organisation. Available on every
        instance; the organisation ID must be the key owner's own organisation.
        """
        root = parse_document(
            self.get_bytes(
                f"/persons/organisations/{organisation_id}",
                {
                    "includeContactDetails": _bool(include_contact_details),
                    "includePersonProperties": _bool(include_person_properties),
                    "includePersonIdentifiers": _bool(include_person_identifiers),
                },
            ),
            "PersonList",
        )
        return parse_persons(root)

    def events(
        self,
        *,
        organisation_ids: Iterable[int] | None = None,
        event_ids: Iterable[int] | None = None,
        from_date: datetime | date | str | None = None,
        to_date: datetime | date | str | None = None,
        from_modify_date: datetime | date | str | None = None,
        to_modify_date: datetime | date | str | None = None,
        classification_ids: Iterable[int] | None = None,
        parent_ids: Iterable[int] | None = None,
        include_entry_breaks: bool = False,
        include_attributes: bool = False,
    ) -> list[Event]:
        """``GET /events``.

        ``organisation_ids`` filters on the *organising* club (a state
        association ID matches every club in that state).
        """
        params = {
            "organisationIds": _ids(organisation_ids),
            "eventIds": _ids(event_ids),
            "fromDate": format_eventor_datetime(from_date) if from_date else None,
            "toDate": format_eventor_datetime(to_date) if to_date else None,
            "fromModifyDate": format_eventor_datetime(from_modify_date)
            if from_modify_date
            else None,
            "toModifyDate": format_eventor_datetime(to_modify_date) if to_modify_date else None,
            "classificationIds": _ids(classification_ids),
            "parentIds": _ids(parent_ids),
            "includeEntryBreaks": _bool(include_entry_breaks) if include_entry_breaks else None,
            "includeAttributes": _bool(include_attributes) if include_attributes else None,
        }
        root = parse_document(self.get_bytes("/events", params), "EventList")
        return parse_events(root)

    def entries(
        self,
        *,
        event_ids: Iterable[int] | None = None,
        organisation_ids: Iterable[int] | None = None,
        event_class_ids: Iterable[int] | None = None,
        from_event_date: datetime | date | str | None = None,
        to_event_date: datetime | date | str | None = None,
        from_entry_date: datetime | date | str | None = None,
        to_entry_date: datetime | date | str | None = None,
        from_modify_date: datetime | date | str | None = None,
        to_modify_date: datetime | date | str | None = None,
        include_person_element: bool = True,
        include_organisation_element: bool = False,
        include_event_element: bool = False,
        include_entry_fees: bool = False,
    ) -> list[Entry]:
        """``GET /entries``: online pre-entries (walk-ups are *not* included).

        Note that ``organisation_ids`` filters on the *entrant's* club, not the
        organiser, so to get everyone entered in your own events pass
        ``event_ids``. ``from_modify_date``/``to_modify_date`` allow incremental
        pulls when a caller wants them.
        """
        params = {
            "eventIds": _ids(event_ids),
            "organisationIds": _ids(organisation_ids),
            "eventClassIds": _ids(event_class_ids),
            "fromEventDate": format_eventor_datetime(from_event_date) if from_event_date else None,
            "toEventDate": format_eventor_datetime(to_event_date) if to_event_date else None,
            "fromEntryDate": format_eventor_datetime(from_entry_date) if from_entry_date else None,
            "toEntryDate": format_eventor_datetime(to_entry_date) if to_entry_date else None,
            "fromModifyDate": format_eventor_datetime(from_modify_date)
            if from_modify_date
            else None,
            "toModifyDate": format_eventor_datetime(to_modify_date) if to_modify_date else None,
            "includePersonElement": _bool(include_person_element),
            "includeOrganisationElement": _bool(include_organisation_element),
            "includeEventElement": _bool(include_event_element),
            "includeEntryFees": _bool(include_entry_fees),
        }
        root = parse_document(self.get_bytes("/entries", params), "EntryList")
        return parse_entries(root)

    def event_starts(self, event_id: int) -> StartList:
        """``GET /starts/event``: every starter once a start list is published.

        Includes walk-up entries that ``/entries`` omits. Returns an empty
        start list before the draw.
        """
        root = parse_document(
            self.get_bytes("/starts/event", {"eventId": str(event_id)}), "StartList"
        )
        return parse_start_list(root)

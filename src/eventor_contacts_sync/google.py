"""Minimal Google People API client: exactly what the sync needs and nothing more.

There is deliberately **no delete method**. The sync creates contacts, updates
the fields it owns and changes label membership; removing a person from the
address book is always a human decision.

Authentication is a service account with domain-wide delegation acting as one
Workspace user (``GOOGLE_USER``). Two ways to get there:

* keyless (preferred): whatever Application Default Credentials are present
  (Workload Identity Federation in GitHub Actions, ``gcloud auth
  application-default login`` on a laptop) impersonate the service account and
  have it sign the delegation assertion through the IAM Credentials API;
* a service-account key file (``GOOGLE_SERVICE_ACCOUNT_FILE``), for setups where
  key creation is allowed and gcloud is not at hand.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Iterable
from types import TracebackType
from typing import Any, Self

import httpx

from eventor_contacts_sync.config import SyncConfig

log = logging.getLogger(__name__)

API_ROOT = "https://people.googleapis.com/v1"
CONTACTS_SCOPE = "https://www.googleapis.com/auth/contacts"
CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
MY_CONTACTS = "contactGroups/myContacts"
# Every field the sync reads and may write. updateContact replaces each masked field
# wholesale, so whatever is written must start from a read of the same fields.
PERSON_FIELDS = "names,emailAddresses,phoneNumbers,addresses,externalIds,clientData,memberships"
# Reads also fetch metadata: updates must echo back metadata.sources[].etag.
READ_FIELDS = PERSON_FIELDS + ",metadata"
BATCH_CREATE_MAX = 200
BATCH_UPDATE_MAX = 200
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

TokenProvider = Callable[[], str]


class GoogleError(Exception):
    """A People API or authentication failure."""

    def __init__(self, message: str, *, status_code: int | None = None, status: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.status = status

    @property
    def is_conflict(self) -> bool:
        """The contact changed since it was read (stale etag)."""
        return self.status in {"FAILED_PRECONDITION", "ABORTED"} or self.status_code == 412


def make_token_provider(config: SyncConfig) -> TokenProvider:
    """Return a callable producing a fresh access token for ``config.google_user``."""
    import google.auth
    from google.auth import exceptions, impersonated_credentials
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    try:
        if config.google_service_account_file is not None:
            credentials: Any = service_account.Credentials.from_service_account_file(
                str(config.google_service_account_file),
                scopes=[CONTACTS_SCOPE],
                subject=config.google_user,
            )
        else:
            source, _project = google.auth.default(scopes=[CLOUD_SCOPE])
            credentials = impersonated_credentials.Credentials(
                source_credentials=source,
                target_principal=config.google_service_account,
                target_scopes=[CONTACTS_SCOPE],
                subject=config.google_user,
            )
    except (exceptions.GoogleAuthError, OSError, ValueError) as exc:
        raise GoogleError(f"could not load Google credentials: {exc}") from exc

    def token() -> str:
        try:
            if not credentials.valid:
                credentials.refresh(Request())
        except exceptions.GoogleAuthError as exc:
            raise GoogleError(f"could not get a Google access token: {exc}") from exc
        return credentials.token

    return token


class PeopleClient:
    """The user's contacts via the People API. Mutations are strictly sequential."""

    def __init__(
        self,
        token_provider: TokenProvider,
        *,
        timeout: float = 60.0,
        max_retries: int = 4,
        backoff_base: float = 1.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = token_provider
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = time.sleep
        self._http = httpx.Client(base_url=API_ROOT, timeout=timeout, transport=transport)

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

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        retry: bool = True,
    ) -> dict[str, Any]:
        attempt = 0 if retry else self.max_retries
        while True:
            headers = {"Authorization": f"Bearer {self._token()}"}
            try:
                response = self._http.request(
                    method, path, params=params, json=json, headers=headers
                )
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise GoogleError(f"{method} {path} failed: {exc}") from exc
                self._backoff(attempt, f"transport error {exc!r}")
                attempt += 1
                continue
            if response.status_code in RETRY_STATUSES and attempt < self.max_retries:
                self._backoff(attempt, f"HTTP {response.status_code}")
                attempt += 1
                continue
            if response.status_code >= 400:
                raise self._error(method, path, response)
            return response.json() if response.content else {}

    def _backoff(self, attempt: int, why: str) -> None:
        delay = self.backoff_base * (2**attempt) + random.uniform(0, self.backoff_base)
        log.warning(
            "%s; retrying in %.1fs (attempt %d/%d)", why, delay, attempt + 1, self.max_retries
        )
        self._sleep(delay)

    @staticmethod
    def _error(method: str, path: str, response: httpx.Response) -> GoogleError:
        message, status = response.text[:300], None
        try:
            error = response.json().get("error", {})
            message = error.get("message") or message
            status = error.get("status")
        except ValueError:
            pass
        return GoogleError(
            f"{method} {path}: HTTP {response.status_code} {status or ''}: {message}".strip(),
            status_code=response.status_code,
            status=status,
        )

    # -- reads ---------------------------------------------------------------

    def list_contacts(self) -> list[dict[str, Any]]:
        """Every contact of the user, contact-source data only (no profile merges)."""
        people: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "personFields": READ_FIELDS,
                "sources": "READ_SOURCE_TYPE_CONTACT",
                "pageSize": 1000,
            }
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", "/people/me/connections", params=params)
            people.extend(data.get("connections", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                return people

    def get_contact(self, resource_name: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/{resource_name}",
            params={"personFields": READ_FIELDS, "sources": "READ_SOURCE_TYPE_CONTACT"},
        )

    def list_groups(self) -> dict[str, str]:
        """User-created labels as ``{name: resourceName}``."""
        groups: dict[str, str] = {}
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {"pageSize": 1000, "groupFields": "name,groupType"}
            if page_token:
                params["pageToken"] = page_token
            data = self._request("GET", "/contactGroups", params=params)
            for group in data.get("contactGroups", []):
                if group.get("groupType") == "USER_CONTACT_GROUP" and group.get("name"):
                    groups.setdefault(group["name"], group["resourceName"])
            page_token = data.get("nextPageToken")
            if not page_token:
                return groups

    # -- writes --------------------------------------------------------------

    def create_group(self, name: str) -> str:
        data = self._request("POST", "/contactGroups", json={"contactGroup": {"name": name}})
        return data["resourceName"]

    def batch_create(self, persons: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create up to 200 contacts; returns the created people in request order."""
        body = {
            "contacts": [{"contactPerson": p} for p in persons],
            "readMask": READ_FIELDS,
            "sources": ["READ_SOURCE_TYPE_CONTACT"],
        }
        # Never retried: a create that timed out may still have happened, and a blind
        # retry would duplicate it. The next run finds it by its Eventor ID instead.
        data = self._request("POST", "/people:batchCreateContacts", json=body, retry=False)
        return [item.get("person", {}) for item in data.get("createdPeople", [])]

    def create_contact(self, person: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/people:createContact",
            params={"personFields": "metadata", "sources": "READ_SOURCE_TYPE_CONTACT"},
            json=person,
            retry=False,
        )

    def batch_update(self, persons: dict[str, dict[str, Any]]) -> None:
        """Update up to 200 contacts keyed by resource name; each must carry its etag."""
        body = {
            "contacts": persons,
            "updateMask": PERSON_FIELDS,
            "readMask": "metadata",
            "sources": ["READ_SOURCE_TYPE_CONTACT"],
        }
        self._request("POST", "/people:batchUpdateContacts", json=body)

    def update_contact(self, resource_name: str, person: dict[str, Any]) -> None:
        self._request(
            "PATCH",
            f"/{resource_name}:updateContact",
            params={
                "updatePersonFields": PERSON_FIELDS,
                "personFields": "metadata",
                "sources": "READ_SOURCE_TYPE_CONTACT",
            },
            json=person,
        )

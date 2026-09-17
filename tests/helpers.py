"""Test doubles shared across the suite."""

from __future__ import annotations

import copy
from typing import Any

from eventor_contacts_sync.google import GoogleError

ENV = {
    "EVENTOR_API_KEY": "ev-key",
    "GOOGLE_USER": "president@example.org",
    "GOOGLE_SERVICE_ACCOUNT": "sync@project.iam.gserviceaccount.com",
}


def contact(resource: str, given: str, family: str, **fields: Any) -> dict[str, Any]:
    """A People API person as ``connections.list`` returns it."""
    person: dict[str, Any] = {
        "resourceName": resource,
        "etag": "etag-1",
        "metadata": {"sources": [{"type": "CONTACT", "id": resource[-4:], "etag": "etag-1"}]},
        "names": [
            {
                "metadata": {"primary": True},
                "displayName": f"{given} {family}",
                "givenName": given,
                "familyName": family,
            }
        ],
    }
    person.update(fields)
    return person


class FakePeople:
    """In-memory People API that behaves like the real one where the sync cares."""

    def __init__(self, contacts: list[dict[str, Any]] | None = None, groups=None) -> None:
        self.contacts = {c["resourceName"]: c for c in (contacts or [])}
        self.groups: dict[str, str] = dict(groups or {})
        self.calls: list[str] = []
        self.fail_batches = False
        self.conflict_once: set[str] = set()
        self._next = 1

    def close(self) -> None:
        pass

    def list_groups(self) -> dict[str, str]:
        return dict(self.groups)

    def list_contacts(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self.contacts.values()))

    def get_contact(self, resource_name: str) -> dict[str, Any]:
        self.calls.append("get")
        return copy.deepcopy(self.contacts[resource_name])

    def create_group(self, name: str) -> str:
        self.calls.append(f"group:{name}")
        self.groups[name] = f"contactGroups/g{len(self.groups) + 1}"
        return self.groups[name]

    def _store(self, resource: str, person: dict[str, Any]) -> dict[str, Any]:
        stored = copy.deepcopy(person)
        version = int(self.contacts.get(resource, {}).get("etag", "etag-0").split("-")[1]) + 1
        stored.update(resourceName=resource, etag=f"etag-{version}")
        for phone in stored.get("phoneNumbers", []):
            phone["canonicalForm"] = phone["value"]
        self.contacts[resource] = stored
        return stored

    def create_contact(self, person: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("create")
        resource = f"people/c{self._next}"
        self._next += 1
        return self._store(resource, person)

    def batch_create(self, persons) -> list[dict[str, Any]]:
        self.calls.append("batch_create")
        if self.fail_batches:
            raise GoogleError("batch boom", status_code=400, status="INVALID_ARGUMENT")
        created = []
        for person in persons:
            created.append(self._store(f"people/c{self._next}", person))
            self._next += 1
        return created

    def update_contact(self, resource_name: str, person: dict[str, Any]) -> None:
        self.calls.append("update")
        if resource_name in self.conflict_once:
            self.conflict_once.discard(resource_name)
            self.contacts[resource_name]["etag"] = "etag-9"
            raise GoogleError("stale", status_code=400, status="FAILED_PRECONDITION")
        if person.get("etag") != self.contacts[resource_name]["etag"]:
            raise GoogleError("stale", status_code=400, status="FAILED_PRECONDITION")
        self._store(resource_name, person)

    def batch_update(self, persons: dict[str, dict[str, Any]]) -> None:
        self.calls.append("batch_update")
        if self.fail_batches or self.conflict_once & set(persons):
            raise GoogleError("batch boom", status_code=400, status="FAILED_PRECONDITION")
        for resource, person in persons.items():
            self._store(resource, person)

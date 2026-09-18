"""Compute the idempotent change set between desired people and the user's contacts.

The plan is pure data so it can be printed as a dry run, serialised into the
JSON report, and executed. Rules:

* A contact belongs to the sync once it carries an ``externalIds`` entry of type
  ``eventor`` holding the Eventor person ID. Matching is by that ID, so a changed
  name or email in Eventor is an update, never a duplicate.
* A hand-made contact is *adopted* (stamped with the ID) only when its name
  matches and it shares an email address or phone number with the Eventor person.
  Contacts that are neither managed nor adopted are never written to.
* On a managed contact the sync owns the name, one email, the mobile, one other
  phone, one address, and the managed labels. The values it last wrote are kept
  in ``clientData`` so that only *its* entry is replaced when Eventor changes;
  anything else on the contact (notes, photo, extra numbers, own labels) is
  passed through untouched. A value missing from Eventor never blanks anything.
* A detail *withheld* from a person because it belongs to someone else's contact
  (see ``sources.assign_owners``) is withdrawn: the entry recorded in the sync
  state is removed. A value Eventor simply no longer has is still never blanked.
* A *dependant* (every detail belongs to someone else) keeps a bare managed
  contact: its sync-written details are withdrawn, the managed labels come off
  and the dependant label goes on, so the stubs can be deleted by hand in one go.
* Someone who drops out of Eventor loses the managed labels. Nothing is deleted.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from eventor_contacts_sync.config import SyncConfig
from eventor_contacts_sync.google import MY_CONTACTS
from eventor_contacts_sync.sources import Address, DesiredPerson, normalise_email, normalise_phone

Action = Literal["create", "adopt", "update", "dependant", "unchanged"]

EXTERNAL_ID_TYPE = "eventor"
STATE_PREFIX = "eventor:"
# Output-only keys the API adds to list items; never sent back.
_OUTPUT_ONLY = ("metadata", "formattedType", "canonicalForm", "displayName", "displayNameLastFirst")


# -- reading a People API person ---------------------------------------------------


def eventor_ids(person: dict[str, Any]) -> list[int]:
    ids = []
    for ext in person.get("externalIds", []):
        if (ext.get("type") or "").lower() == EXTERNAL_ID_TYPE:
            try:
                ids.append(int(ext.get("value", "")))
            except ValueError:
                continue
    return ids


def contact_name(person: dict[str, Any]) -> str:
    for name in person.get("names", []):
        full = f"{name.get('givenName', '')} {name.get('familyName', '')}".strip()
        return full or (name.get("displayName") or name.get("unstructuredName") or "").strip()
    return ""


def _state(person: dict[str, Any]) -> dict[str, str]:
    return {
        item["key"][len(STATE_PREFIX) :]: item.get("value", "")
        for item in person.get("clientData", [])
        if item.get("key", "").startswith(STATE_PREFIX)
    }


def _email_key(value: str | None) -> str:
    return normalise_email(value) or (value or "").strip().lower()


def _phone_key_factory(country_code: str | None) -> Callable[[str | None], str]:
    def key(value: str | None) -> str:
        normalised = normalise_phone(value, country_code) or (value or "")
        return "".join(ch for ch in normalised if ch.isdigit())

    return key


def _address_of(item: dict[str, Any]) -> Address:
    return Address(
        street=item.get("streetAddress", ""),
        city=item.get("city", ""),
        region=item.get("region", ""),
        postal_code=item.get("postalCode", ""),
        country=item.get("country", ""),
    )


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k not in _OUTPUT_ONLY}


def label_names(person: dict[str, Any], group_names: dict[str, str]) -> set[str]:
    """Names of the user labels on ``person``; ``group_names`` maps resource name to name."""
    names = set()
    for membership in person.get("memberships", []):
        resource = membership.get("contactGroupMembership", {}).get("contactGroupResourceName")
        if resource in group_names:
            names.add(group_names[resource])
    return names


# -- plan ----------------------------------------------------------------------


@dataclass(slots=True)
class ContactChange:
    action: Action
    name: str
    person_id: int | None = None
    resource_name: str | None = None
    field_changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    labels_add: frozenset[str] = frozenset()
    labels_remove: frozenset[str] = frozenset()
    desired: DesiredPerson | None = None
    existing: dict[str, Any] | None = None
    body: dict[str, Any] | None = None  # person to write, without memberships
    applied: bool = False
    error: str | None = None

    @property
    def has_writes(self) -> bool:
        return self.action != "unchanged"


@dataclass(slots=True)
class Plan:
    changes: list[ContactChange]
    managed_labels: frozenset[str]
    contacts_total: int = 0
    contacts_managed: int = 0
    possible_duplicates: list[dict[str, Any]] = field(default_factory=list)
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    duplicate_ids: list[dict[str, Any]] = field(default_factory=list)

    def by_action(self, action: Action) -> list[ContactChange]:
        return [c for c in self.changes if c.action == action]

    @property
    def lapsed(self) -> list[ContactChange]:
        return [c for c in self.changes if c.desired is None and c.action == "update"]

    def summary(self) -> dict[str, int]:
        writes = [c for c in self.changes if c.has_writes]
        return {
            "google_contacts": self.contacts_total,
            "managed_contacts": self.contacts_managed,
            "desired_contacts": sum(1 for c in self.changes if c.desired is not None),
            "new_contacts": len(self.by_action("create")),
            "adopted_contacts": len(self.by_action("adopt")),
            "updated_contacts": len(self.by_action("update")),
            "unchanged_contacts": len(self.by_action("unchanged")),
            "lapsed_contacts": len(self.lapsed),
            "dependant_contacts": len(self.by_action("dependant")),
            "label_additions": sum(len(c.labels_add) for c in writes),
            "label_removals": sum(len(c.labels_remove) for c in writes),
            "field_changes": sum(
                len(c.field_changes) for c in writes if c.action in ("update", "adopt", "dependant")
            ),
            "possible_duplicates": len(self.possible_duplicates),
            "ambiguous_matches": len(self.ambiguous),
            "api_errors": sum(1 for c in self.changes if c.error),
        }


# -- merging one contact -----------------------------------------------------------


def _merge_slot(
    items: list[dict[str, Any]],
    desired: Any,
    last_key: str | None,
    key_of: Callable[[dict[str, Any]], str],
    desired_key: str,
    make_item: Callable[[dict[str, Any] | None], dict[str, Any]],
) -> tuple[Any, Any] | None:
    """Make sure ``desired`` is in ``items``; returns (old, new) if the list changed.

    The entry the sync wrote last time (found through ``last_key``) is replaced in
    place, keeping its type; otherwise the value is appended. Other entries stay.
    """
    keys = [key_of(item) for item in items]
    if desired_key in keys:
        return None
    if last_key and last_key in keys:
        index = keys.index(last_key)
        old = items[index]
        items[index] = make_item(old)
        return (old, desired)
    items.append(make_item(None))
    return (None, desired)


def _withdraw(
    items: list[dict[str, Any]], last_key: str | None, key_of: Callable[[dict[str, Any]], str]
) -> dict[str, Any] | None:
    """Remove the entry the sync wrote last time (found through ``last_key``), if present."""
    if not last_key:
        return None
    for index, item in enumerate(items):
        if key_of(item) == last_key:
            return items.pop(index)
    return None


def merge_person(
    desired: DesiredPerson, existing: dict[str, Any] | None, config: SyncConfig
) -> tuple[dict[str, Any], dict[str, tuple[Any, Any]]]:
    """Return (person body without memberships, {field: (old, new)}).

    With ``existing`` the body starts from a copy of it, so every field under the
    update mask round-trips; without, it is a fresh contact.
    """
    source = copy.deepcopy(existing) if existing else {}
    state = _state(source)
    changes: dict[str, tuple[Any, Any]] = {}
    phone_key = _phone_key_factory(config.phone_country_code)

    names = [_clean(n) for n in source.get("names", [])]
    current = names[0] if names else {}
    given, family = current.get("givenName", ""), current.get("familyName", "")
    if not names or (
        config.update_names and (given, family) != (desired.given_name, desired.family_name)
    ):
        fresh = {k: v for k, v in current.items() if k not in ("unstructuredName",)}
        fresh.update(givenName=desired.given_name, familyName=desired.family_name)
        if names:
            changes["name"] = (f"{given} {family}".strip(), desired.name)
        names = [fresh, *names[1:]]

    emails = [_clean(e) for e in source.get("emailAddresses", [])]
    if desired.email:
        change = _merge_slot(
            emails,
            desired.email,
            _email_key(state.get("email")) if state.get("email") else None,
            lambda item: _email_key(item.get("value")),
            _email_key(desired.email),
            lambda old: {**(old or {"type": "home"}), "value": desired.email},
        )
        if change and existing:
            changes["email"] = (change[0] and change[0].get("value"), change[1])
    elif "email" in desired.withheld:
        gone = _withdraw(
            emails,
            _email_key(state.get("email")) if state.get("email") else None,
            lambda item: _email_key(item.get("value")),
        )
        if gone is not None:
            changes["email"] = (gone.get("value"), None)

    phones = [_clean(p) for p in source.get("phoneNumbers", [])]
    # canonicalForm is dropped by _clean, so key the existing entries off the raw read.
    raw_phone_keys = {
        id(clean): phone_key(raw.get("canonicalForm") or raw.get("value"))
        for clean, raw in zip(phones, source.get("phoneNumbers", []), strict=True)
    }
    for slot, value, kind in (
        ("mobile", desired.mobile, "mobile"),
        ("phone", desired.phone, "home"),
    ):
        if not value:
            if slot in desired.withheld:
                gone = _withdraw(
                    phones,
                    phone_key(state.get(slot)) if state.get(slot) else None,
                    lambda item: raw_phone_keys.get(id(item)) or phone_key(item.get("value")),
                )
                if gone is not None:
                    changes[slot] = (gone.get("value"), None)
            continue
        change = _merge_slot(
            phones,
            value,
            phone_key(state.get(slot)) if state.get(slot) else None,
            lambda item: raw_phone_keys.get(id(item)) or phone_key(item.get("value")),
            phone_key(value),
            lambda old, value=value, kind=kind: {**(old or {"type": kind}), "value": value},
        )
        if change and existing:
            changes[slot] = (change[0] and change[0].get("value"), change[1])

    addresses = [_clean(a) for a in source.get("addresses", [])]
    if desired.address:
        wanted = desired.address

        def make_address(old: dict[str, Any] | None, wanted: Address = wanted) -> dict[str, Any]:
            item = {"type": (old or {}).get("type", "home")}
            for key, value in (
                ("streetAddress", wanted.street),
                ("city", wanted.city),
                ("region", wanted.region),
                ("postalCode", wanted.postal_code),
                ("country", wanted.country),
            ):
                if value:
                    item[key] = value
            return item

        change = _merge_slot(
            addresses,
            wanted,
            state.get("address") or None,
            lambda item: _address_of(item).key,
            wanted.key,
            make_address,
        )
        if change and existing:
            old = change[0]
            changes["address"] = (old and _address_of(old).one_line, wanted.one_line)

    external = [_clean(e) for e in source.get("externalIds", [])]
    if desired.person_id not in eventor_ids(source):
        external.append({"type": EXTERNAL_ID_TYPE, "value": str(desired.person_id)})

    new_state = dict(state)
    for slot, value in (
        ("email", desired.email),
        ("mobile", desired.mobile),
        ("phone", desired.phone),
        ("address", desired.address.key if desired.address else None),
    ):
        if value:
            new_state[slot] = value
        elif slot in desired.withheld:
            new_state.pop(slot, None)
    client_data = [
        _clean(c)
        for c in source.get("clientData", [])
        if not c.get("key", "").startswith(STATE_PREFIX)
    ]
    client_data += [{"key": STATE_PREFIX + k, "value": v} for k, v in sorted(new_state.items())]
    if existing and new_state != state and not changes and eventor_ids(source):
        changes["sync-state"] = (None, "refreshed")

    body: dict[str, Any] = {
        "names": names,
        "emailAddresses": emails,
        "phoneNumbers": phones,
        "addresses": addresses,
        "externalIds": external,
        "clientData": client_data,
    }
    if existing:
        body["etag"] = existing.get("etag")
        body["metadata"] = existing.get("metadata")
    return body, changes


def memberships_for(
    change: ContactChange, group_ids: dict[str, str]
) -> list[dict[str, dict[str, str]]]:
    """Existing memberships minus removed labels plus added ones (``group_ids``: name -> id)."""
    remove = {group_ids[name] for name in change.labels_remove if name in group_ids}
    resources = []
    for membership in (change.existing or {}).get("memberships", []):
        resource = membership.get("contactGroupMembership", {}).get("contactGroupResourceName")
        if resource and resource not in remove and resource not in resources:
            resources.append(resource)
    if change.existing is None:
        resources.append(MY_CONTACTS)
    for name in sorted(change.labels_add):
        if group_ids[name] not in resources:
            resources.append(group_ids[name])
    return [{"contactGroupMembership": {"contactGroupResourceName": r}} for r in resources]


# -- planning ------------------------------------------------------------------


def plan_contact(
    desired: DesiredPerson,
    existing: dict[str, Any] | None,
    *,
    group_names: dict[str, str],
    managed_labels: frozenset[str],
    config: SyncConfig,
) -> ContactChange:
    """The change for one desired person against its matched contact (or none)."""
    body, field_changes = merge_person(desired, existing, config)
    if existing is None:
        return ContactChange(
            action="create",
            name=desired.name,
            person_id=desired.person_id,
            labels_add=desired.labels,
            desired=desired,
            body=body,
        )
    current = label_names(existing, group_names)
    labels_add = frozenset(desired.labels - current)
    labels_remove = frozenset((current & managed_labels) - desired.labels)
    adopting = desired.person_id not in eventor_ids(existing)
    action: Action = "unchanged"
    if adopting:
        action = "adopt"
    elif field_changes or labels_add or labels_remove:
        action = "update"
    return ContactChange(
        action=action,
        name=desired.name,
        person_id=desired.person_id,
        resource_name=existing.get("resourceName"),
        field_changes=field_changes,
        labels_add=labels_add,
        labels_remove=labels_remove,
        desired=desired,
        existing=existing,
        body=body,
    )


def plan_dependant(
    pid: int,
    contact: dict[str, Any],
    *,
    group_names: dict[str, str],
    managed_labels: frozenset[str],
    config: SyncConfig,
) -> ContactChange:
    """Strip a dependant's managed contact down to a labelled stub (idempotent)."""
    state = _state(contact)
    phone_key = _phone_key_factory(config.phone_country_code)
    body = {k: [_clean(i) for i in contact.get(k, [])] for k in _BODY_FIELDS}
    body["etag"] = contact.get("etag")
    body["metadata"] = contact.get("metadata")
    field_changes: dict[str, tuple[Any, Any]] = {}
    raw_phone_keys = {
        id(clean): phone_key(raw.get("canonicalForm") or raw.get("value"))
        for clean, raw in zip(body["phoneNumbers"], contact.get("phoneNumbers", []), strict=True)
    }
    gone = _withdraw(
        body["emailAddresses"],
        _email_key(state["email"]) if state.get("email") else None,
        lambda item: _email_key(item.get("value")),
    )
    if gone is not None:
        field_changes["email"] = (gone.get("value"), None)
    for slot in ("mobile", "phone"):
        gone = _withdraw(
            body["phoneNumbers"],
            phone_key(state[slot]) if state.get(slot) else None,
            lambda item: raw_phone_keys.get(id(item)) or phone_key(item.get("value")),
        )
        if gone is not None:
            field_changes[slot] = (gone.get("value"), None)
    new_state = {k: v for k, v in state.items() if k not in ("email", "mobile", "phone")}
    if new_state != state:
        body["clientData"] = [
            c for c in body["clientData"] if not c.get("key", "").startswith(STATE_PREFIX)
        ] + [{"key": STATE_PREFIX + k, "value": v} for k, v in sorted(new_state.items())]
    current = label_names(contact, group_names)
    labels_add = frozenset({config.label_dependant} - current)
    labels_remove = frozenset((current & managed_labels) - {config.label_dependant})
    changed = bool(field_changes or labels_add or labels_remove)
    return ContactChange(
        action="dependant" if changed else "unchanged",
        name=contact_name(contact),
        person_id=pid,
        resource_name=contact.get("resourceName"),
        field_changes=field_changes,
        labels_add=labels_add,
        labels_remove=labels_remove,
        existing=contact,
        body=body,
    )


def _detail_keys(person: dict[str, Any], phone_key: Callable[[str | None], str]) -> set[str]:
    keys = {"e:" + _email_key(e.get("value")) for e in person.get("emailAddresses", [])}
    keys |= {
        "p:" + phone_key(p.get("canonicalForm") or p.get("value"))
        for p in person.get("phoneNumbers", [])
    }
    return keys - {"e:", "p:"}


def compute_plan(
    desired: list[DesiredPerson],
    contacts: list[dict[str, Any]],
    *,
    group_names: dict[str, str],
    managed_labels: frozenset[str],
    config: SyncConfig,
    dependant_ids: frozenset[int] = frozenset(),
) -> Plan:
    plan = Plan(changes=[], managed_labels=managed_labels, contacts_total=len(contacts))
    phone_key = _phone_key_factory(config.phone_country_code)

    by_id: dict[int, dict[str, Any]] = {}
    unmanaged: list[dict[str, Any]] = []
    unmanaged_by_name: dict[str, list[dict[str, Any]]] = {}
    for contact in contacts:
        ids = eventor_ids(contact)
        if not ids:
            unmanaged.append(contact)
            name = contact_name(contact).casefold()
            if name:
                unmanaged_by_name.setdefault(name, []).append(contact)
            continue
        plan.contacts_managed += 1
        for pid in ids:
            if pid in by_id:
                plan.duplicate_ids.append(
                    {
                        "person_id": pid,
                        "name": contact_name(contact),
                        "kept": by_id[pid].get("resourceName"),
                        "ignored": contact.get("resourceName"),
                    }
                )
            else:
                by_id[pid] = contact

    adopted: set[str] = set()
    desired_ids: set[int] = set()
    for person in sorted(
        desired, key=lambda p: (p.family_name.casefold(), p.given_name.casefold())
    ):
        desired_ids.add(person.person_id)
        existing = by_id.get(person.person_id)
        if existing is None:
            same_name = [
                c
                for c in unmanaged_by_name.get(person.name.casefold(), [])
                if c.get("resourceName") not in adopted
            ]
            wanted = {"e:" + _email_key(person.email)} if person.email else set()
            wanted |= {"p:" + phone_key(v) for v in (person.mobile, person.phone) if v}
            matches = [c for c in same_name if _detail_keys(c, phone_key) & wanted]
            if config.adopt_existing and len(matches) == 1:
                existing = matches[0]
                adopted.add(existing["resourceName"])
            elif config.adopt_existing and len(matches) > 1:
                # Two hand-made contacts both look like this person. Creating a third
                # would make it worse; leave it for a human to merge.
                plan.ambiguous.append(
                    {
                        "person_id": person.person_id,
                        "name": person.name,
                        "contacts": [c.get("resourceName") for c in matches],
                    }
                )
                continue
            else:
                # Not adopted, so a new contact is created; tell the human about any
                # hand-made contact that looks like the same person ("Rob" vs "Robert"
                # with the same mobile, or the same name with different details).
                shares = [
                    c
                    for c in unmanaged
                    if c not in same_name
                    and c.get("resourceName") not in adopted
                    and _detail_keys(c, phone_key) & wanted
                ]
                for reason, found in (("same name", same_name), ("same email or phone", shares)):
                    if found:
                        plan.possible_duplicates.append(
                            {
                                "person_id": person.person_id,
                                "name": person.name,
                                "reason": reason,
                                "contacts": [c.get("resourceName") for c in found],
                                "contact_names": [contact_name(c) for c in found],
                            }
                        )
        plan.changes.append(
            plan_contact(
                person,
                existing,
                group_names=group_names,
                managed_labels=managed_labels,
                config=config,
            )
        )

    # Managed contacts no longer in any source lose only the managed labels.
    seen: set[str] = set()
    for pid, contact in sorted(by_id.items()):
        resource = contact.get("resourceName", "")
        if resource in seen or any(i in desired_ids for i in eventor_ids(contact)):
            continue
        seen.add(resource)
        if pid in dependant_ids:
            change = plan_dependant(
                pid,
                contact,
                group_names=group_names,
                managed_labels=managed_labels,
                config=config,
            )
            if change.has_writes:
                plan.changes.append(change)
            continue
        stale = frozenset(label_names(contact, group_names) & managed_labels)
        if not stale:
            continue
        body = {k: [_clean(i) for i in contact.get(k, [])] for k in _BODY_FIELDS}
        body["etag"] = contact.get("etag")
        body["metadata"] = contact.get("metadata")
        plan.changes.append(
            ContactChange(
                action="update",
                name=contact_name(contact),
                person_id=pid,
                resource_name=resource,
                labels_remove=stale,
                existing=contact,
                body=body,
            )
        )
    return plan


_BODY_FIELDS = ("names", "emailAddresses", "phoneNumbers", "addresses", "externalIds", "clientData")

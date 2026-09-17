"""Pull the *desired* set of contacts from Eventor.

Modelled on ``eventor_mailchimp_sync.sources``: every run does a full pull of
current-year memberships (plus last year's early in the year) and everyone who
entered one of the club's events in a trailing window. The difference is the
unit: Mailchimp is keyed on email, Google Contacts on the person, so the result
is one :class:`DesiredPerson` per Eventor person ID and a family sharing an
address becomes several contacts.

``/entries`` carries no contact details and ``/memberships`` carries no postal
address, so ``/persons/organisations`` (everyone attached to the club, lapsed
members included) is always fetched as the contact-detail index.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime

from eventor_client import EventorClient, EventorHTTPError, EventorParseError
from eventor_client._xml import attr, child, children
from eventor_client.models import Event, Organisation, Person
from eventor_client.parsing import parse_person

from eventor_contacts_sync.config import SyncConfig

log = logging.getLogger(__name__)

EVENT_ID_CHUNK = 50
AU_STATES = frozenset({"NSW", "ACT", "VIC", "QLD", "SA", "WA", "TAS", "NT"})
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalise_email(email: str | None) -> str | None:
    """Lower-case and strip; return ``None`` for empty or clearly invalid values."""
    if email is None:
        return None
    value = email.strip().lower()
    if not value or not _EMAIL_RE.match(value):
        return None
    return value


def normalise_phone(value: str | None, country_code: str | None) -> str | None:
    """Tidy a phone number into E.164 where possible (``0412 345 678`` -> ``+61412345678``).

    Only digits and a leading ``+`` survive. A leading ``0`` is swapped for the
    country code when one is known; a number that already starts with the country
    code gains a ``+``. Anything shorter than eight digits is treated as absent.
    """
    if value is None:
        return None
    raw = value.strip()
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 8:
        return None
    if plus:
        return f"+{digits}"
    if country_code:
        if digits.startswith("0"):
            return f"+{country_code}{digits[1:]}"
        if digits.startswith(country_code) and len(digits) > len(country_code) + 6:
            return f"+{digits}"
    return digits


def membership_years(now: datetime | date, config: SyncConfig) -> tuple[int, ...]:
    """Years whose memberships count as current: this year, plus last year early on."""
    years = [now.year]
    if now.month <= config.previous_year_until_month:
        years.append(now.year - 1)
    return tuple(years)


def months_before(when: datetime, months: int) -> datetime:
    """Calendar-month subtraction that clamps the day (31 Mar - 1 month -> 28 Feb)."""
    month_index = when.year * 12 + (when.month - 1) - months
    year, month = divmod(month_index, 12)
    month += 1
    leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
    last_day = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return when.replace(year=year, month=month, day=min(when.day, last_day))


def member_label_for_year(config: SyncConfig, year: int) -> str:
    return f"{config.label_member} {year}"


def managed_labels(config: SyncConfig, years: Iterable[int]) -> frozenset[str]:
    """Labels this tool adds *and removes*. ``label_all`` is added but never removed."""
    labels = {member_label_for_year(config, y) for y in years}
    labels.add(config.label_member)
    labels.add(config.label_entrant)
    labels.update(rule.label for rule in config.series)
    return frozenset(labels)


@dataclass(frozen=True, slots=True)
class Address:
    street: str = ""
    city: str = ""
    region: str = ""
    postal_code: str = ""
    country: str = ""

    def __bool__(self) -> bool:
        return bool(self.street or self.city)

    @property
    def key(self) -> str:
        """Comparison key: case, punctuation and spacing differences are not changes."""
        parts = (self.street, self.city, self.postal_code)
        return "|".join(re.sub(r"[^a-z0-9]+", " ", p.casefold()).strip() for p in parts)

    @property
    def one_line(self) -> str:
        parts = (self.street, self.city, self.region, self.postal_code, self.country)
        return ", ".join(p for p in parts if p)


@dataclass(frozen=True, slots=True)
class PersonDetails:
    """One person from ``/persons/organisations``, with the address the client omits."""

    person: Person
    address: Address | None = None


@dataclass(frozen=True, slots=True)
class DesiredPerson:
    """What one Google contact should look like after the sync."""

    person_id: int
    given_name: str
    family_name: str
    email: str | None = None
    mobile: str | None = None
    phone: str | None = None
    address: Address | None = None
    labels: frozenset[str] = frozenset()
    membership_years: frozenset[int] = frozenset()
    is_member: bool = False
    sources: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return f"{self.given_name} {self.family_name}".strip()


@dataclass(frozen=True, slots=True)
class NoContactPerson:
    person_id: int | None
    name: str
    sources: tuple[str, ...]
    is_member: bool


@dataclass(slots=True)
class PullResult:
    organisation: Organisation
    membership_years: tuple[int, ...]
    member_source: str
    window_start: datetime
    window_end: datetime
    events: list[Event]
    people: list[DesiredPerson]
    no_contact: list[NoContactPerson]
    managed_labels: frozenset[str]
    stats: dict[str, int] = field(default_factory=dict)


# -- parsing -------------------------------------------------------------------


def _country_name(address_el: ET.Element) -> str:
    country = child(address_el, "Country")
    if country is None:
        return ""
    names = children(country, "Name")
    for name in names:
        if (attr(name, "languageId") or "").lower() == "en":
            return (name.text or "").strip()
    return (names[0].text or "").strip() if names else ""


def parse_address(person_el: ET.Element) -> Address | None:
    for address_el in children(person_el, "Address"):
        street = (attr(address_el, "street") or "").strip()
        care_of = (attr(address_el, "careOf") or "").strip()
        region = ""
        if care_of.upper() in AU_STATES:
            # The Australian instance has no state field and stores it in careOf
            # (verified September 2026: every non-empty careOf was a state code).
            region, care_of = care_of.upper(), ""
        if care_of and street:
            prefix = care_of if care_of.lower().startswith(("c/", "care of")) else f"c/o {care_of}"
            street = f"{prefix}, {street}"
        address = Address(
            street=street,
            city=(attr(address_el, "city") or "").strip(),
            region=region,
            postal_code=(attr(address_el, "zipCode") or "").strip(),
            country=_country_name(address_el),
        )
        if address:
            return address
    return None


def parse_person_details(root: ET.Element) -> list[PersonDetails]:
    """Parse a ``PersonList`` document, keeping each person's postal address."""
    return [
        PersonDetails(person=parse_person(el), address=parse_address(el))
        for el in children(root, "Person")
    ]


# -- pulling -------------------------------------------------------------------


@dataclass(slots=True)
class _Builder:
    """Accumulates everything seen about one person across the sources."""

    person_id: int
    given_name: str = ""
    family_name: str = ""
    email: str | None = None
    mobile: str | None = None
    phone: str | None = None
    address: Address | None = None
    labels: set[str] = field(default_factory=set)
    years: set[int] = field(default_factory=set)
    is_member: bool = False
    sources: list[str] = field(default_factory=list)

    def see(self, source: str) -> None:
        if source not in self.sources:
            self.sources.append(source)

    def fill(self, details: PersonDetails | None) -> None:
        """Fill gaps (never overwrite) from the ``/persons/organisations`` record."""
        if details is None:
            return
        p = details.person
        self.given_name = self.given_name or p.given_name
        self.family_name = self.family_name or p.family_name
        self.email = self.email or p.email
        self.mobile = self.mobile or p.mobile
        self.phone = self.phone or p.phone
        self.address = self.address or details.address


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _fetch_details(client: EventorClient, org_id: int) -> dict[int, PersonDetails]:
    log.info("fetching persons attached to organisation %d (contact details)", org_id)
    root = client.get_xml(
        f"/persons/organisations/{org_id}",
        {"includeContactDetails": "true"},
    )
    return {d.person.id: d for d in parse_person_details(root) if d.person.id is not None}


def pull(client: EventorClient, config: SyncConfig, now: datetime | None = None) -> PullResult:
    """Fetch members, events and entrants and build one desired contact per person."""
    now = now or datetime.now()
    log.info("asking Eventor which organisation the API key belongs to")
    org = client.organisation_for_api_key()
    log.info("organisation: %s (#%s)", org.name or org.short_name, org.id)
    if org.id is None:
        raise EventorParseError("/organisation/apiKey returned no OrganisationId")
    years = membership_years(now, config)
    window_start = months_before(now, config.window_months)
    window_end = now.replace(hour=23, minute=59, second=59, microsecond=0)
    stats: dict[str, int] = {"memberships": 0, "memberships_unpaid_skipped": 0}

    details = _fetch_details(client, org.id)
    stats["persons_in_organisation"] = len(details)
    builders: dict[int, _Builder] = {}

    member_source = "memberships"
    try:
        for year in years:
            log.info("fetching %d memberships from Eventor", year)
            for m in client.memberships(org.id, year, include_contact_details=True):
                if config.require_paid and not m.paid:
                    stats["memberships_unpaid_skipped"] += 1
                    continue
                if m.person.id is None:
                    continue
                stats["memberships"] += 1
                b = builders.setdefault(m.person.id, _Builder(m.person.id))
                b.given_name = b.given_name or m.person.given_name
                b.family_name = b.family_name or m.person.family_name
                b.email = b.email or m.email
                b.mobile = b.mobile or m.mobile
                b.phone = b.phone or m.phone
                b.is_member = True
                b.years.add(m.year)
                b.labels |= {config.label_member, member_label_for_year(config, m.year)}
                b.see(f"membership:{m.year}")
    except EventorHTTPError as exc:
        if exc.status_code != 404:
            raise
        # Only the Australian instance has /memberships; elsewhere everyone attached
        # to the organisation counts as a member for the current year.
        log.warning("/memberships not available on this instance; using /persons instead")
        member_source = "persons"
        for pid in details:
            b = builders.setdefault(pid, _Builder(pid))
            b.is_member = True
            b.years.add(now.year)
            b.labels |= {config.label_member, member_label_for_year(config, now.year)}
            b.see(f"persons:{now.year}")
            stats["memberships"] += 1

    log.info(
        "fetching events organised by #%d between %s and %s",
        org.id,
        window_start.date(),
        window_end.date(),
    )
    all_events = client.events(
        organisation_ids=[org.id], from_date=window_start, to_date=window_end
    )
    events = [
        e
        for e in all_events
        if not e.is_cancelled and (not e.organiser_ids or org.id in e.organiser_ids)
    ]
    log.info("%d events found", len(events))
    stats["events"] = len(events)
    stats["events_cancelled_skipped"] = len(all_events) - len(events)
    by_event_id = {e.id: e for e in events}
    by_race_id = {race.id: e for e in events for race in e.races}

    stats.update({"entries": 0, "entries_team_skipped": 0, "entries_without_person": 0})
    unknown: dict[tuple[object, ...], NoContactPerson] = {}
    chunks = list(_chunks(sorted(by_event_id), EVENT_ID_CHUNK))
    for index, chunk in enumerate(chunks, 1):
        log.info(
            "fetching entries for %d events (request %d of %d); this is the slow part",
            len(chunk),
            index,
            len(chunks),
        )
        for entry in client.entries(event_ids=chunk, include_person_element=True):
            if entry.is_team:
                stats["entries_team_skipped"] += 1
                continue
            stats["entries"] += 1
            event = by_event_id.get(entry.event_id) if entry.event_id else None
            if event is None:
                for rid in entry.event_race_ids:
                    event = by_race_id.get(rid)
                    if event is not None:
                        break
            labels = {config.label_entrant}
            if event is not None:
                labels |= {rule.label for rule in config.series if rule.matches(event)}
            source = f"entry:{event.id if event else entry.event_id}"

            pid = entry.person_id
            if pid is None and entry.person is None:
                stats["entries_without_person"] += 1
                continue
            if pid is not None and (pid in builders or pid in details):
                b = builders.setdefault(pid, _Builder(pid))
                b.labels |= labels
                b.see(source)
                continue
            # Entrants from other clubs and casual entrants: no contact details are
            # available through the API, so they can only be reported.
            person = entry.person
            name = person.full_name if person is not None else ""
            key = ("id", pid) if pid is not None else ("name", name.casefold())
            seen = unknown.get(key)
            sources = (*seen.sources, source) if seen and source not in seen.sources else None
            if seen is None:
                unknown[key] = NoContactPerson(pid, name, (source,), False)
            elif sources is not None:
                unknown[key] = NoContactPerson(pid, seen.name or name, sources, False)

    people: list[DesiredPerson] = []
    no_contact = list(unknown.values())
    for pid in sorted(builders):
        b = builders[pid]
        b.fill(details.get(pid))
        email = normalise_email(b.email)
        mobile = normalise_phone(b.mobile, config.phone_country_code)
        phone = normalise_phone(b.phone, config.phone_country_code)
        if phone == mobile:
            phone = None
        name = f"{b.given_name} {b.family_name}".strip()
        if not name or not (email or mobile or phone):
            no_contact.append(NoContactPerson(pid, name, tuple(b.sources), b.is_member))
            continue
        people.append(
            DesiredPerson(
                person_id=pid,
                given_name=b.given_name.strip(),
                family_name=b.family_name.strip(),
                email=email,
                mobile=mobile,
                phone=phone,
                address=b.address if config.sync_addresses else None,
                labels=frozenset(b.labels | {config.label_all}),
                membership_years=frozenset(b.years),
                is_member=b.is_member,
                sources=tuple(b.sources),
            )
        )
    no_contact.sort(key=lambda p: (not p.is_member, p.name.casefold()))
    stats["people"] = len(people)
    stats["members"] = sum(1 for p in people if p.is_member)
    stats["entrants"] = sum(1 for p in people if config.label_entrant in p.labels)
    stats["with_address"] = sum(1 for p in people if p.address)
    stats["no_contact"] = len(no_contact)
    return PullResult(
        organisation=org,
        membership_years=years,
        member_source=member_source,
        window_start=window_start,
        window_end=window_end,
        events=events,
        people=people,
        no_contact=no_contact,
        managed_labels=managed_labels(config, years),
        stats=stats,
    )

"""Frozen dataclasses describing the subset of Eventor data this client parses.

Element names follow the community XSD
(https://github.com/orienteering-oss/eventor-api-openapi-spec/blob/main/schema.xsd).
Fields that Eventor may omit are optional. Everything is immutable and
hashable so results can be put in sets and used as dictionary keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import IntEnum


class EventStatus(IntEnum):
    """``EventStatusId`` values as documented on ``/api/documentation``."""

    APPLIED = 1
    APPROVED_BY_REGION = 2
    APPROVED = 3
    CREATED = 4
    ENTRY_OPENED = 5
    ENTRY_PAUSED = 6
    ENTRY_CLOSED = 7
    LIVE = 8
    COMPLETED = 9
    CANCELLED = 10
    REPORTED = 11


class EventClassification(IntEnum):
    """``EventClassificationId`` values from the ``/events`` documentation."""

    CHAMPIONSHIP = 1
    NATIONAL = 2
    STATE = 3
    LOCAL = 4
    CLUB = 5
    INTERNATIONAL = 6


@dataclass(frozen=True, slots=True)
class Organisation:
    """An ``Organisation`` element (club, state association, federation)."""

    id: int | None
    name: str
    short_name: str
    media_name: str | None = None
    organisation_type_id: int | None = None
    country_id: int | None = None
    parent_organisation_id: int | None = None


@dataclass(frozen=True, slots=True)
class Person:
    """A ``Person`` element, with contact details when the API includes them.

    ``email``/``phone``/``mobile`` come from ``Tele`` child elements and are only
    present when the endpoint was called with ``includeContactDetails=true`` (or,
    for entries, ``includePersonElement=true``) and the caller is allowed to see
    them.
    """

    id: int | None
    given_name: str
    family_name: str
    sex: str | None = None
    birth_date: date | None = None
    nationality_country_id: int | None = None
    organisation_id: int | None = None
    email: str | None = None
    phone: str | None = None
    mobile: str | None = None
    external_ids: tuple[tuple[int, str], ...] = ()
    modify_date: datetime | None = None

    @property
    def full_name(self) -> str:
        return f"{self.given_name} {self.family_name}".strip()


@dataclass(frozen=True, slots=True)
class Membership:
    """One ``Membership`` element from ``/memberships`` (Australian instance only).

    The nested person uses the compact ``EventorPerson`` shape (Id, FirstName,
    LastName, BirthDate, Sex) rather than the full ``Person`` element; it is
    normalised into :class:`Person` here. Contact details live on the membership
    itself, not on the person.
    """

    id: int
    year: int
    person: Person
    paid: bool
    type_id: int | None = None
    type_name: str | None = None
    group_membership: bool = False
    group_membership_id: int | None = None
    applied_time: datetime | None = None
    paid_time: datetime | None = None
    phone: str | None = None
    mobile: str | None = None
    email: str | None = None
    organisation_id: int | None = None


@dataclass(frozen=True, slots=True)
class EventRace:
    """An ``EventRace`` element (one race of a possibly multi-day event)."""

    id: int
    name: str
    race_date: datetime | None = None
    race_distance: str | None = None
    race_light_condition: str | None = None


@dataclass(frozen=True, slots=True)
class Event:
    """An ``Event`` element from ``/events``."""

    id: int
    name: str
    start_date: datetime | None = None
    finish_date: datetime | None = None
    classification_id: int | None = None
    status_id: int | None = None
    event_form: str = "IndSingleDay"
    organiser_ids: tuple[int, ...] = ()
    races: tuple[EventRace, ...] = ()
    discipline_ids: tuple[int, ...] = ()
    parent_event_id: int | None = None
    modify_date: datetime | None = None

    @property
    def is_cancelled(self) -> bool:
        return self.status_id == EventStatus.CANCELLED


@dataclass(frozen=True, slots=True)
class Entry:
    """An ``Entry`` element from ``/entries``.

    Individual entries carry a ``Competitor`` with a person (either a bare
    ``PersonId`` or a full ``Person`` when ``includePersonElement=true``). Team
    and relay entries carry a ``TeamName`` instead and are flagged by
    :attr:`is_team`; their members are not expanded by this client.
    """

    id: int | None
    event_id: int | None
    event_race_ids: tuple[int, ...] = ()
    competitor_id: int | None = None
    person_id: int | None = None
    person: Person | None = None
    organisation_id: int | None = None
    organisation: Organisation | None = None
    class_ids: tuple[int, ...] = ()
    class_short_names: tuple[str, ...] = ()
    bib_number: str | None = None
    entry_date: datetime | None = None
    competitor_status: str | None = None
    modify_date: datetime | None = None
    team_name: str | None = None

    @property
    def is_team(self) -> bool:
        return self.team_name is not None


@dataclass(frozen=True, slots=True)
class PersonStart:
    """A ``PersonStart`` from ``/starts/event`` (one starter in one class)."""

    person_id: int | None
    person: Person | None
    organisation_id: int | None = None
    class_id: int | None = None
    class_short_name: str | None = None
    start_time: datetime | None = None
    bib_number: str | None = None
    ccard_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StartList:
    """A ``StartList`` from ``/starts/event``. Empty until a start list is published."""

    event_id: int | None
    starts: tuple[PersonStart, ...] = ()

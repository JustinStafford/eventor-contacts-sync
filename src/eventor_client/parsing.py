"""Convert Eventor XML elements into the dataclasses in :mod:`eventor_client.models`.

Each ``parse_*`` function takes an element and is deliberately tolerant: missing
optional children become ``None``/empty rather than raising, because the four
Eventor instances are not perfectly consistent with the community XSD.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from eventor_client._xml import (
    attr,
    bool_attr,
    child,
    children,
    date_clock,
    int_text,
    iso_date,
    iso_datetime,
    local_name,
    text,
)
from eventor_client.models import (
    Entry,
    Event,
    EventRace,
    Membership,
    Organisation,
    Person,
    PersonStart,
    StartList,
)


def _country_id(el: ET.Element | None) -> int | None:
    """``<CountryId value="..."/>`` or ``<Country><CountryId value=.../></Country>``."""
    if el is None:
        return None
    cid = child(el, "CountryId")
    if cid is None:
        cid = child(child(el, "Country"), "CountryId")
    if cid is None:
        return None
    value = attr(cid, "value") or text(cid)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _organisation_ref(el: ET.Element | None) -> tuple[int | None, Organisation | None]:
    """Resolve an ``OrganisationId``/``Organisation`` choice to (id, organisation)."""
    org_el = child(el, "Organisation")
    if org_el is not None:
        org = parse_organisation(org_el)
        return org.id, org
    return int_text(el, "OrganisationId"), None


def parse_organisation(el: ET.Element) -> Organisation:
    parent = child(el, "ParentOrganisation")
    parent_id = None
    if parent is not None:
        parent_id, parent_org = _organisation_ref(parent)
        if parent_org is not None:
            parent_id = parent_org.id
    return Organisation(
        id=int_text(el, "OrganisationId"),
        name=text(el, "Name") or "",
        short_name=text(el, "ShortName") or "",
        media_name=text(el, "MediaName"),
        organisation_type_id=int_text(el, "OrganisationTypeId"),
        country_id=_country_id(el),
        parent_organisation_id=parent_id,
    )


def _given_name(person_name: ET.Element | None) -> str:
    givens = children(person_name, "Given")
    if not givens:
        return ""

    def order(g: ET.Element) -> int:
        try:
            return int(g.get("sequence", "0"))
        except ValueError:
            return 0

    parts = [text(g) or "" for g in sorted(givens, key=order)]
    return " ".join(p for p in parts if p)


def _tele(el: ET.Element) -> tuple[str | None, str | None, str | None]:
    """Return (email, phone, mobile) from the ``Tele`` children of ``el``."""
    email = phone = mobile = None
    for tele in children(el, "Tele"):
        email = email or attr(tele, "mailAddress")
        phone = phone or attr(tele, "phoneNumber")
        mobile = mobile or attr(tele, "mobilePhoneNumber")
    return email, phone, mobile


def parse_person(el: ET.Element) -> Person:
    """Parse a full ``Person`` element (as used by ``/persons/...``, ``/entries``)."""
    name_el = child(el, "PersonName")
    email, phone, mobile = _tele(el)
    org_id, org = _organisation_ref(el)
    external = []
    for ext in children(el, "ExternalPersonId"):
        type_id = attr(ext, "typeId")
        ext_id = attr(ext, "id")
        if type_id is not None and ext_id is not None:
            try:
                external.append((int(type_id), ext_id))
            except ValueError:
                continue
    return Person(
        id=int_text(el, "PersonId"),
        given_name=_given_name(name_el),
        family_name=text(name_el, "Family") or "",
        sex=attr(el, "sex"),
        birth_date=iso_date(text(child(el, "BirthDate"), "Date")),
        nationality_country_id=_country_id(child(el, "Nationality")),
        organisation_id=org.id if org is not None else org_id,
        email=email,
        phone=phone,
        mobile=mobile,
        external_ids=tuple(external),
        modify_date=date_clock(child(el, "ModifyDate")),
    )


def parse_persons(root: ET.Element) -> list[Person]:
    """Parse a ``PersonList`` document."""
    return [parse_person(p) for p in children(root, "Person")]


def _membership_person(el: ET.Element | None) -> Person:
    """Parse the compact ``EventorPerson`` shape nested in a ``Membership``."""
    sex = text(el, "Sex")
    if sex is not None:
        sex = {"female": "F", "male": "M"}.get(sex.lower(), sex[:1].upper())
    return Person(
        id=int_text(el, "Id"),
        given_name=text(el, "FirstName") or "",
        family_name=text(el, "LastName") or "",
        sex=sex,
        birth_date=iso_date(text(el, "BirthDate")),
    )


def parse_membership(el: ET.Element, organisation_id: int | None = None) -> Membership:
    type_el = child(el, "Type")
    group_id = attr(el, "groupMembershipId")
    return Membership(
        id=int_text(el, "Id") or 0,
        year=int_text(el, "Year") or 0,
        person=_membership_person(child(el, "Person")),
        paid=bool_attr(el, "paid"),
        type_id=int_text(type_el, "Id"),
        type_name=text(type_el, "Name"),
        group_membership=bool_attr(type_el, "groupMembership"),
        group_membership_id=int(group_id) if group_id and group_id.isdigit() else None,
        applied_time=iso_datetime(text(el, "AppliedTime")),
        paid_time=iso_datetime(text(el, "PaidTime")),
        phone=text(el, "PhoneNumber"),
        mobile=text(el, "MobilePhoneNumber"),
        email=text(el, "Email"),
        organisation_id=organisation_id,
    )


def parse_memberships(root: ET.Element) -> list[Membership]:
    """Parse an ``OrganisationMembershipList`` document."""
    result: list[Membership] = []
    for block in children(root, "OrganisationMemberships"):
        org_el = child(block, "Organisation")
        org_id = int_text(org_el, "Id") if org_el is not None else None
        if org_id is None and org_el is not None:
            org_id = int_text(org_el, "OrganisationId")
        result.extend(parse_membership(m, org_id) for m in children(block, "Membership"))
    return result


def parse_event_race(el: ET.Element) -> EventRace:
    return EventRace(
        id=int_text(el, "EventRaceId") or 0,
        name=text(el, "Name") or "",
        race_date=date_clock(child(el, "RaceDate")),
        race_distance=attr(el, "raceDistance"),
        race_light_condition=attr(el, "raceLightCondition"),
    )


def parse_event(el: ET.Element) -> Event:
    organiser = child(el, "Organiser")
    organiser_ids: list[int] = []
    if organiser is not None:
        for c in organiser:
            name = local_name(c.tag)
            if name == "OrganisationId":
                value = int_text(c)
                if value is not None:
                    organiser_ids.append(value)
            elif name == "Organisation":
                value = int_text(c, "OrganisationId")
                if value is not None:
                    organiser_ids.append(value)
    classification_id = int_text(el, "EventClassificationId")
    if classification_id is None:
        classification_id = int_text(child(el, "EventClassification"), "EventClassificationId")
    status_id = int_text(el, "EventStatusId")
    if status_id is None:
        status_id = int_text(child(el, "EventStatus"), "EventStatusId")
    disciplines = [v for v in (int_text(d) for d in children(el, "DisciplineId")) if v is not None]
    disciplines += [
        v
        for v in (int_text(d, "DisciplineId") for d in children(el, "Discipline"))
        if v is not None
    ]
    parent = child(el, "ParentEvent")
    parent_id = int_text(parent, "EventId") if parent is not None else None
    return Event(
        id=int_text(el, "EventId") or 0,
        name=text(el, "Name") or "",
        start_date=date_clock(child(el, "StartDate")),
        finish_date=date_clock(child(el, "FinishDate")),
        classification_id=classification_id,
        status_id=status_id,
        event_form=attr(el, "eventForm") or "IndSingleDay",
        organiser_ids=tuple(organiser_ids),
        races=tuple(parse_event_race(r) for r in children(el, "EventRace")),
        discipline_ids=tuple(disciplines),
        parent_event_id=parent_id,
        modify_date=date_clock(child(el, "ModifyDate")),
    )


def parse_events(root: ET.Element) -> list[Event]:
    """Parse an ``EventList`` document."""
    return [parse_event(e) for e in children(root, "Event")]


def parse_entry(el: ET.Element) -> Entry:
    competitor = child(el, "Competitor")
    person_id = None
    person = None
    competitor_id = None
    org_id = None
    org = None
    team_name = text(el, "TeamName")
    if competitor is not None:
        competitor_id = int_text(competitor, "CompetitorId")
        person_el = child(competitor, "Person")
        if person_el is not None:
            person = parse_person(person_el)
            person_id = person.id
        else:
            person_id = int_text(competitor, "PersonId")
        org_id, org = _organisation_ref(competitor)
        if org is not None:
            org_id = org.id
    elif team_name is not None:
        org_id = int_text(el, "OrganisationId")

    event_id = int_text(el, "EventId")
    event_el = child(el, "Event")
    if event_id is None and event_el is not None:
        event_id = int_text(event_el, "EventId")
    race_ids = [v for v in (int_text(r) for r in children(el, "EventRaceId")) if v is not None]
    for race in children(el, "EventRace"):
        rid = int_text(race, "EventRaceId")
        if rid is not None:
            race_ids.append(rid)
        if event_id is None:
            event_id = int_text(race, "EventId")

    class_ids: list[int] = []
    class_names: list[str] = []
    for cls in children(el, "EntryClass"):
        cid = int_text(cls, "EventClassId")
        if cid is None:
            cid = int_text(child(cls, "EventClass"), "EventClassId")
        if cid is not None:
            class_ids.append(cid)
        cname = text(cls, "ClassShortName")
        if cname is None:
            cname = text(child(cls, "EventClass"), "ClassShortName")
        if cname is not None:
            class_names.append(cname)

    status_el = child(el, "CompetitorStatus")
    return Entry(
        id=int_text(el, "EntryId"),
        event_id=event_id,
        event_race_ids=tuple(race_ids),
        competitor_id=competitor_id,
        person_id=person_id,
        person=person,
        organisation_id=org_id,
        organisation=org,
        class_ids=tuple(class_ids),
        class_short_names=tuple(class_names),
        bib_number=text(el, "BibNumber"),
        entry_date=date_clock(child(el, "EntryDate")),
        competitor_status=attr(status_el, "value"),
        modify_date=date_clock(child(el, "ModifyDate")),
        team_name=team_name,
    )


def parse_entries(root: ET.Element) -> list[Entry]:
    """Parse an ``EntryList`` document."""
    return [parse_entry(e) for e in children(root, "Entry")]


def _start_time(start: ET.Element | None):
    st = child(start, "StartTime")
    if st is None:
        return None
    return date_clock(st) if text(st, "Date") else None


def parse_start_list(root: ET.Element) -> StartList:
    """Parse a ``StartList`` document from ``/starts/event``."""
    event_id = int_text(root, "EventId")
    event_el = child(root, "Event")
    if event_id is None and event_el is not None:
        event_id = int_text(event_el, "EventId")
    starts: list[PersonStart] = []
    for class_start in children(root, "ClassStart"):
        class_id = int_text(class_start, "EventClassId")
        class_name = None
        class_el = child(class_start, "EventClass")
        if class_el is not None:
            class_id = class_id if class_id is not None else int_text(class_el, "EventClassId")
            class_name = text(class_el, "ClassShortName")
        for ps in children(class_start, "PersonStart"):
            person_el = child(ps, "Person")
            person = parse_person(person_el) if person_el is not None else None
            person_id = person.id if person is not None else int_text(ps, "PersonId")
            org_id, org = _organisation_ref(ps)
            if org is not None:
                org_id = org.id
            start = child(ps, "Start")
            if start is None:
                race_start = child(ps, "RaceStart")
                start = child(race_start, "Start") if race_start is not None else None
            ccards = [v for v in (text(c) for c in children(start, "CCardId")) if v is not None]
            ccards += [
                v for v in (text(c, "CCardId") for c in children(start, "CCard")) if v is not None
            ]
            starts.append(
                PersonStart(
                    person_id=person_id,
                    person=person,
                    organisation_id=org_id,
                    class_id=class_id,
                    class_short_name=class_name,
                    start_time=_start_time(start),
                    bib_number=text(start, "BibNumber"),
                    ccard_ids=tuple(ccards),
                )
            )
    return StartList(event_id=event_id, starts=tuple(starts))

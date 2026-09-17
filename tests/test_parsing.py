"""Parsing tests against the sanitised XML fixtures built from the community XSD."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from eventor_client._xml import parse_document
from eventor_client.exceptions import EventorParseError
from eventor_client.models import EventStatus
from eventor_client.parsing import (
    parse_entries,
    parse_events,
    parse_memberships,
    parse_organisation,
    parse_persons,
    parse_start_list,
)


def test_parse_organisation(fixture_bytes):
    org = parse_organisation(
        parse_document(fixture_bytes("organisation_apikey.xml"), "Organisation")
    )
    assert org.id == 123
    assert org.name == "Example Orienteers"
    assert org.short_name == "EXO"
    assert org.media_name == "Example Orienteers"
    assert org.organisation_type_id == 3
    assert org.country_id == 36
    assert org.parent_organisation_id == 2


def test_parse_document_checks_root(fixture_bytes):
    with pytest.raises(EventorParseError, match="expected <PersonList>"):
        parse_document(fixture_bytes("organisation_apikey.xml"), "PersonList")


def test_parse_document_rejects_html():
    with pytest.raises(EventorParseError, match="not well-formed"):
        parse_document(b"<html><body>Login</body>")


def test_parse_memberships(fixture_bytes):
    members = parse_memberships(
        parse_document(fixture_bytes("memberships.xml"), "OrganisationMembershipList")
    )
    assert [m.id for m in members] == [9001, 9002, 9003, 9004, 9005]
    alex = members[0]
    assert alex.year == 2026
    assert alex.paid is True
    assert alex.organisation_id == 123
    assert alex.type_id == 1 and alex.type_name == "Senior"
    assert alex.group_membership is False
    assert alex.person.id == 1001
    assert alex.person.given_name == "Alex"
    assert alex.person.family_name == "Example"
    assert alex.person.sex == "M"
    assert alex.person.birth_date == date(1985, 4, 12)
    # Email is kept verbatim (apart from stripping); normalisation is the sync layer's job.
    assert alex.email == "Alex.Example@Example.com"
    assert alex.mobile == "0400 000 001"
    assert alex.phone is None
    assert alex.applied_time == datetime(2026, 1, 5, 10, 15)
    assert alex.paid_time == datetime(2026, 1, 5, 10, 16, 30)

    family = members[1]
    assert family.group_membership is True
    assert family.group_membership_id == 55
    assert family.type_name == "Family"

    unpaid = members[3]
    assert unpaid.paid is False
    assert unpaid.paid_time is None
    assert unpaid.person.birth_date is None

    no_email = members[4]
    assert no_email.email is None


def test_parse_persons(fixture_bytes):
    persons = parse_persons(
        parse_document(fixture_bytes("persons_organisations.xml"), "PersonList")
    )
    assert [p.id for p in persons] == [1001, 1002, 1004, 1005]
    alex, sam, pat, kim = persons
    assert pat.email == "pat.unpaid@example.com"
    assert alex.given_name == "Alex"
    assert alex.family_name == "Example"
    assert alex.full_name == "Alex Example"
    assert alex.email == "alex.example@example.com"
    assert alex.mobile == "0400 000 001"
    assert alex.organisation_id == 123
    assert alex.nationality_country_id == 36
    assert alex.birth_date == date(1985, 4, 12)
    assert alex.modify_date == datetime(2026, 1, 5, 10, 15)
    assert alex.sex == "M"

    # Given names are ordered by their sequence attribute, not document order.
    assert sam.given_name == "Sam Lee"
    # Email is picked up from whichever Tele element carries it.
    assert sam.email == "sample.family@example.com"
    assert sam.phone == "+61 2 0000 0002"
    # Nested <Organisation> and <Country> forms are handled too.
    assert sam.organisation_id == 123
    assert sam.nationality_country_id == 36
    assert sam.external_ids == ((1, "AUS-1002"),)

    assert kim.email is None
    assert kim.given_name == "Kim"


def test_parse_events(fixture_bytes):
    events = parse_events(parse_document(fixture_bytes("events.xml"), "EventList"))
    assert [e.id for e in events] == [5001, 5002, 5003]
    sprint, bush, night = events
    assert sprint.name == "Sprint Series Round 3"
    assert sprint.start_date == datetime(2026, 5, 10, 10, 0)
    assert sprint.finish_date == datetime(2026, 5, 10, 13, 0)
    assert sprint.classification_id == 5
    assert sprint.status_id == EventStatus.COMPLETED
    assert sprint.organiser_ids == (123,)
    assert sprint.discipline_ids == (1,)
    assert sprint.event_form == "IndSingleDay"
    assert len(sprint.races) == 1
    assert sprint.races[0].id == 7001
    assert sprint.races[0].race_distance == "Sprint"
    assert sprint.races[0].race_date == datetime(2026, 5, 10, 10, 0)
    assert sprint.modify_date == datetime(2026, 5, 11, 18, 0)
    assert not sprint.is_cancelled

    # Expanded EventClassification/EventStatus/Discipline/Organisation forms.
    assert bush.classification_id == 4
    assert bush.status_id == 9
    assert bush.discipline_ids == (1,)
    assert bush.organiser_ids == (123, 124)
    assert bush.start_date == datetime(2026, 6, 14, 0, 0)
    assert bush.finish_date is None
    assert bush.parent_event_id == 4999

    assert night.is_cancelled


def test_parse_entries(fixture_bytes):
    entries = parse_entries(parse_document(fixture_bytes("entries.xml"), "EntryList"))
    assert [e.id for e in entries] == [80001, 80002, 80003, 80004, 80005, 80006, 80007]
    alex, vic, gus, team, sam, kim, pat = entries
    assert pat.person is not None and pat.person.email is None and pat.event_id == 5002

    assert alex.competitor_id == 60001
    assert alex.person_id == 1001
    assert alex.person is not None and alex.person.email == "alex.example@example.com"
    assert alex.person.organisation_id == 123
    assert alex.organisation_id == 123
    assert alex.event_id is None  # only EventRaceId given
    assert alex.event_race_ids == (7001,)
    assert alex.class_ids == (3001,)
    assert alex.bib_number == "12"
    assert alex.entry_date == datetime(2026, 5, 1, 9, 0)
    assert alex.competitor_status == "Active"
    assert not alex.is_team

    # Non-member without contact details.
    assert vic.person is not None and vic.person.email is None
    assert vic.organisation_id == 456
    assert vic.event_id == 5002
    assert vic.class_ids == (3002,)
    assert vic.class_short_names == ("Moderate",)

    # Non-member with contact details present.
    assert gus.person is not None and gus.person.email == "gus.guest@example.com"

    assert team.is_team
    assert team.team_name == "Relay Team A"
    assert team.person is None
    assert team.organisation_id == 123
    assert team.event_id == 5002

    assert sam.person is not None and sam.person.given_name == "Sam Lee"
    assert sam.person.email == "SAMPLE.family@example.com"

    # includePersonElement=false shape: bare PersonId.
    assert kim.person is None
    assert kim.person_id == 1005


def test_parse_start_list(fixture_bytes):
    start_list = parse_start_list(parse_document(fixture_bytes("starts_event.xml"), "StartList"))
    assert start_list.event_id == 5001
    assert len(start_list.starts) == 3
    alex, walkup_id_only, wendy = start_list.starts
    assert alex.person_id == 1001
    assert alex.person is not None and alex.person.family_name == "Example"
    assert alex.organisation_id == 123
    assert alex.class_id == 3001
    assert alex.class_short_name == "Hard"
    assert alex.start_time == datetime(2026, 5, 10, 10, 2)
    assert alex.bib_number == "12"
    assert alex.ccard_ids == ("123456",)

    assert walkup_id_only.person is None
    assert walkup_id_only.person_id == 3001
    assert walkup_id_only.ccard_ids == ("654321",)
    assert walkup_id_only.start_time is None  # no Date on StartTime

    assert wendy.person_id is None
    assert wendy.person is not None and wendy.person.full_name == "Wendy Walkup"


def test_models_are_frozen_and_hashable(fixture_bytes):
    persons = parse_persons(parse_document(fixture_bytes("persons_organisations.xml")))
    assert len(set(persons)) == 4
    with pytest.raises(AttributeError):
        persons[0].email = "x@example.com"  # type: ignore[misc]

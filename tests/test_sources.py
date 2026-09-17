"""Eventor pull: per-person records with contact details, addresses and labels."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
import respx
from eventor_client import AU_BASE_URL, EventorClient

from eventor_contacts_sync.config import load_config
from eventor_contacts_sync.sources import (
    Address,
    managed_labels,
    membership_years,
    months_before,
    normalise_phone,
    pull,
)
from helpers import ENV

XML = {"Content-Type": "application/xml"}
NOW = datetime(2026, 9, 6, 12, 0)


@pytest.fixture
def eventor_routes(fixture_bytes):
    with respx.mock(assert_all_called=False) as mock:
        for path, name in (
            ("/organisation/apiKey", "organisation_apikey.xml"),
            ("/memberships", "memberships.xml"),
            ("/persons/organisations/123", "persons_organisations.xml"),
            ("/events", "events.xml"),
            ("/entries", "entries.xml"),
        ):
            mock.get(f"{AU_BASE_URL}{path}").mock(
                return_value=httpx.Response(200, content=fixture_bytes(name), headers=XML)
            )
        yield mock


def test_membership_years_and_window():
    cfg = load_config(ENV)
    assert membership_years(date(2026, 3, 31), cfg) == (2026, 2025)
    assert membership_years(date(2026, 4, 1), cfg) == (2026,)
    assert months_before(datetime(2026, 3, 31), 1) == datetime(2026, 2, 28)


def test_managed_labels_exclude_the_permanent_label():
    cfg = load_config(ENV)
    assert managed_labels(cfg, (2026, 2025)) == {
        "NOC Member",
        "NOC Member 2026",
        "NOC Member 2025",
        "NOC Entrant",
    }


def test_normalise_phone():
    assert normalise_phone("0412 345 678", "61") == "+61412345678"
    assert normalise_phone("+61 2 0000 0002", "61") == "+61200000002"
    assert normalise_phone("1234", "61") is None


def test_address_key_ignores_case_and_punctuation():
    a = Address(street="1 Compass Way", city="Canberra", postal_code="2600")
    b = Address(street="1 compass way,", city="CANBERRA", region="ACT", postal_code="2600")
    assert a.key == b.key
    assert not Address()


def test_pull_builds_one_contact_per_person(eventor_routes):
    cfg = load_config(ENV)
    with EventorClient(AU_BASE_URL, "ev-key") as client:
        result = pull(client, cfg, NOW)
    people = {p.person_id: p for p in result.people}

    alex = people[1001]
    assert (alex.given_name, alex.family_name) == ("Alex", "Example")
    assert alex.email == "alex.example@example.com"
    assert alex.mobile == "+61400000001"
    assert alex.is_member
    assert {"Eventor", "NOC Member", "NOC Member 2026", "NOC Entrant"} <= alex.labels
    # /memberships has no address; it comes from /persons, with the AU state from careOf.
    assert alex.address == Address("1 Compass Way", "Canberra", "ACT", "2600", "Australia")

    # A family sharing one address is two contacts, not one.
    assert people[1002].email == people[1003].email == "sample.family@example.com"
    # Unpaid membership: not a member, but still attached to the club and an entrant.
    assert 1004 in people
    assert not people[1004].is_member
    assert "NOC Member" not in people[1004].labels
    assert "NOC Entrant" in people[1004].labels
    # Entrants from other clubs have no contact details and are only reported.
    assert 2001 not in people
    assert {p.person_id for p in result.no_contact} >= {2001, 2002}
    assert result.stats["entries_team_skipped"] == 1


def test_pull_without_addresses(eventor_routes):
    cfg = load_config({**ENV, "SYNC_ADDRESSES": "false"})
    with EventorClient(AU_BASE_URL, "ev-key") as client:
        result = pull(client, cfg, NOW)
    assert all(p.address is None for p in result.people)


def test_pull_falls_back_to_persons_when_memberships_missing(eventor_routes):
    eventor_routes.get(f"{AU_BASE_URL}/memberships").mock(return_value=httpx.Response(404))
    cfg = load_config(ENV)
    with EventorClient(AU_BASE_URL, "ev-key") as client:
        result = pull(client, cfg, NOW)
    assert result.member_source == "persons"
    assert all(p.is_member for p in result.people)

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
    _Builder,
    _Candidate,
    assign_owners,
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
        "Member",
        "Member 2026",
        "Member 2025",
        "Entrant",
        "Dependant",
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
    assert {"Eventor", "Member", "Member 2026", "Entrant"} <= alex.labels
    # /memberships has no address; it comes from /persons, with the AU state from careOf.
    assert alex.address == Address("1 Compass Way", "Canberra", "ACT", "2600", "Australia")

    # A family sharing one email: the parent (born 1980) owns it, the child (born 2015)
    # has nothing else and becomes a dependant rather than a duplicate handle.
    assert people[1002].email == "sample.family@example.com"
    assert 1003 not in people
    (dependant,) = result.dependants
    assert (dependant.person_id, dependant.owner_id) == (1003, 1002)
    assert (dependant.name, dependant.owner_name) == ("Jo Sample", "Sam Sample")
    assert dependant.is_member
    assert result.stats["dependants"] == 1
    # Unpaid membership: not a member, but still attached to the club and an entrant.
    assert 1004 in people
    assert not people[1004].is_member
    assert "Member" not in people[1004].labels
    assert "Entrant" in people[1004].labels
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


def candidate(pid, *, member=True, born=None, email=None, mobile=None, phone=None):
    b = _Builder(pid, "Given", f"Family{pid}", is_member=member, birth_date=born)
    return _Candidate(b, email, mobile, phone)


def test_assign_owners_prefers_members_then_oldest_then_lowest_id():
    parent = candidate(20, born=date(1980, 1, 1), email="fam@example.com", mobile="+61400000001")
    child = candidate(10, born=date(2015, 1, 1), email="fam@example.com", mobile="+61400000001")
    teen = candidate(30, born=date(2010, 1, 1), email="fam@example.com", mobile="+61400000002")
    lapsed = candidate(5, member=False, born=date(1970, 1, 1), email="fam@example.com")
    dependants = assign_owners([child, parent, teen, lapsed])

    assert (parent.email, parent.mobile) == ("fam@example.com", "+61400000001")
    assert parent.withheld == set()
    # The child loses everything and is reported against the parent.
    assert (child.email, child.mobile) == (None, None)
    assert child.withheld == {"email", "mobile"}
    # The teenager keeps their own mobile and only the email is withheld.
    assert (teen.email, teen.mobile) == (None, "+61400000002")
    assert teen.withheld == {"email"}
    # Older but not a member: members come first.
    assert lapsed.email is None
    assert dependants == {10: 20, 5: 20}


def test_assign_owners_ties_break_on_lowest_person_id_and_ignore_unshared_values():
    a = candidate(2, email="pair@example.com", phone="+61200000000")
    b = candidate(1, email="pair@example.com", mobile="+61400000009")
    assert assign_owners([a, b]) == {}
    assert b.email == "pair@example.com"
    assert a.email is None and a.phone == "+61200000000"


def test_assign_owners_treats_mobile_and_other_phone_as_one_number_space():
    parent = candidate(1, born=date(1975, 1, 1), phone="+61400000001")
    child = candidate(2, born=date(2012, 1, 1), mobile="+61400000001")
    assert assign_owners([parent, child]) == {2: 1}
    assert parent.phone == "+61400000001"
    assert child.mobile is None

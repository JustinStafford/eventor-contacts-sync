"""End-to-end runs against a fake People API: apply, idempotency, guard, failure handling."""

from __future__ import annotations

import json
from datetime import datetime

import httpx
import pytest
import respx
from typer.testing import CliRunner

from eventor_client import AU_BASE_URL, EventorClient
from eventor_contacts_sync import cli
from eventor_contacts_sync.config import load_config
from eventor_contacts_sync.sync import SafetyGuardError, run
from helpers import ENV, FakePeople, contact

XML = {"Content-Type": "application/xml"}
NOW = datetime(2026, 9, 6, 12, 0)
CFG = load_config(ENV)


@pytest.fixture
def eventor(fixture_bytes):
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


def sync(people: FakePeople, **kwargs):
    with EventorClient(AU_BASE_URL, "ev-key") as client:
        return run(CFG, now=NOW, eventor_client=client, people_client=people, **kwargs)


def test_dry_run_writes_nothing(eventor):
    people = FakePeople()
    result = sync(people)
    assert result.plan.summary()["new_contacts"] > 0
    assert people.calls == []
    assert "DRY RUN" in result.diff_text


def test_apply_then_second_run_is_a_no_op(eventor):
    people = FakePeople()
    first = sync(people, apply=True)
    assert first.ok
    created = first.plan.summary()["new_contacts"]
    assert len(people.contacts) == created
    assert {"Eventor", "Member", "Member 2026", "Entrant"} <= set(people.groups)
    assert people.calls.count("batch_create") == 1
    assert all(c.applied for c in first.plan.changes if c.has_writes)

    people.calls.clear()
    second = sync(people, apply=True)
    summary = second.plan.summary()
    assert summary["unchanged_contacts"] == created
    assert summary["new_contacts"] == summary["updated_contacts"] == 0
    assert people.calls == []


def test_limit_caps_writes_and_the_next_run_finishes_the_job(eventor):
    people = FakePeople()
    first = sync(people, apply=True, limit=2)
    assert len(people.contacts) == 2
    assert sum(c.applied for c in first.plan.changes) == 2
    sync(people, apply=True)
    assert len(people.contacts) == first.plan.summary()["new_contacts"]


def test_hand_made_contact_is_adopted_not_duplicated(eventor):
    hand_made = contact(
        "people/h1",
        "Alex",
        "Example",
        emailAddresses=[{"value": "Alex.Example@example.com"}],
        biographies=[{"value": "note"}],
    )
    people = FakePeople([hand_made])
    result = sync(people, apply=True)
    assert result.plan.summary()["adopted_contacts"] == 1
    names = [c["names"][0]["givenName"] for c in people.contacts.values()]
    assert names.count("Alex") == 1
    adopted = people.contacts["people/h1"]
    assert adopted["externalIds"] == [{"type": "eventor", "value": "1001"}]
    assert len(adopted["emailAddresses"]) == 1  # same address, different case: not re-added


def test_batch_failure_falls_back_to_single_writes(eventor):
    people = FakePeople()
    people.fail_batches = True
    result = sync(people, apply=True)
    assert result.ok
    assert people.calls.count("create") == len(people.contacts) > 0


def test_ambiguous_batch_create_failure_is_not_retried_one_by_one(eventor):
    from eventor_contacts_sync.google import GoogleError

    people = FakePeople()

    def timed_out(_persons):
        raise GoogleError("POST /people:batchCreateContacts: HTTP 503", status_code=503)

    people.batch_create = timed_out
    result = sync(people, apply=True)
    assert not result.ok
    assert "create" not in people.calls  # a blind retry could duplicate the whole batch
    assert all("left for the next run" in c.error for c in result.plan.by_action("create"))


def test_stale_etag_is_re_read_and_retried(eventor):
    people = FakePeople()
    sync(people, apply=True)
    resource = next(r for r, c in people.contacts.items() if c["externalIds"][0]["value"] == "1001")
    people.contacts[resource]["emailAddresses"] = [{"value": "stale@example.com"}]
    people.contacts[resource]["clientData"] = [
        {"key": "eventor:email", "value": "stale@example.com"}
    ]
    people.conflict_once = {resource}
    people.calls.clear()
    result = sync(people, apply=True)
    assert result.ok
    assert people.calls == ["batch_update", "update", "get", "update"]
    assert people.contacts[resource]["emailAddresses"] == [{"value": "alex.example@example.com"}]


def test_guard_refuses_mass_label_removal_unless_forced(eventor, fixture_bytes):
    groups = {"Member": "contactGroups/m", "Eventor": "contactGroups/all"}
    members = [
        contact(
            f"people/c{i}",
            "Old",
            f"Member{i}",
            externalIds=[{"type": "eventor", "value": str(7000 + i)}],
            memberships=[
                {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/m"}}
            ],
        )
        for i in range(12)
    ]
    people = FakePeople(members, groups)
    with pytest.raises(SafetyGuardError, match="12 of 12"):
        sync(people, apply=True)
    assert people.calls == []
    assert sync(people, apply=True, force=True).ok
    assert sync(people).plan.summary()["lapsed_contacts"] == 0


def test_guard_refuses_when_eventor_returns_no_members(eventor):
    empty = b'<?xml version="1.0"?><OrganisationMembershipList/>'
    eventor.get(f"{AU_BASE_URL}/memberships").mock(
        return_value=httpx.Response(200, content=empty, headers=XML)
    )
    with pytest.raises(SafetyGuardError, match="no members"):
        sync(FakePeople(), apply=True)


def test_cli_writes_report_and_redacts(eventor, tmp_path, monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("eventor_contacts_sync.sync.make_people_client", lambda cfg: FakePeople())
    result = CliRunner().invoke(
        cli.app, ["--no-progress", "sync", "--redact", "--as-of", "2026-09-06"]
    )
    assert result.exit_code == 0, result.output
    assert "Alex" not in result.output
    assert "Eventor #1001" in result.output
    assert "president" not in result.output
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["mode"] == "dry-run"
    assert report["summary"]["new_contacts"] == len(report["changes"])
    assert "example.com" not in json.dumps(report)
    dumped = json.dumps(report)
    for leak in ("Alex", "Sample", "Compass", "0400", "+61", "president"):
        assert leak not in dumped, leak
        assert leak not in result.output, leak


def test_redacted_errors_and_logs_are_scrubbed(eventor, caplog):
    from eventor_contacts_sync.google import GoogleError

    people = FakePeople()

    def rejected(_persons):
        raise GoogleError(
            "Invalid value alex.example@example.com / +61 400 000 001", status_code=400
        )

    people.batch_create = rejected
    people.create_contact = rejected
    with EventorClient(AU_BASE_URL, "ev-key") as client, caplog.at_level("INFO"):
        result = run(
            CFG, now=NOW, apply=True, redact=True, eventor_client=client, people_client=people
        )
    assert not result.ok
    text = caplog.text + result.diff_text + json.dumps(result.report)
    for leak in ("alex.example", "400 000 001", "Alex", "president"):
        assert leak not in text, leak
    assert "[email]" in text and "Eventor #1001" in text


def test_cli_reports_missing_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    result = CliRunner().invoke(cli.app, ["sync"])
    assert result.exit_code == cli.EXIT_CONFIG
    assert "GOOGLE_USER" in result.output

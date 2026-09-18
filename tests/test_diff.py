"""Planning: matching, adoption, field ownership and label changes."""

from __future__ import annotations

from eventor_contacts_sync.config import load_config
from eventor_contacts_sync.diff import compute_plan, memberships_for, merge_person, plan_dependant
from eventor_contacts_sync.sources import Address, DesiredPerson
from helpers import ENV, contact

CFG = load_config(ENV)
MANAGED = frozenset({"Member", "Member 2026", "Entrant", "Dependant"})
GROUPS = {
    "contactGroups/all": "Eventor",
    "contactGroups/m": "Member",
    "contactGroups/m26": "Member 2026",
    "contactGroups/e": "Entrant",
    "contactGroups/own": "Committee",
    "contactGroups/d": "Dependant",
}
GROUP_IDS = {name: resource for resource, name in GROUPS.items()}


def person(pid=1001, given="Alex", family="Example", **kwargs) -> DesiredPerson:
    defaults = {
        "email": "alex@example.com",
        "mobile": "+61400000001",
        "labels": frozenset({"Eventor", "Member", "Member 2026"}),
        "is_member": True,
    }
    return DesiredPerson(pid, given, family, **{**defaults, **kwargs})


def member_of(*resources: str) -> list[dict]:
    return [{"contactGroupMembership": {"contactGroupResourceName": r}} for r in resources]


def managed(resource="people/c1", pid=1001, **fields) -> dict:
    fields.setdefault("externalIds", [{"type": "eventor", "value": str(pid)}])
    return contact(resource, "Alex", "Example", **fields)


def plan_for(desired, contacts, cfg=CFG):
    return compute_plan(desired, contacts, group_names=GROUPS, managed_labels=MANAGED, config=cfg)


def test_unknown_person_is_created_in_my_contacts_with_labels():
    plan = plan_for([person(address=Address("1 Compass Way", "Canberra", "ACT", "2600"))], [])
    (change,) = plan.changes
    assert change.action == "create"
    assert change.body["names"] == [{"givenName": "Alex", "familyName": "Example"}]
    assert change.body["emailAddresses"] == [{"type": "home", "value": "alex@example.com"}]
    assert change.body["phoneNumbers"] == [{"type": "mobile", "value": "+61400000001"}]
    assert change.body["addresses"][0]["region"] == "ACT"
    assert change.body["externalIds"] == [{"type": "eventor", "value": "1001"}]
    assert {"key": "eventor:email", "value": "alex@example.com"} in change.body["clientData"]
    resources = [
        m["contactGroupMembership"]["contactGroupResourceName"]
        for m in memberships_for(change, GROUP_IDS)
    ]
    assert resources[0] == "contactGroups/myContacts"
    assert set(resources[1:]) == {"contactGroups/all", "contactGroups/m", "contactGroups/m26"}


def test_matching_is_by_eventor_id_so_a_new_email_is_an_update_not_a_duplicate():
    existing = managed(
        emailAddresses=[
            {"value": "old@example.com", "type": "work", "metadata": {"primary": True}},
            {"value": "private@example.com", "type": "other"},
        ],
        phoneNumbers=[{"value": "0400 000 001", "canonicalForm": "+61400000001"}],
        clientData=[
            {"key": "eventor:email", "value": "old@example.com"},
            {"key": "someone-else", "value": "keep"},
        ],
        memberships=member_of("contactGroups/myContacts", "contactGroups/all", "contactGroups/m",
                              "contactGroups/m26", "contactGroups/own"),
    )  # fmt: skip
    plan = plan_for([person()], [existing])
    (change,) = plan.changes
    assert change.action == "update"
    assert change.field_changes == {"email": ("old@example.com", "alex@example.com")}
    # Only the entry the sync wrote is replaced, in place, keeping its type.
    assert change.body["emailAddresses"] == [
        {"value": "alex@example.com", "type": "work"},
        {"value": "private@example.com", "type": "other"},
    ]
    # The phone differs only in formatting: untouched.
    assert change.body["phoneNumbers"] == [{"value": "0400 000 001"}]
    assert {"key": "someone-else", "value": "keep"} in change.body["clientData"]
    assert change.body["etag"] == "etag-1"
    assert not change.labels_add
    assert not change.labels_remove


def test_in_sync_contact_is_unchanged():
    body, _ = merge_person(person(), None, CFG)
    existing = managed(
        **{k: v for k, v in body.items() if k != "names"},
        memberships=member_of("contactGroups/all", "contactGroups/m", "contactGroups/m26"),
    )
    (change,) = plan_for([person()], [existing]).changes
    assert change.action == "unchanged"


def test_missing_eventor_values_never_blank_the_contact():
    existing = managed(
        emailAddresses=[{"value": "alex@example.com"}],
        phoneNumbers=[{"value": "+61400000001"}],
        addresses=[{"streetAddress": "1 Compass Way", "city": "Canberra"}],
        clientData=[
            {"key": "eventor:email", "value": "alex@example.com"},
            {"key": "eventor:mobile", "value": "+61400000001"},
        ],
        memberships=member_of("contactGroups/all", "contactGroups/m", "contactGroups/m26"),
    )
    (change,) = plan_for(
        [person(email=None, mobile=None, phone="+61200000002")], [existing]
    ).changes
    assert change.field_changes == {"phone": (None, "+61200000002")}
    assert change.body["emailAddresses"] == [{"value": "alex@example.com"}]
    assert len(change.body["phoneNumbers"]) == 2
    assert change.body["addresses"] == [{"streetAddress": "1 Compass Way", "city": "Canberra"}]


def test_labels_added_and_only_managed_labels_removed():
    existing = managed(
        emailAddresses=[{"value": "alex@example.com"}],
        phoneNumbers=[{"value": "+61400000001"}],
        memberships=member_of("contactGroups/myContacts", "contactGroups/e", "contactGroups/own"),
    )
    (change,) = plan_for([person()], [existing]).changes
    assert change.labels_add == {"Eventor", "Member", "Member 2026"}
    assert change.labels_remove == {"Entrant"}
    resources = {
        m["contactGroupMembership"]["contactGroupResourceName"]
        for m in memberships_for(change, GROUP_IDS)
    }
    assert "contactGroups/own" in resources
    assert "contactGroups/myContacts" in resources
    assert "contactGroups/e" not in resources


def test_hand_made_contact_is_adopted_on_name_and_shared_detail():
    hand_made = contact(
        "people/h1",
        "alex",
        "EXAMPLE",
        phoneNumbers=[{"value": "0400 000 001", "canonicalForm": "+61400000001"}],
        biographies=[{"value": "met at the state champs"}],
    )
    (change,) = plan_for([person()], [hand_made]).changes
    assert change.action == "adopt"
    assert change.resource_name == "people/h1"
    assert change.body["externalIds"] == [{"type": "eventor", "value": "1001"}]
    assert change.field_changes["name"] == ("alex EXAMPLE", "Alex Example")
    assert change.field_changes["email"] == (None, "alex@example.com")
    assert "biographies" not in change.body  # not under the update mask, so left alone


def test_same_name_without_shared_detail_is_created_and_reported():
    stranger = contact("people/h1", "Alex", "Example", emailAddresses=[{"value": "x@y.com"}])
    plan = plan_for([person()], [stranger])
    assert plan.changes[0].action == "create"
    assert plan.possible_duplicates[0]["contacts"] == ["people/h1"]


def test_different_name_with_shared_detail_is_never_adopted_but_is_reported():
    nickname = contact(
        "people/h1", "Lex", "Example", emailAddresses=[{"value": "alex@example.com"}]
    )
    plan = plan_for([person()], [nickname])
    assert plan.changes[0].action == "create"
    (dup,) = plan.possible_duplicates
    assert dup["reason"] == "same email or phone"
    assert dup["contact_names"] == ["Lex Example"]


def test_family_sharing_an_email_adopts_only_the_matching_name():
    hand_made = contact("people/h1", "Sam", "Sample", emailAddresses=[{"value": "fam@x.com"}])
    desired = [
        person(1002, "Sam", "Sample", email="fam@x.com", mobile=None),
        person(1003, "Robin", "Sample", email="fam@x.com", mobile=None),
    ]
    plan = plan_for(desired, [hand_made])
    assert {c.name: c.action for c in plan.changes} == {
        "Sam Sample": "adopt",
        "Robin Sample": "create",
    }


def test_two_matching_hand_made_contacts_are_ambiguous_and_skipped():
    twins = [
        contact(f"people/h{i}", "Alex", "Example", emailAddresses=[{"value": "alex@example.com"}])
        for i in (1, 2)
    ]
    plan = plan_for([person()], twins)
    assert plan.changes == []
    assert plan.ambiguous[0]["person_id"] == 1001


def test_adoption_can_be_switched_off():
    cfg = load_config({**ENV, "SYNC_ADOPT_EXISTING": "false"})
    hand_made = contact(
        "people/h1", "Alex", "Example", emailAddresses=[{"value": "alex@example.com"}]
    )
    plan = plan_for([person()], [hand_made], cfg)
    assert plan.changes[0].action == "create"


def test_lapsed_contact_loses_managed_labels_only_and_unmanaged_contacts_are_ignored():
    lapsed = managed(
        "people/c9",
        pid=999,
        memberships=member_of("contactGroups/all", "contactGroups/m", "contactGroups/own"),
    )
    friend = contact("people/h1", "Some", "Friend", memberships=member_of("contactGroups/m"))
    plan = plan_for([], [lapsed, friend])
    (change,) = plan.changes
    assert change.resource_name == "people/c9"
    assert change.labels_remove == {"Member"}  # 'Eventor' and 'Committee' stay
    assert plan.lapsed == [change]


def test_names_left_alone_when_update_names_is_off():
    cfg = load_config({**ENV, "SYNC_UPDATE_NAMES": "false"})
    existing = managed(
        emailAddresses=[{"value": "alex@example.com"}], phoneNumbers=[{"value": "+61400000001"}]
    )
    existing["names"][0]["givenName"] = "Lex"
    (change,) = plan_for([person()], [existing], cfg).changes
    assert "name" not in change.field_changes
    assert change.body["names"][0]["givenName"] == "Lex"


def test_withheld_detail_is_withdrawn_only_if_the_sync_wrote_it():
    existing = managed(
        emailAddresses=[
            {"value": "parent@example.com", "type": "home"},
            {"value": "own@example.com", "type": "work"},
        ],
        phoneNumbers=[
            {"value": "+61400000001", "canonicalForm": "+61400000001", "type": "mobile"},
            {"value": "+61299999999", "canonicalForm": "+61299999999", "type": "home"},
        ],
        clientData=[
            {"key": "eventor:email", "value": "parent@example.com"},
            {"key": "eventor:mobile", "value": "+61400000001"},
        ],
    )
    desired = person(email=None, mobile=None, withheld=frozenset({"email", "mobile", "phone"}))
    body, changes = merge_person(desired, existing, CFG)
    assert changes == {"email": ("parent@example.com", None), "mobile": ("+61400000001", None)}
    assert body["emailAddresses"] == [{"value": "own@example.com", "type": "work"}]
    assert body["phoneNumbers"] == [{"value": "+61299999999", "type": "home"}]
    assert [c["key"] for c in body["clientData"]] == []

    # Idempotent: running again over the result changes nothing.
    _, changes = merge_person(desired, {**body, "resourceName": "people/c1"}, CFG)
    assert changes == {}


def test_missing_value_without_withheld_still_never_blanks():
    existing = managed(
        emailAddresses=[{"value": "alex@example.com", "type": "home"}],
        clientData=[{"key": "eventor:email", "value": "alex@example.com"}],
    )
    body, changes = merge_person(person(email=None, mobile=None), existing, CFG)
    assert changes == {}
    assert body["emailAddresses"] == [{"value": "alex@example.com", "type": "home"}]


def test_dependant_contact_is_stripped_and_relabelled_then_left_alone():
    existing = managed(
        pid=1003,
        emailAddresses=[{"value": "fam@example.com", "type": "home"}],
        phoneNumbers=[
            {"value": "+61400000001", "canonicalForm": "+61400000001", "type": "mobile"},
            {"value": "+61411111111", "canonicalForm": "+61411111111", "type": "other"},
        ],
        clientData=[
            {"key": "eventor:email", "value": "fam@example.com"},
            {"key": "eventor:mobile", "value": "+61400000001"},
            {"key": "eventor:address", "value": "1 compass way|canberra|2600"},
        ],
        memberships=member_of(
            "contactGroups/all", "contactGroups/m", "contactGroups/m26", "contactGroups/own"
        ),
    )
    # Not flagged as a dependant: an ordinary lapse, labels only.
    (lapse,) = plan_for([], [existing]).changes
    assert lapse.action == "update" and not lapse.field_changes
    plan = compute_plan(
        [], [existing], group_names=GROUPS, managed_labels=MANAGED, config=CFG,
        dependant_ids=frozenset({1003}),
    )  # fmt: skip
    (change,) = plan.changes
    assert change.action == "dependant"
    assert change.field_changes == {
        "email": ("fam@example.com", None),
        "mobile": ("+61400000001", None),
    }
    assert change.body["emailAddresses"] == []
    # The hand-added number stays; so does the address state and the Eventor ID.
    assert change.body["phoneNumbers"] == [{"value": "+61411111111", "type": "other"}]
    assert change.body["clientData"] == [
        {"key": "eventor:address", "value": "1 compass way|canberra|2600"}
    ]
    assert change.body["externalIds"] == [{"type": "eventor", "value": "1003"}]
    assert change.labels_add == {"Dependant"}
    assert change.labels_remove == {"Member", "Member 2026"}
    resources = {
        m["contactGroupMembership"]["contactGroupResourceName"]
        for m in memberships_for(change, GROUP_IDS)
    }
    assert resources == {"contactGroups/all", "contactGroups/own", "contactGroups/d"}
    assert plan.summary()["dependant_contacts"] == 1
    assert plan.summary()["lapsed_contacts"] == 0

    stub = {
        **change.body,
        "resourceName": "people/c1",
        "memberships": member_of("contactGroups/all", "contactGroups/own", "contactGroups/d"),
    }
    redo = plan_dependant(1003, stub, group_names=GROUPS, managed_labels=MANAGED, config=CFG)
    assert redo.action == "unchanged"


def test_dependant_who_gets_their_own_mobile_becomes_a_contact_again():
    stub = managed(
        pid=1003,
        memberships=member_of("contactGroups/all", "contactGroups/d"),
    )
    plan = plan_for([person(pid=1003, email=None, mobile="+61400000777")], [stub])
    (change,) = plan.changes
    assert change.action == "update"
    assert change.labels_remove == {"Dependant"}
    assert change.body["phoneNumbers"] == [{"type": "mobile", "value": "+61400000777"}]

"""People API client: paging, payload shapes, retries and error mapping."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from eventor_contacts_sync.google import (
    API_ROOT,
    PERSON_FIELDS,
    READ_FIELDS,
    GoogleError,
    PeopleClient,
)


@pytest.fixture
def client():
    with PeopleClient(lambda: "tok") as c:
        c._sleep = lambda _s: None
        yield c


@respx.mock
def test_list_contacts_pages_and_reads_contact_source_only(client):
    route = respx.get(f"{API_ROOT}/people/me/connections").mock(
        side_effect=[
            httpx.Response(
                200, json={"connections": [{"resourceName": "people/c1"}], "nextPageToken": "n"}
            ),
            httpx.Response(200, json={"connections": [{"resourceName": "people/c2"}]}),
        ]
    )
    assert [p["resourceName"] for p in client.list_contacts()] == ["people/c1", "people/c2"]
    first, second = (call.request for call in route.calls)
    assert first.url.params["personFields"] == READ_FIELDS
    assert "metadata" in READ_FIELDS.split(",")  # updates need metadata.sources[].etag
    assert first.url.params["sources"] == "READ_SOURCE_TYPE_CONTACT"
    assert first.headers["Authorization"] == "Bearer tok"
    assert second.url.params["pageToken"] == "n"


@respx.mock
def test_list_groups_ignores_system_groups(client):
    respx.get(f"{API_ROOT}/contactGroups").mock(
        return_value=httpx.Response(
            200,
            json={
                "contactGroups": [
                    {"resourceName": "contactGroups/myContacts", "name": "myContacts",
                     "groupType": "SYSTEM_CONTACT_GROUP"},
                    {"resourceName": "contactGroups/g1", "name": "NOC Member",
                     "groupType": "USER_CONTACT_GROUP"},
                ]
            },
        )
    )  # fmt: skip
    assert client.list_groups() == {"NOC Member": "contactGroups/g1"}


@respx.mock
def test_batch_update_sends_full_mask(client):
    route = respx.post(f"{API_ROOT}/people:batchUpdateContacts").mock(
        return_value=httpx.Response(200, json={})
    )
    client.batch_update({"people/c1": {"etag": "e", "names": []}})
    body = json.loads(route.calls.last.request.content)
    assert body["updateMask"] == PERSON_FIELDS
    assert body["contacts"] == {"people/c1": {"etag": "e", "names": []}}


@respx.mock
def test_reads_retry_but_creates_never_do(client):
    read = respx.get(f"{API_ROOT}/contactGroups").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={})]
    )
    assert client.list_groups() == {}
    assert read.call_count == 2

    create = respx.post(f"{API_ROOT}/people:batchCreateContacts").mock(
        return_value=httpx.Response(503, json={"error": {"status": "UNAVAILABLE", "message": "x"}})
    )
    with pytest.raises(GoogleError):
        client.batch_create([{"names": []}])
    assert create.call_count == 1


@respx.mock
def test_errors_carry_google_status(client):
    respx.patch(f"{API_ROOT}/people/c1:updateContact").mock(
        return_value=httpx.Response(
            400, json={"error": {"status": "FAILED_PRECONDITION", "message": "etag mismatch"}}
        )
    )
    with pytest.raises(GoogleError) as excinfo:
        client.update_contact("people/c1", {"etag": "old"})
    assert excinfo.value.is_conflict
    assert "etag mismatch" in str(excinfo.value)


def test_client_has_no_delete_method():
    assert not [name for name in dir(PeopleClient) if "delete" in name.lower()]

"""HTTP-level tests for EventorClient using respx. Nothing here reaches the network."""

from __future__ import annotations

from datetime import date, datetime

import httpx
import pytest
import respx

from eventor_client import (
    AU_BASE_URL,
    EventorAuthError,
    EventorClient,
    EventorError,
    EventorHTTPError,
    ResponseCache,
    format_eventor_datetime,
)

XML_HEADERS = {"Content-Type": "application/xml; charset=utf-8"}


@pytest.fixture
def client():
    with EventorClient(AU_BASE_URL, "test-key", backoff_base=0.0) as c:
        c._sleep = lambda _seconds: None
        yield c


@respx.mock
def test_sends_api_key_header_and_parses_org(client, fixture_bytes):
    route = respx.get(f"{AU_BASE_URL}/organisation/apiKey").mock(
        return_value=httpx.Response(
            200, content=fixture_bytes("organisation_apikey.xml"), headers=XML_HEADERS
        )
    )
    org = client.organisation_for_api_key()
    assert org.id == 123
    request = route.calls.last.request
    assert request.headers["ApiKey"] == "test-key"
    assert "eventor-client" in request.headers["User-Agent"]


@respx.mock
def test_memberships_query_parameters(client, fixture_bytes):
    route = respx.get(f"{AU_BASE_URL}/memberships").mock(
        return_value=httpx.Response(
            200, content=fixture_bytes("memberships.xml"), headers=XML_HEADERS
        )
    )
    members = client.memberships(123, 2026)
    assert len(members) == 5
    params = route.calls.last.request.url.params
    assert params["organisationId"] == "123"
    assert params["year"] == "2026"
    assert params["includeContactDetails"] == "true"
    assert params["includeChildOrganisations"] == "false"


@respx.mock
def test_persons_in_organisation(client, fixture_bytes):
    route = respx.get(f"{AU_BASE_URL}/persons/organisations/123").mock(
        return_value=httpx.Response(
            200, content=fixture_bytes("persons_organisations.xml"), headers=XML_HEADERS
        )
    )
    persons = client.persons_in_organisation(123)
    assert [p.id for p in persons] == [1001, 1002, 1004, 1005]
    assert route.calls.last.request.url.params["includeContactDetails"] == "true"


@respx.mock
def test_events_formats_dates_and_ids(client, fixture_bytes):
    route = respx.get(f"{AU_BASE_URL}/events").mock(
        return_value=httpx.Response(200, content=fixture_bytes("events.xml"), headers=XML_HEADERS)
    )
    events = client.events(
        organisation_ids=[123, 124],
        from_date=date(2025, 9, 1),
        to_date=datetime(2026, 9, 6, 23, 59, 59),
    )
    assert len(events) == 3
    params = route.calls.last.request.url.params
    assert params["organisationIds"] == "123,124"
    assert params["fromDate"] == "2025-09-01 00:00:00"
    assert params["toDate"] == "2026-09-06 23:59:59"
    assert "eventIds" not in params
    assert "includeEntryBreaks" not in params


@respx.mock
def test_entries_exposes_modify_date_filters(client, fixture_bytes):
    route = respx.get(f"{AU_BASE_URL}/entries").mock(
        return_value=httpx.Response(200, content=fixture_bytes("entries.xml"), headers=XML_HEADERS)
    )
    entries = client.entries(event_ids=[5001, 5002], from_modify_date="2026-01-01 00:00:00")
    assert len(entries) == 7
    params = route.calls.last.request.url.params
    assert params["eventIds"] == "5001,5002"
    assert params["includePersonElement"] == "true"
    assert params["fromModifyDate"] == "2026-01-01 00:00:00"
    assert "organisationIds" not in params


@respx.mock
def test_event_starts(client, fixture_bytes):
    respx.get(f"{AU_BASE_URL}/starts/event").mock(
        return_value=httpx.Response(
            200, content=fixture_bytes("starts_event.xml"), headers=XML_HEADERS
        )
    )
    start_list = client.event_starts(5001)
    assert start_list.event_id == 5001
    assert len(start_list.starts) == 3


@respx.mock
def test_get_xml_extension_point(client, fixture_bytes):
    respx.get(f"{AU_BASE_URL}/results/event").mock(
        return_value=httpx.Response(
            200, content=b"<ResultList><Foo/></ResultList>", headers=XML_HEADERS
        )
    )
    root = client.get_xml("/results/event", {"eventId": "5001"})
    assert root.tag == "ResultList"


@respx.mock
def test_auth_error(client):
    respx.get(f"{AU_BASE_URL}/organisation/apiKey").mock(
        return_value=httpx.Response(401, text="nope")
    )
    with pytest.raises(EventorAuthError) as info:
        client.organisation_for_api_key()
    assert info.value.status_code == 401


@respx.mock
def test_backs_off_on_5xx_then_succeeds(client, fixture_bytes):
    route = respx.get(f"{AU_BASE_URL}/organisation/apiKey")
    route.side_effect = [
        httpx.Response(503, text="busy"),
        httpx.Response(502, text="bad gateway"),
        httpx.Response(200, content=fixture_bytes("organisation_apikey.xml"), headers=XML_HEADERS),
    ]
    sleeps: list[float] = []
    client._sleep = sleeps.append
    org = client.organisation_for_api_key()
    assert org.id == 123
    assert route.call_count == 3
    assert len(sleeps) == 2


@respx.mock
def test_retries_cloudflare_origin_errors(client, fixture_bytes):
    route = respx.get(f"{AU_BASE_URL}/organisation/apiKey")
    route.side_effect = [
        httpx.Response(
            520, text="<!DOCTYPE html>cloudflare", headers={"Content-Type": "text/html"}
        ),
        httpx.Response(200, content=fixture_bytes("organisation_apikey.xml"), headers=XML_HEADERS),
    ]
    assert client.organisation_for_api_key().id == 123
    assert route.call_count == 2


@respx.mock
def test_gives_up_after_max_retries(client):
    route = respx.get(f"{AU_BASE_URL}/organisation/apiKey").mock(
        return_value=httpx.Response(500, text="boom")
    )
    with pytest.raises(EventorHTTPError) as info:
        client.organisation_for_api_key()
    assert info.value.status_code == 500
    assert route.call_count == client.max_retries + 1


@respx.mock
def test_honours_retry_after(client, fixture_bytes):
    route = respx.get(f"{AU_BASE_URL}/organisation/apiKey")
    route.side_effect = [
        httpx.Response(429, text="slow down", headers={"Retry-After": "7"}),
        httpx.Response(200, content=fixture_bytes("organisation_apikey.xml"), headers=XML_HEADERS),
    ]
    sleeps: list[float] = []
    client._sleep = sleeps.append
    client.organisation_for_api_key()
    assert sleeps == [7.0]


@respx.mock
def test_retries_transport_errors(client, fixture_bytes):
    route = respx.get(f"{AU_BASE_URL}/organisation/apiKey")
    route.side_effect = [
        httpx.ConnectError("refused"),
        httpx.Response(200, content=fixture_bytes("organisation_apikey.xml"), headers=XML_HEADERS),
    ]
    assert client.organisation_for_api_key().id == 123
    assert route.call_count == 2


@respx.mock
def test_does_not_retry_4xx(client):
    route = respx.get(f"{AU_BASE_URL}/events").mock(
        return_value=httpx.Response(400, text="bad request")
    )
    with pytest.raises(EventorHTTPError):
        client.events()
    assert route.call_count == 1


@respx.mock
def test_html_response_is_an_error(client):
    respx.get(f"{AU_BASE_URL}/organisation/apiKey").mock(
        return_value=httpx.Response(
            200, text="<html><body>Sign in</body></html>", headers={"Content-Type": "text/html"}
        )
    )
    with pytest.raises(EventorError):
        client.organisation_for_api_key()


@respx.mock
def test_disk_cache_serves_repeat_requests(tmp_path, fixture_bytes):
    route = respx.get(f"{AU_BASE_URL}/organisation/apiKey").mock(
        return_value=httpx.Response(
            200, content=fixture_bytes("organisation_apikey.xml"), headers=XML_HEADERS
        )
    )
    cache = ResponseCache(tmp_path, ttl_seconds=3600)
    with EventorClient(AU_BASE_URL, "key-a", cache=cache) as client:
        client.organisation_for_api_key()
        client.organisation_for_api_key()
    assert route.call_count == 1
    # A different API key must not see the cached body.
    with EventorClient(AU_BASE_URL, "key-b", cache=cache) as other:
        other.organisation_for_api_key()
    assert route.call_count == 2
    # Nothing on disk contains the key itself.
    for path in tmp_path.iterdir():
        assert b"key-a" not in path.read_bytes()
        assert "key-a" not in path.name


@respx.mock
def test_disk_cache_expires(tmp_path, fixture_bytes):
    route = respx.get(f"{AU_BASE_URL}/organisation/apiKey").mock(
        return_value=httpx.Response(
            200, content=fixture_bytes("organisation_apikey.xml"), headers=XML_HEADERS
        )
    )
    cache = ResponseCache(tmp_path, ttl_seconds=0)
    with EventorClient(AU_BASE_URL, "key", cache=cache) as client:
        client.organisation_for_api_key()
        client.organisation_for_api_key()
    assert route.call_count == 2
    assert cache.clear() == 1


def test_min_interval_spaces_requests(fixture_bytes):
    sleeps: list[float] = []
    with respx.mock:
        respx.get(f"{AU_BASE_URL}/organisation/apiKey").mock(
            return_value=httpx.Response(
                200, content=fixture_bytes("organisation_apikey.xml"), headers=XML_HEADERS
            )
        )
        with EventorClient(AU_BASE_URL, "key", min_interval=5.0) as client:
            client._sleep = sleeps.append
            client.organisation_for_api_key()
            client.organisation_for_api_key()
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 5.0


def test_requires_api_key():
    with pytest.raises(EventorError):
        EventorClient(AU_BASE_URL, "")


def test_format_eventor_datetime():
    assert format_eventor_datetime(date(2026, 1, 2)) == "2026-01-02 00:00:00"
    assert format_eventor_datetime(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02 03:04:05"
    assert format_eventor_datetime("2026-01-02 00:00:00") == "2026-01-02 00:00:00"

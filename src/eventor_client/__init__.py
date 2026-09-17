"""Thin, typed client for the Eventor orienteering API.

Eventor (https://eventor.orienteering.asn.au, https://eventor.orientering.se,
https://eventor.orientering.no, https://eventor.orienteering.sport) exposes an
XML-over-HTTP API authenticated with a single ``ApiKey`` header. This package
wraps the endpoints a club typically needs and parses the XML into frozen
dataclasses. It knows nothing about Mailchimp and is intended to be reusable
by other clubs and other projects.

Reference: https://github.com/orienteering-oss/eventor-api-openapi-spec
"""

from eventor_client.cache import ResponseCache
from eventor_client.client import (
    AU_BASE_URL,
    IOF_BASE_URL,
    NO_BASE_URL,
    SE_BASE_URL,
    EventorClient,
    format_eventor_datetime,
)
from eventor_client.exceptions import (
    EventorAuthError,
    EventorError,
    EventorHTTPError,
    EventorParseError,
)
from eventor_client.models import (
    Entry,
    Event,
    EventRace,
    EventStatus,
    Membership,
    Organisation,
    Person,
    PersonStart,
    StartList,
)

__all__ = [
    "AU_BASE_URL",
    "IOF_BASE_URL",
    "NO_BASE_URL",
    "SE_BASE_URL",
    "Entry",
    "Event",
    "EventRace",
    "EventStatus",
    "EventorAuthError",
    "EventorClient",
    "EventorError",
    "EventorHTTPError",
    "EventorParseError",
    "Membership",
    "Organisation",
    "Person",
    "PersonStart",
    "ResponseCache",
    "StartList",
    "format_eventor_datetime",
]

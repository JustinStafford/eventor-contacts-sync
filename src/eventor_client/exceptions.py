"""Exception hierarchy for the Eventor client."""

from __future__ import annotations


class EventorError(Exception):
    """Base class for all errors raised by :mod:`eventor_client`."""


class EventorHTTPError(EventorError):
    """The API returned an unexpected HTTP status."""

    def __init__(self, status_code: int, url: str, body: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.body = body
        snippet = body.strip().replace("\n", " ")[:200]
        message = f"HTTP {status_code} from {url}"
        if snippet:
            message = f"{message}: {snippet}"
        super().__init__(message)


class EventorAuthError(EventorHTTPError):
    """The API key was rejected (401/403)."""


class EventorParseError(EventorError):
    """The response body could not be parsed as the expected XML document."""

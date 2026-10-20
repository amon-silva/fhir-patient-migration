"""HTTP calls of the FHIR Bulk Data export flow, plus the retry/backoff policy."""

import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

TIMEOUT = (5, 60)  # seconds: connect, read
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


class FhirError(Exception):
    def __init__(self, message, status=None, transient=False, retry_after=0):
        super().__init__(message)
        self.status = status  # HTTP status, None for network errors
        self.transient = transient  # worth retrying: network error, 429 or 5xx
        self.retry_after = retry_after  # seconds the server asked us to wait


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["Accept"] = "application/fhir+json"
    return session


def with_retries[T](
    call: Callable[[], T], on_attempt: Callable[[], None] | None = None, attempts: int | None = None
) -> T:
    """Run `call()`, retrying transient FhirErrors with exponential backoff
    (1s, 2s, 4s, ...), waiting at least the server's Retry-After.
    `on_attempt` runs before each try, so callers can audit attempt counts."""
    max_attempts: int = attempts or settings.FHIR_MAX_ATTEMPTS
    for attempt in range(1, max_attempts + 1):
        if on_attempt:
            on_attempt()
        try:
            return call()
        except FhirError as exc:
            if not exc.transient or attempt == max_attempts:
                raise
            delay = max(2 ** (attempt - 1), exc.retry_after)
            logger.warning("%s; retry %d/%d in %ss", exc, attempt, max_attempts - 1, delay)
            time.sleep(delay)
    raise AssertionError("unreachable: the last attempt returns or raises")


def kickoff(session: requests.Session, resource_types: str, since: datetime | None = None, ids_only: bool = False) -> str:
    """POST /$export and return the status (polling) URL."""
    parameters = [{"name": "_outputFormat", "valueString": "application/fhir+ndjson"}]
    # The IG: repeat the parameter per value, never comma-delimit them in one.
    parameters += [{"name": "_type", "valueString": t} for t in resource_types.split(",")]
    if since:
        parameters.append({"name": "_since", "valueInstant": since.isoformat()})
    if ids_only:
        parameters.append({"name": "_elements", "valueString": "id"})
    response = _request(
        session,
        "POST",
        f"{settings.FHIR_BASE_URL}/$export",
        json={"resourceType": "Parameters", "parameter": parameters},
        headers={"Prefer": "respond-async", "Content-Type": "application/fhir+json"},
    )
    if response.status_code != 202 or "Content-Location" not in response.headers:
        raise FhirError(f"Unexpected kickoff response {response.status_code}")
    return response.headers["Content-Location"]


def poll(session: requests.Session, status_url: str) -> tuple[dict[str, Any] | None, requests.Response]:
    """One status check. Returns (manifest or None while in progress, response)."""
    response = _request(session, "GET", status_url)
    if response.status_code == 202:
        return None, response
    return response.json(), response


def download(session: requests.Session, url: str, path: Path) -> int:
    """Stream one NDJSON file to `path` (atomically) and return its line count."""
    response = _request(session, "GET", url, headers={"Accept": "application/fhir+ndjson"}, stream=True)
    lines = 0
    partial = f"{path}.part"
    try:
        with open(partial, "wb") as out:
            for line in response.iter_lines():
                if line:
                    out.write(line + b"\n")
                    lines += 1
    except requests.RequestException as exc:
        raise FhirError(f"GET {url} interrupted: {exc}", transient=True) from exc
    os.replace(partial, path)
    return lines


def delete(session: requests.Session, status_url: str) -> int:
    """Ask the server to remove an export's files. Returns the HTTP status;
    404 means it's already gone, which is fine."""
    try:
        return _request(session, "DELETE", status_url).status_code
    except FhirError as exc:
        if exc.status == 404:
            return 404
        raise


def retry_after_seconds(response: requests.Response) -> int:
    """Retry-After as seconds; it may be a number or an HTTP date."""
    value = response.headers.get("Retry-After")
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    try:
        return max(0, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return 0


def expires_at(response: requests.Response) -> datetime | None:
    value = response.headers.get("Expires")
    try:
        return parsedate_to_datetime(value) if value else None
    except (TypeError, ValueError):
        return None


def _request(session, method, url, **kwargs):
    try:
        response = session.request(method, url, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise FhirError(f"{method} {url} failed: {exc}", transient=True) from exc
    if response.status_code >= 400:
        raise FhirError(
            f"{method} {url} returned {response.status_code}",
            status=response.status_code,
            transient=response.status_code in TRANSIENT_STATUSES,
            retry_after=retry_after_seconds(response),
        )
    return response

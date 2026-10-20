"""Export and Delete scripts (Plan.md: "Export Script", "Delete Script").

Every step is recorded on ExportJob / ExportFile, so re-running `run_export`
on the same job continues from where it stopped.
"""

import logging
import shutil
import time
from datetime import datetime, timedelta

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from . import fhir_client, verification
from .fhir_client import FhirError
from .models import ExportFile, ExportJob

logger = logging.getLogger(__name__)

TYPES = "Patient,Observation"
MAX_POLL_DELAY = 300  # seconds; cap of the polling backoff (Retry-After can exceed it)
MANIFEST_KINDS = {"output": ExportFile.Kind.OUTPUT, "deleted": ExportFile.Kind.DELETED, "error": ExportFile.Kind.OUTCOME}

# FOR TESTS ONLY: the public sandbox holds far more data than this exercise needs,
# so the first export only asks for what changed in the last day. In a real
# migration the first export is a full one (since=None).
TEST_ONLY_FIRST_EXPORT_WINDOW = timedelta(days=1)


class ExportFailed(Exception):
    pass


def next_since() -> datetime:
    """One job's transactionTime becomes the next one's `since`."""
    previous = ExportJob.objects.filter(status=ExportJob.Status.COMPLETED).order_by("-pk").first()
    if previous:
        return previous.transaction_time
    return timezone.now() - TEST_ONLY_FIRST_EXPORT_WINDOW


def run_export(job: ExportJob, session: requests.Session | None = None) -> ExportJob:
    """Kick off (or resume) `job` and download every file of its manifest."""
    session = session or fhir_client.make_session()
    data_files = job.files.exclude(kind=ExportFile.Kind.IDS)
    try:
        if not job.polling_url or (job.status == ExportJob.Status.DEAD and not data_files.exists()):
            _export(job, session, _types_to_reexport(job) or TYPES)
        elif not data_files.exists():
            _apply_manifest(job, *_wait_for_manifest(job, session))  # resumed while polling
        _download_pending(job, session)

        # Last resort for files that still fail: a new export of their type.
        if types := _types_to_reexport(job):
            logger.warning("Export %s: re-exporting %s", job.pk, types)
            _export(job, session, types)
            _download_pending(job, session)

        first = ExportJob.objects.filter(status=ExportJob.Status.COMPLETED, pk__lt=job.pk).order_by("pk").first()
        ids_files = job.files.filter(kind=ExportFile.Kind.IDS).exclude(status=ExportFile.Status.DEAD)
        if first and not data_files.filter(status=ExportFile.Status.FAILED).exists() and (
            not ids_files.exists() or ids_files.exclude(status=ExportFile.Status.DOWNLOADED).exists()
        ):
            _export_ids(job, session, first.since)
    except FhirError as exc:
        job.status = ExportJob.Status.DEAD
        job.save()
        raise ExportFailed(f"Export {job.pk} is dead: {exc}. Re-run export_fhir to resume it.") from exc

    failed = job.files.filter(status=ExportFile.Status.FAILED).count()
    job.status = ExportJob.Status.FAILED if failed else ExportJob.Status.COMPLETED
    job.save()
    if failed:
        raise ExportFailed(f"Export {job.pk}: {failed} file(s) failed. Re-run export_fhir to resume it.")
    return job


def delete_export(job: ExportJob, session: requests.Session | None = None, force: bool = False) -> None:
    """DELETE the export on the Data Provider Server, then our local NDJSON copies.
    An unfinished export can't be migrated, so only a completed one is checked first."""
    if job.polling_url:
        if job.status == ExportJob.Status.COMPLETED and not force:
            _check_safe_to_delete(job)
        session = session or fhir_client.make_session()
        status = fhir_client.with_retries(lambda: fhir_client.delete(session, job.polling_url))
        logger.info("Export %s: server DELETE returned %s", job.pk, status)
        job.polling_url = ""  # gone on the server: nothing left to poll, re-fetch or delete
        job.save(update_fields=["polling_url"])
    try:
        shutil.rmtree(settings.FHIR_EXPORT_DIR / str(job.pk))
    except FileNotFoundError:
        pass  # nothing was downloaded, or an earlier run already removed it
    except OSError as exc:
        # The server DELETE is idempotent (404 is OK), so re-running after fixing this is safe.
        raise ExportFailed(f"Export {job.pk}: deleted on the server, but local files remain: {exc}") from exc
    logger.info("Export %s: local files removed", job.pk)


def _count_attempt(row):
    row.attempts += 1
    row.save(update_fields=["attempts"])


def _export(job, session, types):
    """Kick off an export of `types` for this job, wait for its manifest and record
    its files. A re-export replaces (marks `dead`) the files it supersedes."""
    previous_url = job.polling_url
    job.polling_url = fhir_client.with_retries(
        lambda: fhir_client.kickoff(session, types, job.since), on_attempt=lambda: _count_attempt(job)
    )
    job.status = ExportJob.Status.IN_PROGRESS
    job.save()
    logger.info("Export %s kicked off (%s): %s", job.pk, types, job.polling_url)
    manifest, response = _wait_for_manifest(job, session)

    with transaction.atomic():
        replaced = job.files.exclude(kind=ExportFile.Kind.IDS)
        if types != TYPES:
            replaced = replaced.filter(resource_type__in=types.split(","))
        replaced.update(status=ExportFile.Status.DEAD)
        _apply_manifest(job, manifest, response)

    if previous_url:
        # Whatever we still need from the old export is on disk or being replaced.
        try:
            fhir_client.delete(session, previous_url)
        except FhirError as exc:
            logger.warning("Export %s: could not DELETE the replaced export: %s", job.pk, exc)


def _export_ids(job, session, since):
    """Delta exports only (Plan.md: "Transaction Time"): the source may not report
    deletions, so we also ask for the ids of everything it still has (`_elements=id`,
    same `since` as the first export) and the migration removes what's missing.
    It runs after the data export, so everything that export brought is in the list.
    Only its files are needed, so it is DELETEd on the server once they're downloaded."""
    url = fhir_client.with_retries(
        lambda: fhir_client.kickoff(session, TYPES, since, ids_only=True), on_attempt=lambda: _count_attempt(job)
    )
    logger.info("Export %s: ID-only export kicked off: %s", job.pk, url)
    manifest, _ = _wait_for_manifest(job, session, url)
    with transaction.atomic():
        job.files.filter(kind=ExportFile.Kind.IDS).update(status=ExportFile.Status.DEAD)
        ExportFile.objects.bulk_create(
            ExportFile(export_job=job, kind=ExportFile.Kind.IDS, resource_type=entry["type"], url=entry["url"],
                       manifest_count=entry.get("count"))
            for entry in manifest.get("output", [])
        )
    for file in job.files.filter(kind=ExportFile.Kind.IDS, status=ExportFile.Status.PENDING):
        _download(job, file, session, refresh=False)  # a failure re-runs the whole ID-only export
    try:
        fhir_client.delete(session, url)
    except FhirError as exc:
        logger.warning("Export %s: could not DELETE the ID-only export: %s", job.pk, exc)


def _wait_for_manifest(job, session, url=None):
    """Poll with exponential backoff, waiting at least the server's Retry-After."""
    url = url or job.polling_url
    deadline = time.monotonic() + settings.FHIR_POLL_TIMEOUT
    attempt = 0
    while True:
        manifest, response = fhir_client.with_retries(lambda: fhir_client.poll(session, url))
        if manifest is not None:
            return manifest, response
        logger.info("Export %s: %s", job.pk, response.headers.get("X-Progress", "in progress"))
        delay = max(min(2**attempt, MAX_POLL_DELAY), fhir_client.retry_after_seconds(response))
        if time.monotonic() + delay > deadline:
            raise FhirError(f"Export not ready after {settings.FHIR_POLL_TIMEOUT}s")
        attempt += 1
        time.sleep(delay)


def _manifest_entries(manifest):
    return [(kind, entry) for key, kind in MANIFEST_KINDS.items() for entry in manifest.get(key, [])]


def _apply_manifest(job, manifest, response):
    if job.transaction_time is None:  # a re-export keeps the first one: the safer `since`
        job.transaction_time = parse_datetime(manifest["transactionTime"])
    job.expires_at = fhir_client.expires_at(response)
    job.save()
    ExportFile.objects.bulk_create(
        ExportFile(
            export_job=job,
            kind=kind,
            resource_type=entry["type"] if kind == ExportFile.Kind.OUTPUT else "",
            url=entry["url"],
            manifest_count=entry.get("count"),
        )
        for kind, entry in _manifest_entries(manifest)
    )


def _refresh_manifest(job, session):
    """Poll again for fresh links. URLs change, so files are matched by position:
    the current manifest's files are the last ones created, in manifest order."""
    manifest, response = fhir_client.with_retries(lambda: fhir_client.poll(session, job.polling_url))
    if manifest is None:
        raise FhirError("The polling URL no longer returns a manifest")
    entries = _manifest_entries(manifest)
    files = list(job.files.exclude(kind=ExportFile.Kind.IDS).order_by("-pk")[: len(entries)])[::-1]
    if [f.kind for f in files] != [kind for kind, _ in entries]:
        raise FhirError("The refreshed manifest doesn't match the recorded files")
    for file, (_, entry) in zip(files, entries):
        if file.url != entry["url"]:
            file.url = entry["url"]
            file.save(update_fields=["url"])
    job.expires_at = fhir_client.expires_at(response)
    job.save(update_fields=["expires_at"])


def _download_pending(job, session):
    pending = job.files.filter(status__in=[ExportFile.Status.PENDING, ExportFile.Status.FAILED])
    for file in pending.exclude(kind=ExportFile.Kind.IDS):
        _download(job, file, session)


def _download(job, file, session, refresh=True):
    """Retry with backoff; if that fails, re-fetch the manifest and try once more."""
    file.local_path.parent.mkdir(parents=True, exist_ok=True)

    def count_attempt():
        _count_attempt(file)

    def fetch():
        if refresh and job.expires_at and timezone.now() >= job.expires_at:
            logger.info("Export %s: links expired, fetching a fresh manifest", job.pk)
            _refresh_manifest(job, session)
            file.refresh_from_db(fields=["url"])
        return fhir_client.download(session, file.url, file.local_path)

    try:
        try:
            lines = fhir_client.with_retries(fetch, on_attempt=count_attempt)
        except FhirError as exc:
            if not refresh:
                raise
            logger.warning("%s: %s; re-fetching the manifest for one more try", file, exc)
            _refresh_manifest(job, session)
            file.refresh_from_db(fields=["url"])
            count_attempt()
            lines = fhir_client.download(session, file.url, file.local_path)
    except FhirError as exc:
        file.status = ExportFile.Status.FAILED
        file.save(update_fields=["status"])
        logger.error("%s failed: %s", file, exc)
        return
    file.status = ExportFile.Status.DOWNLOADED
    file.save(update_fields=["status"])
    logger.info("%s downloaded: %s lines", file, lines)


def _types_to_reexport(job):
    """The resource types of failed files. Deleted/outcome files have no type, so
    if one of those failed, everything is re-exported."""
    failed = job.files.filter(status=ExportFile.Status.FAILED).exclude(kind=ExportFile.Kind.IDS)
    if failed.exclude(kind=ExportFile.Kind.OUTPUT).exists():
        return TYPES
    return ",".join(sorted(set(failed.values_list("resource_type", flat=True))))


def _check_safe_to_delete(job):
    """After DELETE the manifest is gone for good, so only delete once nothing
    needs a retry: the latest migration completed and verification passes."""
    run = job.migration_runs.order_by("-pk").first()
    if not run or run.status != run.Status.COMPLETED:
        raise ExportFailed(f"Export {job.pk} has no completed migration run; use --force to delete anyway.")
    result = verification.verify(job)
    if result.status != result.Status.PASSED:
        raise ExportFailed(f"Export {job.pk} fails verification (run {result.pk}); use --force to delete anyway.")

"""Migration script (Plan.md: "Migration Script" and "Rollback").

Reads the downloaded NDJSON files in chunks and upserts each line by `fhir_id`,
so any part can be re-run safely. Progress lives in MigrationRun/MigrationChunk,
so an interrupted run resumes where it stopped.
"""

import json
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import connection, transaction

from patients.models import Observation, Patient

from . import verification
from .mapping import is_subsetted, map_observation, map_patient, patient_id_from_reference
from .models import ExportFile, ExportJob, MigrationChunk, MigrationRun, VerificationRun

logger = logging.getLogger(__name__)

MAX_VERIFY_ROUNDS = 3
MODELS = {"Patient": Patient, "Observation": Observation}
Phase = MigrationChunk.Phase
OK, ORPHAN, BAD = "ok", "orphan", "bad"


class MigrationFailed(Exception):
    pass


def migrate(export_job: ExportJob, workers: int = 4, chunk_size: int = 500) -> MigrationRun:
    """Run (or resume) the migration of a completed export."""
    if export_job.status != ExportJob.Status.COMPLETED:
        raise MigrationFailed(f"Export {export_job.pk} is {export_job.status}, not completed.")

    run = export_job.migration_runs.exclude(status=MigrationRun.Status.COMPLETED).order_by("-pk").first()
    if run:
        logger.info("Resuming migration run %s", run.pk)
        run.chunks.filter(status=MigrationChunk.Status.IN_PROGRESS).update(status=MigrationChunk.Status.PENDING)
    else:
        run = MigrationRun.objects.create(export_job=export_job, code_version=code_version())
    run.status = MigrationRun.Status.IN_PROGRESS
    run.save()

    try:
        # Phase 2 chunks only exist once the patients passed verification.
        if not run.chunks.filter(phase=Phase.OBSERVATIONS).exists():
            _process_phase(run, Phase.PATIENTS, workers, chunk_size)
            _verify_patients(run)
        _process_phase(run, Phase.OBSERVATIONS, workers, chunk_size)
        _apply_deletions(run)
        if export_job.files.filter(kind=ExportFile.Kind.IDS).exists():  # delta exports only
            _reconcile(run)
        run.status = MigrationRun.Status.COMPLETED
    except Exception:
        run.status = MigrationRun.Status.FAILED
        raise
    finally:
        run.save()
    return run


def reprocess_issues(verification_run: VerificationRun) -> int:
    """`migrate_fhir --issues`: re-process only the lines a verification flagged,
    marking each issue resolved once its line migrates."""
    issues = verification_run.issues.filter(resolved=False, export_file__isnull=False, line_no__isnull=False)
    resolved = 0
    for file in ExportFile.objects.filter(pk__in=issues.values("export_file")).select_related("export_job"):
        run = file.export_job.migration_runs.order_by("-pk").first()
        if run is None:
            raise MigrationFailed(f"Export {file.export_job_id} was never migrated.")
        file_issues = list(issues.filter(export_file=file))
        wanted = {issue.line_no for issue in file_issues}
        lines = {number: text for number, text in file.lines() if number in wanted}
        for issue in file_issues:
            with transaction.atomic():
                if _migrate_line(run, file, issue.line_no, lines.get(issue.line_no, "")) == BAD:
                    continue
                issue.resolved = True
                issue.save(update_fields=["resolved"])
                resolved += 1
    return resolved


def rollback(run: MigrationRun) -> tuple[int, int]:
    """Remove the rows a (bad) run wrote. Rows a later run re-wrote belong to that run."""
    observations, _ = Observation.objects.filter(migration_run=run).delete()
    patients, _ = Patient.objects.filter(migration_run=run).delete()
    return patients, observations


def code_version() -> str:
    if version := os.environ.get("CODE_VERSION"):
        return version
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=settings.BASE_DIR, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _files(export_job, kind, resource_type=""):
    return export_job.files.filter(kind=kind, resource_type=resource_type, status=ExportFile.Status.DOWNLOADED).order_by("pk")


def _process_phase(run, phase, workers, chunk_size):
    resource_type = "Patient" if phase == Phase.PATIENTS else "Observation"
    for file in _files(run.export_job, ExportFile.Kind.OUTPUT, resource_type):
        if not run.chunks.filter(export_file=file).exists():
            line_count = sum(1 for _ in file.lines())
            MigrationChunk.objects.bulk_create(
                MigrationChunk(
                    migration_run=run,
                    export_file=file,
                    phase=phase,
                    line_start=start,
                    line_end=min(start + chunk_size - 1, line_count),
                )
                for start in range(1, line_count + 1, chunk_size)
            )
    chunks = list(
        run.chunks.filter(phase=phase, status__in=[MigrationChunk.Status.PENDING, MigrationChunk.Status.FAILED])
        .select_related("migration_run", "export_file")
    )
    logger.info("Run %s phase %s: %s chunk(s) to process", run.pk, phase, len(chunks))
    if workers == 1:
        for chunk in chunks:
            _process_chunk(chunk)
    else:
        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(_process_chunk_in_thread, chunks))


def _process_chunk_in_thread(chunk):
    try:
        _process_chunk(chunk)
    finally:
        connection.close()  # each thread has its own DB connection


def _process_chunk(chunk):
    """A bad line doesn't stop its chunk: it's skipped and the chunk ends `failed`,
    for verification to catch. The chunk commits as a unit."""
    chunk.status = MigrationChunk.Status.IN_PROGRESS
    chunk.save(update_fields=["status"])
    with transaction.atomic():
        results = [
            _migrate_line(chunk.migration_run, chunk.export_file, number, text)
            for number, text in chunk.export_file.lines(chunk.line_start, chunk.line_end)
        ]
        chunk.status = MigrationChunk.Status.FAILED if BAD in results else MigrationChunk.Status.COMPLETED
        chunk.save(update_fields=["status"])
    logger.info(
        "Chunk %s (%s, lines %s-%s): %s, %s bad line(s), %s orphan(s)",
        chunk.pk, chunk.export_file, chunk.line_start, chunk.line_end,
        chunk.status, results.count(BAD), results.count(ORPHAN),
    )


def _migrate_line(run, file, line_no, text):
    """Map and upsert one NDJSON line. Returns OK, ORPHAN or BAD."""
    try:
        resource = json.loads(text)
        if resource.get("resourceType") != file.resource_type:
            raise ValueError(f"expected {file.resource_type}, got {resource.get('resourceType')}")
        fields = map_patient(resource) if file.resource_type == "Patient" else map_observation(resource)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        # Error type and message only: never the line itself, it may hold PHI.
        logger.warning("%s line %s skipped: %s: %s", file, line_no, type(exc).__name__, exc)
        return BAD

    fhir_id = fields.pop("fhir_id")
    defaults = {**fields, "migration_run": run, "source_file": file}
    result = OK
    if file.resource_type == "Observation":
        patient_id = patient_id_from_reference(fields["subject_reference"])
        defaults["patient"] = Patient.objects.filter(fhir_id=patient_id).first() if patient_id else None
        if defaults["patient"] is None:
            # Orphan: kept with a null FK and its subject_reference, so it can be linked later.
            logger.debug("%s line %s: Observation %s orphan (%s)", file, line_no, fhir_id, patient_id or "non-Patient subject")
            result = ORPHAN
    if is_subsetted(resource):
        # The IG: an incomplete resource must not overwrite a complete one. It may still create one.
        MODELS[file.resource_type].objects.get_or_create(fhir_id=fhir_id, defaults=defaults)
    else:
        MODELS[file.resource_type].objects.update_or_create(fhir_id=fhir_id, defaults=defaults)
    return result


def _verify_patients(run):
    """Phase 2 needs every patient in place: verify, re-process the flagged lines,
    and repeat at most MAX_VERIFY_ROUNDS times before stopping for manual review."""
    result = verification.verify(run.export_job, resource_type="Patient")
    for _ in range(MAX_VERIFY_ROUNDS):
        if result.status == result.Status.PASSED:
            return
        reprocess_issues(result)
        result = verification.verify(run.export_job, resource_type="Patient")
    if result.status != result.Status.PASSED:
        raise MigrationFailed(
            f"Patients still have issues after {MAX_VERIFY_ROUNDS} rounds (verification run {result.pk}). "
            "Fix the cause (usually the mapping), then re-run to resume."
        )


def _apply_deletions(run):
    """`deleted` files (only in `_since` exports): each line is a Bundle whose
    entries are `DELETE Type/id` requests for resources removed at the source."""
    for file in _files(run.export_job, ExportFile.Kind.DELETED):
        for _, text in file.lines():
            for entry in json.loads(text).get("entry", []):
                request = entry.get("request", {})
                parts = request.get("url", "").split("/")
                if request.get("method") == "DELETE" and len(parts) >= 2 and parts[0] in MODELS:
                    _delete_local(parts[0], parts[1])


def _reconcile(run):
    """Remove local records whose fhir_id is missing from the ids files: deleted at
    the source even if no tombstone said so. Checks every type before deleting any."""
    stale = {}
    for resource_type, model in MODELS.items():
        source_ids = {
            json.loads(text)["id"]
            for file in _files(run.export_job, ExportFile.Kind.IDS, resource_type)
            for _, text in file.lines()
        }
        # An empty or much shorter list more likely means a broken export than mass deletions.
        if not source_ids:
            raise MigrationFailed(f"The ID-only export has no {resource_type} ids; refusing to delete everything.")
        stale[resource_type] = list(model.objects.exclude(fhir_id__in=source_ids).values_list("fhir_id", flat=True))
        if len(stale[resource_type]) > model.objects.count() * settings.FHIR_RECONCILE_MAX_SHARE:
            raise MigrationFailed(
                f"The ID-only export would delete {len(stale[resource_type])} of {model.objects.count()} "
                f"{resource_type} records; refusing. Check the export, or raise FHIR_RECONCILE_MAX_SHARE."
            )
    for resource_type, fhir_ids in stale.items():
        for fhir_id in fhir_ids:
            _delete_local(resource_type, fhir_id)


def _delete_local(resource_type, fhir_id):
    deleted, _ = MODELS[resource_type].objects.filter(fhir_id=fhir_id).delete()
    if deleted:
        logger.info("%s %s deleted: removed at the source", resource_type, fhir_id)

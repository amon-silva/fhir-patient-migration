"""Verification script (Plan.md: "Verification Script" and "Validation").

Compares an export's NDJSON files with the database and stores every problem as
a VerificationIssue, so `migrate_fhir --issues` can re-process exactly those lines.
"""

import json
import logging
from collections import Counter

from patients.models import Observation, Patient

from .mapping import map_observation, map_patient, patient_id_from_reference
from .models import ExportFile, ExportJob, VerificationIssue, VerificationRun

logger = logging.getLogger(__name__)

RESOURCES = {"Patient": (Patient, map_patient), "Observation": (Observation, map_observation)}
Check = VerificationIssue.Check


def verify(export_job: ExportJob, resource_type: str = "") -> VerificationRun:
    """Verify an export (all types, or just `resource_type`) against the DB."""
    run = VerificationRun.objects.create()
    types = [resource_type] if resource_type else list(RESOURCES)
    files = export_job.files.exclude(status=ExportFile.Status.DEAD)
    migration_run = export_job.migration_runs.order_by("-pk").first()
    issues = []

    for file in files.exclude(status=ExportFile.Status.DOWNLOADED):
        if file.resource_type in types or not resource_type:
            issues.append(VerificationIssue(export_file=file, check_type=Check.DOWNLOAD))

    downloaded = files.filter(status=ExportFile.Status.DOWNLOADED)
    if not resource_type:
        # A partial success still returns 200: errors only show up in these files.
        for file in downloaded.filter(kind=ExportFile.Kind.OUTCOME):
            issues += _outcome_issues(file)
    deleted = _deleted_references(downloaded.filter(kind=ExportFile.Kind.DELETED))
    deleted |= _reconciled_references(downloaded.filter(kind__in=[ExportFile.Kind.OUTPUT, ExportFile.Kind.IDS]))
    for file in downloaded.filter(kind=ExportFile.Kind.OUTPUT, resource_type__in=types):
        issues += _check_file(file, migration_run, deleted)

    for issue in issues:
        issue.verification_run = run
    VerificationIssue.objects.bulk_create(issues)
    run.status = VerificationRun.Status.FAILED if issues else VerificationRun.Status.PASSED
    run.save()
    logger.info("Verification run %s %s: %s", run.pk, run.status, dict(Counter(str(i.check_type) for i in issues)))
    return run


def _outcome_issues(file):
    issues = []
    for line_no, text in file.lines():
        for item in json.loads(text).get("issue", []):
            logger.info("%s line %s: %s", file, line_no, item.get("diagnostics") or item.get("code"))
            issues.append(VerificationIssue(export_file=file, line_no=line_no, check_type=Check.OUTCOME))
    return issues


def _deleted_references(files):
    """'Type/id' of every resource the export's tombstones removed."""
    return {
        "/".join(entry["request"]["url"].split("/")[:2])
        for file in files
        for _, text in file.lines()
        for entry in json.loads(text).get("entry", [])
        if entry.get("request", {}).get("method") == "DELETE"
    }


def _reconciled_references(files):
    """'Type/id' of exported resources missing from the ids files (delta exports
    only): deleted at the source after the data export, so the reconcile removed them."""
    ids = {"output": set(), "ids": set()}
    for file in files:
        for _, text in file.lines():
            try:
                ids[file.kind].add(f"{file.resource_type}/{json.loads(text)['id']}")
            except (ValueError, KeyError, TypeError):
                pass  # a bad line: _check_file reports it
    return ids["output"] - ids["ids"] if ids["ids"] else set()


def _check_file(file, migration_run, deleted):
    model, mapper = RESOURCES[file.resource_type]
    issues = []
    mapped = {}  # fhir_id -> (line_no, fields)
    line_count = 0
    for line_no, text in file.lines():
        line_count = line_no
        try:
            fields = mapper(json.loads(text))
        except (ValueError, KeyError, TypeError, AttributeError):
            issues.append(VerificationIssue(export_file=file, line_no=line_no, check_type=Check.BAD_LINE))
            continue
        mapped[fields["fhir_id"]] = (line_no, fields)

    # Per file: manifest count (when sent) = lines in the file = lines the run read.
    chunks = migration_run.chunks.filter(export_file=file) if migration_run else []
    lines_read = sum(chunk.line_end - chunk.line_start + 1 for chunk in chunks)
    if lines_read != line_count or file.manifest_count not in (None, line_count):
        logger.info("%s: manifest count %s, %s lines, %s read", file, file.manifest_count, line_count, lines_read)
        issues.append(VerificationIssue(export_file=file, check_type=Check.COUNT))

    # Line by line: the row exists and holds what the mapping produces.
    rows = model.objects.in_bulk(list(mapped), field_name="fhir_id")
    for fhir_id, (line_no, fields) in mapped.items():
        row = rows.get(fhir_id)
        if row is None:
            if f"{file.resource_type}/{fhir_id}" not in deleted:
                issues.append(VerificationIssue(export_file=file, line_no=line_no, fhir_id=fhir_id, check_type=Check.MISSING))
            continue
        if row.source_file_id != file.pk:
            continue  # a newer export file has re-written this row since
        differing = [name for name, value in fields.items() if getattr(row, name) != value]
        if differing:
            # Field names only: the values are clinical data.
            logger.info("%s line %s: %s differs in %s", file, line_no, fhir_id, ", ".join(differing))
            issues.append(VerificationIssue(export_file=file, line_no=line_no, fhir_id=fhir_id, check_type=Check.MISMATCH))
        elif file.resource_type == "Observation" and row.patient_id is None and _is_linkable(row):
            issues.append(VerificationIssue(export_file=file, line_no=line_no, fhir_id=fhir_id, check_type=Check.ORPHAN_LINKABLE))
    return issues


def _is_linkable(observation):
    """An orphan is explained when its subject isn't a Patient, or its patient is
    still missing (logged at migration). If the patient exists now, it should be linked."""
    patient_id = patient_id_from_reference(observation.subject_reference)
    return bool(patient_id) and Patient.objects.filter(fhir_id=patient_id).exists()

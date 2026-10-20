"""Audit tables of the migration pipeline: see "Database schema" in Plan.md."""

from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models

if TYPE_CHECKING:
    from django.db.models.fields.related_descriptors import RelatedManager


class ExportJob(models.Model):
    if TYPE_CHECKING:  # reverse relations, for editors: Django adds them at runtime
        files: RelatedManager["ExportFile"]
        migration_runs: RelatedManager["MigrationRun"]

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress"  # kicked off: polling or downloading
        COMPLETED = "completed"  # every file downloaded
        FAILED = "failed"  # some files failed even after a re-export; re-run to retry
        DEAD = "dead"  # the kickoff gave up after max attempts; re-run to retry

    polling_url = models.URLField(max_length=512, blank=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.IN_PROGRESS)
    attempts = models.PositiveIntegerField(default=0)  # kickoffs, re-exports included
    since = models.DateTimeField(null=True)  # what we asked for; null on a full export
    transaction_time = models.DateTimeField(null=True)  # what the provider returned
    expires_at = models.DateTimeField(null=True)  # the manifest links' `Expires`


class ExportFile(models.Model):
    class Kind(models.TextChoices):
        OUTPUT = "output"  # resources
        DELETED = "deleted"  # tombstones (only with `_since`)
        OUTCOME = "outcome"  # OperationOutcome messages (the manifest's `error`)
        IDS = "ids"  # delta exports only: ids of everything the source still has

    class Status(models.TextChoices):
        PENDING = "pending"
        DOWNLOADED = "downloaded"
        FAILED = "failed"
        DEAD = "dead"  # given up and replaced by a re-export of its type

    if TYPE_CHECKING:
        export_job_id: int

    export_job = models.ForeignKey(ExportJob, on_delete=models.CASCADE, related_name="files")
    kind = models.CharField(max_length=16, choices=Kind)
    resource_type = models.CharField(max_length=64, blank=True)  # output and ids files only
    # An export file can live in cloud storage, where the URL carries a signature parameter
    # and gets long, so it has extra length for safety.
    url = models.URLField(max_length=2048)
    manifest_count = models.PositiveIntegerField(null=True)  # optional in the spec
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    attempts = models.PositiveIntegerField(default=0)

    @property
    def local_path(self):
        return Path(settings.FHIR_EXPORT_DIR) / str(self.export_job_id) / f"{self.pk}.ndjson"

    def lines(self, start=1, end=None):
        """(line_no, text) pairs of the downloaded file; 1-based, `end` inclusive."""
        with open(self.local_path, encoding="utf-8") as ndjson:
            yield from enumerate(islice(ndjson, start - 1, end), start=start)

    def __str__(self):
        return f"file {self.pk} ({self.kind} {self.resource_type})" if self.resource_type else f"file {self.pk} ({self.kind})"


class MigrationRun(models.Model):
    if TYPE_CHECKING:
        chunks: RelatedManager["MigrationChunk"]

    class Status(models.TextChoices):
        PENDING = "pending"
        IN_PROGRESS = "in_progress"
        COMPLETED = "completed"
        FAILED = "failed"

    export_job = models.ForeignKey(ExportJob, on_delete=models.PROTECT, related_name="migration_runs")
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)
    code_version = models.CharField(max_length=64)  # which mapping code wrote the rows


class MigrationChunk(models.Model):
    class Phase(models.IntegerChoices):
        PATIENTS = 1
        OBSERVATIONS = 2

    class Status(models.TextChoices):
        PENDING = "pending"
        IN_PROGRESS = "in_progress"
        COMPLETED = "completed"
        FAILED = "failed"  # some lines were skipped; verification catches them

    migration_run = models.ForeignKey(MigrationRun, on_delete=models.CASCADE, related_name="chunks")
    export_file = models.ForeignKey(ExportFile, on_delete=models.PROTECT)
    phase = models.PositiveSmallIntegerField(choices=Phase)
    line_start = models.PositiveIntegerField()  # 1-based, inclusive
    line_end = models.PositiveIntegerField()  # inclusive
    status = models.CharField(max_length=16, choices=Status, default=Status.PENDING)


class VerificationRun(models.Model):
    if TYPE_CHECKING:
        issues: RelatedManager["VerificationIssue"]

    class Status(models.TextChoices):
        IN_PROGRESS = "in_progress"
        PASSED = "passed"
        FAILED = "failed"

    status = models.CharField(max_length=16, choices=Status, default=Status.IN_PROGRESS)


class VerificationIssue(models.Model):
    class Check(models.TextChoices):
        DOWNLOAD = "download"  # a manifest file isn't downloaded
        COUNT = "count"  # manifest count / lines in file / lines read disagree
        OUTCOME = "outcome"  # an entry in the manifest's outcome files
        BAD_LINE = "bad_line"  # the line can't be parsed or mapped
        MISSING = "missing"  # the line's resource isn't in the DB
        MISMATCH = "mismatch"  # it is, but fields differ from the source
        ORPHAN_LINKABLE = "orphan_linkable"  # orphan whose patient exists now

    verification_run = models.ForeignKey(VerificationRun, on_delete=models.CASCADE, related_name="issues")
    export_file = models.ForeignKey(ExportFile, null=True, on_delete=models.PROTECT)
    line_no = models.PositiveIntegerField(null=True)
    fhir_id = models.CharField(max_length=64, blank=True)
    # `check` would shadow Django's Model.check(); the column keeps the Plan's name.
    check_type = models.CharField(max_length=16, choices=Check, db_column="check")
    resolved = models.BooleanField(default=False)

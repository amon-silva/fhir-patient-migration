from collections import Counter

from django.core.management.base import BaseCommand, CommandError

from fhir_migration.models import ExportJob, VerificationRun
from fhir_migration.verification import verify


class Command(BaseCommand):
    help = "Compare an export's NDJSON files with the database and record every issue."

    def add_arguments(self, parser):
        parser.add_argument("export_id", type=int)
        parser.add_argument("--type", default="", choices=["", "Patient", "Observation"])

    def handle(self, *args, **options):
        export_job = ExportJob.objects.filter(pk=options["export_id"]).first()
        if export_job is None:
            raise CommandError(f"Export {options['export_id']} not found.")

        run = verify(export_job, resource_type=options["type"])
        if run.status == VerificationRun.Status.FAILED:
            counts = Counter(run.issues.values_list("check_type", flat=True))
            raise CommandError(
                f"Verification run {run.pk}: {sum(counts.values())} issue(s) {dict(counts)}. "
                f"Fix the mapping if needed, then: migrate_fhir --issues {run.pk}"
            )
        self.stdout.write(f"Verification run {run.pk} passed. Next: delete_fhir_export {export_job.pk}")

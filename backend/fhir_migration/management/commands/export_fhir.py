from django.core.management.base import BaseCommand, CommandError

from fhir_migration.export import ExportFailed, next_since, run_export
from fhir_migration.models import ExportFile, ExportJob


class Command(BaseCommand):
    help = (
        "Export Patients and Observations with FHIR Bulk Data ($export) and download the NDJSON files. "
        "Resumes the latest export if it didn't finish. Otherwise the first export is full and each later "
        "one is a delta from the previous export's transactionTime (never typed by hand: a wrong `since` "
        "would silently skip changes). A delta export also fetches the ids the source still has, "
        "to remove what it deleted."
    )

    def handle(self, *args, **options):
        job = self.choose_export()
        self.stdout.write(f"Export {job.pk} (since {job.since.isoformat() if job.since else 'the beginning'})")

        try:
            run_export(job)
        except ExportFailed as exc:
            raise CommandError(str(exc)) from exc
        files = job.files.filter(status=ExportFile.Status.DOWNLOADED).count()
        self.stdout.write(
            f"Export {job.pk} completed: {files} file(s), transactionTime {job.transaction_time.isoformat()}. "
            f"Next: migrate_fhir {job.pk}"
        )

    def choose_export(self):
        """Resume the latest export if it didn't finish; otherwise start a new one.
        The latest export's files must finish downloading before a new export starts,
        to not break the `since` chain."""
        latest_export = ExportJob.objects.order_by("-pk").first()
        if latest_export and latest_export.status != ExportJob.Status.COMPLETED:
            self.stdout.write(f"Resuming export {latest_export.pk} ({latest_export.status})")
            return latest_export
        return ExportJob.objects.create(since=next_since())

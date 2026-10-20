from django.core.management.base import BaseCommand, CommandError

from fhir_migration.export import ExportFailed, delete_export
from fhir_migration.models import ExportJob


class Command(BaseCommand):
    help = "DELETE the export on the FHIR server and remove our downloaded NDJSON files."

    def add_arguments(self, parser):
        parser.add_argument("export_id", type=int)
        parser.add_argument("--force", action="store_true", help="delete a completed export even if it isn't migrated and verified")

    def handle(self, *args, **options):
        job = ExportJob.objects.filter(pk=options["export_id"]).first()
        if job is None:
            raise CommandError(f"Export {options['export_id']} not found.")
        try:
            delete_export(job, force=options["force"])
        except ExportFailed as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Export {job.pk} deleted on the server and locally.")

from django.core.management.base import BaseCommand, CommandError

from fhir_migration import migration
from fhir_migration.models import ExportJob, MigrationRun, VerificationRun


class Command(BaseCommand):
    help = "Load an export's NDJSON files into the database (resumes an unfinished run)."

    def add_arguments(self, parser):
        target = parser.add_mutually_exclusive_group(required=True)
        target.add_argument("export_id", nargs="?", type=int)
        target.add_argument("--issues", type=int, metavar="VERIFICATION_RUN_ID", help="re-process only the flagged lines")
        target.add_argument("--rollback", type=int, metavar="MIGRATION_RUN_ID", help="remove the rows a run wrote")
        parser.add_argument("--workers", type=int, default=4)
        parser.add_argument("--chunk-size", type=int, default=500)

    def handle(self, *args, **options):
        if options["issues"]:
            return self.reprocess(options["issues"])
        if options["rollback"]:
            return self.rollback(options["rollback"])

        export_job = ExportJob.objects.filter(pk=options["export_id"]).first()
        if export_job is None:
            raise CommandError(f"Export {options['export_id']} not found.")
        try:
            run = migration.migrate(export_job, options["workers"], options["chunk_size"])
        except migration.MigrationFailed as exc:
            raise CommandError(str(exc)) from exc
        failed_chunks = run.chunks.filter(status="failed").count()
        self.stdout.write(
            f"Migration run {run.pk} {run.status} ({failed_chunks} chunk(s) with skipped lines). "
            f"Next: verify_fhir {export_job.pk}"
        )

    def reprocess(self, verification_run_id):
        verification_run = VerificationRun.objects.filter(pk=verification_run_id).first()
        if verification_run is None:
            raise CommandError(f"Verification run {verification_run_id} not found.")
        try:
            resolved = migration.reprocess_issues(verification_run)
        except migration.MigrationFailed as exc:
            raise CommandError(str(exc)) from exc
        remaining = verification_run.issues.filter(resolved=False).count()
        self.stdout.write(f"Resolved {resolved} issue(s), {remaining} remain. Run verify_fhir again to confirm.")

    def rollback(self, run_id):
        run = MigrationRun.objects.filter(pk=run_id).first()
        if run is None:
            raise CommandError(f"Migration run {run_id} not found.")
        patients, observations = migration.rollback(run)
        self.stdout.write(f"Rolled back run {run.pk}: removed {patients} patients, {observations} observations.")

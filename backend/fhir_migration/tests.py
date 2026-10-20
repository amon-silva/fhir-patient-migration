import io
import json
import logging
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from patients.models import Observation, Patient

from . import export, fhir_client, migration
from .fhir_client import FhirError
from .mapping import map_observation, map_patient, patient_id_from_reference
from .models import ExportFile, ExportJob, MigrationChunk, MigrationRun, VerificationIssue, VerificationRun
from .verification import verify

logging.disable(logging.CRITICAL)

# Synthetic FHIR resources, shaped like the HAPI sandbox's.
PATIENT = {
    "resourceType": "Patient",
    "id": "p1",
    "identifier": [
        {"system": "urn:other", "value": "X-9"},
        {"type": {"coding": [{"code": "MR"}]}, "value": "MRN-1"},
    ],
    "name": [
        {"use": "nickname", "given": ["Jo"]},
        {"use": "official", "family": "Doe", "given": ["Jane", "Q"]},
    ],
    "gender": "female",
    "birthDate": "1980-05-01",
}
OBSERVATION = {
    "resourceType": "Observation",
    "id": "o1",
    "status": "final",
    "subject": {"reference": "Patient/p1"},
    "code": {"coding": [{"system": "http://loinc.org", "code": "777-3", "display": "Platelets"}]},
    "effectiveDateTime": "2024-01-02T10:00:00-04:00",
    "valueQuantity": {"value": 250, "unit": "10*3/uL"},
    "referenceRange": [{"low": {"value": 150}, "high": {"value": 400}}],
}


def patient(fhir_id, **fields):
    return {**PATIENT, "id": fhir_id, **fields}


def observation(fhir_id, subject="Patient/p1", **fields):
    return {**OBSERVATION, "id": fhir_id, "subject": {"reference": subject}, **fields}


class MappingTests(SimpleTestCase):
    def test_patient_uses_official_name_and_mr_identifier(self):
        fields = map_patient(PATIENT)
        self.assertEqual((fields["family_name"], fields["given_names"]), ("Doe", "Jane Q"))
        self.assertEqual(fields["mrn"], "MRN-1")
        self.assertEqual(fields["birth_date"], date(1980, 5, 1))

    def test_patient_with_minimal_data_and_partial_birth_date(self):
        fields = map_patient({"id": "p2", "birthDate": "1980"})
        self.assertEqual(fields["family_name"], "")
        self.assertIsNone(fields["mrn"])
        self.assertIsNone(fields["birth_date"])

    def test_observation_flattens_code_value_and_range(self):
        fields = map_observation(OBSERVATION)
        self.assertEqual((fields["code_system"], fields["code"]), ("http://loinc.org", "777-3"))
        self.assertEqual(fields["value_quantity"], {"value": 250, "unit": "10*3/uL"})
        self.assertEqual((fields["ref_low"], fields["ref_high"]), (150, 400))
        self.assertEqual(fields["effective_at"], datetime(2024, 1, 2, 14, tzinfo=timezone.utc))

    def test_observation_without_status_is_rejected(self):
        # FHIR requires Observation.status; the sandbox has a few without it.
        with self.assertRaises(KeyError):
            map_observation({"id": "o2"})

    def test_patient_id_from_reference(self):
        self.assertEqual(patient_id_from_reference("Patient/1"), "1")
        self.assertEqual(patient_id_from_reference("https://x/baseR4/Patient/1/_history/2"), "1")
        self.assertIsNone(patient_id_from_reference("Group/7"))


@mock.patch("time.sleep")
class RetryTests(SimpleTestCase):
    def test_retries_transient_errors_with_backoff_and_retry_after(self, sleep):
        call = mock.Mock(side_effect=[FhirError("503", transient=True), FhirError("429", transient=True, retry_after=30), "ok"])
        self.assertEqual(fhir_client.with_retries(call), "ok")
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 30])  # backoff, then Retry-After

    def test_does_not_retry_client_errors(self, sleep):
        call = mock.Mock(side_effect=FhirError("400", status=400))
        with self.assertRaises(FhirError):
            fhir_client.with_retries(call)
        self.assertEqual(call.call_count, 1)

    def test_gives_up_after_max_attempts(self, sleep):
        call = mock.Mock(side_effect=FhirError("down", transient=True))
        with self.assertRaises(FhirError):
            fhir_client.with_retries(call, attempts=3)
        self.assertEqual(call.call_count, 3)


class KickoffTests(SimpleTestCase):
    def test_sends_every_type_in_one_parameter_and_asks_only_for_ids(self):
        session = mock.Mock()
        session.request.return_value = mock.Mock(status_code=202, headers={"Content-Location": "https://fhir.test/poll"})
        fhir_client.kickoff(session, "Patient,Observation", ids_only=True)
        parameters = session.request.call_args.kwargs["json"]["parameter"]
        # HAPI drops all but the first of repeated `_type` parameters, so they go in one, comma-joined.
        self.assertEqual([p for p in parameters if p["name"] == "_type"], [{"name": "_type", "valueString": "Patient,Observation"}])
        self.assertIn({"name": "_elements", "valueString": "id"}, parameters)


class TempExportDirMixin(SimpleTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        override = override_settings(FHIR_EXPORT_DIR=Path(tmp.name))
        override.enable()
        self.addCleanup(override.disable)


def manifest(*entries, key="output"):
    return {"transactionTime": "2026-10-20T00:00:00Z", key: [{"type": t, "url": u} for t, u in entries]}


@mock.patch("time.sleep")
class ExportTests(TempExportDirMixin, TestCase):
    def fake_download(self, failing_urls=()):
        def download(session, url, path):
            if url in failing_urls:
                raise FhirError(f"GET {url} returned 500", status=500, transient=True)
            path.write_text(json.dumps(PATIENT) + "\n")
            return 1

        return download

    def test_polls_until_ready_and_downloads_every_file(self, sleep):
        polls = [(None, mock.Mock(headers={"X-Progress": "50%", "Retry-After": "120"})),
                 (manifest(("Patient", "u1"), ("Observation", "u2")), mock.Mock(headers={}))]
        with (
            mock.patch.object(fhir_client, "kickoff", return_value="https://fhir.test/poll"),
            mock.patch.object(fhir_client, "poll", side_effect=polls),
            mock.patch.object(fhir_client, "download", side_effect=self.fake_download()),
        ):
            job = export.run_export(ExportJob.objects.create())

        self.assertEqual(job.status, ExportJob.Status.COMPLETED)
        self.assertEqual(sleep.call_args.args[0], 120)  # waited at least Retry-After
        self.assertEqual(set(job.files.values_list("status", flat=True)), {"downloaded"})

    def test_delta_export_also_downloads_the_ids_the_source_still_has(self, sleep):
        ExportJob.objects.create(status=ExportJob.Status.COMPLETED, since=None)  # the first export
        ready = mock.Mock(headers={})
        with (
            mock.patch.object(fhir_client, "kickoff", side_effect=["https://fhir.test/data", "https://fhir.test/ids"]) as kickoff,
            mock.patch.object(fhir_client, "poll", side_effect=[(manifest(("Patient", "u1")), ready),
                                                               (manifest(("Patient", "ids-url")), ready)]),
            mock.patch.object(fhir_client, "download", side_effect=self.fake_download()),
            mock.patch.object(fhir_client, "delete", return_value=202) as delete,
        ):
            job = export.run_export(ExportJob.objects.create(since=datetime(2026, 10, 19, tzinfo=timezone.utc)))

        self.assertEqual(kickoff.call_args.kwargs, {"ids_only": True})  # after the data export
        self.assertEqual(kickoff.call_args.args[2], None)  # the first export's `since`
        delete.assert_called_once_with(mock.ANY, "https://fhir.test/ids")  # only its files were needed
        self.assertEqual(job.polling_url, "https://fhir.test/data")  # kept for the Delete script
        self.assertEqual(job.files.get(kind=ExportFile.Kind.IDS).status, ExportFile.Status.DOWNLOADED)

    def test_kickoff_failing_after_max_attempts_marks_the_job_dead(self, sleep):
        with mock.patch.object(fhir_client, "kickoff", side_effect=FhirError("503", transient=True)):
            with self.assertRaises(export.ExportFailed):
                export.run_export(ExportJob.objects.create())
        job = ExportJob.objects.get()
        self.assertEqual((job.status, job.attempts), (ExportJob.Status.DEAD, 5))

    def test_failing_download_falls_back_to_re_exporting_its_type(self, sleep):
        first = manifest(("Patient", "p-url"), ("Observation", "bad-url"))
        second = manifest(("Observation", "new-url"))
        polls = [(first, mock.Mock(headers={})),  # first export
                 (first, mock.Mock(headers={})),  # manifest re-fetched after retries failed
                 (second, mock.Mock(headers={}))]  # re-export of Observation
        with (
            mock.patch.object(fhir_client, "kickoff", side_effect=["https://fhir.test/1", "https://fhir.test/2"]) as kickoff,
            mock.patch.object(fhir_client, "poll", side_effect=polls),
            mock.patch.object(fhir_client, "download", side_effect=self.fake_download({"bad-url"})),
            mock.patch.object(fhir_client, "delete", return_value=202) as delete,
        ):
            job = export.run_export(ExportJob.objects.create())

        self.assertEqual(kickoff.call_args.args[1], "Observation")
        delete.assert_called_once_with(mock.ANY, "https://fhir.test/1")  # replaced export cleaned up
        self.assertEqual((job.status, job.attempts), (ExportJob.Status.COMPLETED, 2))
        self.assertEqual(job.files.get(url="bad-url").status, ExportFile.Status.DEAD)
        self.assertEqual(job.files.get(url="new-url").status, ExportFile.Status.DOWNLOADED)


class ExportCommandTests(TestCase):
    def export(self, *args):
        def complete(job):
            job.status, job.transaction_time = ExportJob.Status.COMPLETED, datetime(2026, 10, 20, tzinfo=timezone.utc)
            job.save()

        with mock.patch("fhir_migration.management.commands.export_fhir.run_export", side_effect=complete) as run:
            call_command("export_fhir", *args, stdout=io.StringIO())
        return run.call_args.args[0]

    def test_resumes_an_unfinished_export_else_continues_the_since_chain(self):
        dead = ExportJob.objects.create(status=ExportJob.Status.DEAD, since=datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.assertEqual(self.export(), dead)  # no argument: resume the latest unfinished export
        dead.refresh_from_db()
        self.assertEqual(self.export().since, dead.transaction_time)  # then a delta from it


class MigrationTests(TempExportDirMixin, TestCase):
    def make_export(self, files):
        """files: [(kind, resource_type, [resource dicts or raw lines])] already downloaded."""
        job = ExportJob.objects.create(status=ExportJob.Status.COMPLETED, polling_url="https://fhir.test/poll")
        for kind, resource_type, lines in files:
            file = ExportFile.objects.create(
                export_job=job, kind=kind, resource_type=resource_type, url="u", status=ExportFile.Status.DOWNLOADED
            )
            file.local_path.parent.mkdir(parents=True, exist_ok=True)
            file.local_path.write_text("".join((l if isinstance(l, str) else json.dumps(l)) + "\n" for l in lines))
        return job

    def migrate(self, job):
        return migration.migrate(job, workers=1, chunk_size=2)

    def test_migrates_patients_then_observations_and_is_idempotent(self):
        job = self.make_export([
            ("output", "Patient", [patient("p1"), patient("p2"), patient("p3")]),
            ("output", "Observation", [observation("o1"), observation("o2", subject="Group/7"), observation("o3", subject="Patient/nope")]),
        ])
        run = self.migrate(job)
        self.migrate(job)  # re-running upserts, never duplicates

        self.assertEqual(run.status, MigrationRun.Status.COMPLETED)
        self.assertEqual((Patient.objects.count(), Observation.objects.count()), (3, 3))
        self.assertEqual(Observation.objects.get(fhir_id="o1").patient.fhir_id, "p1")
        orphan = Observation.objects.get(fhir_id="o3")
        self.assertEqual((orphan.patient, orphan.subject_reference), (None, "Patient/nope"))
        self.assertEqual(run.chunks.filter(phase=MigrationChunk.Phase.PATIENTS).count(), 2)  # 3 lines, chunks of 2
        self.assertEqual(verify(job).status, VerificationRun.Status.PASSED)  # orphans are all explained

    def test_patients_failing_verification_stop_the_run_before_observations(self):
        job = self.make_export([
            ("output", "Patient", [patient("p1"), "{not json"]),
            ("output", "Observation", [observation("o1")]),
        ])
        with self.assertRaises(migration.MigrationFailed):
            self.migrate(job)

        run = MigrationRun.objects.get()
        self.assertEqual(run.status, MigrationRun.Status.FAILED)
        self.assertFalse(run.chunks.filter(phase=MigrationChunk.Phase.OBSERVATIONS).exists())
        self.assertEqual(Observation.objects.count(), 0)
        self.assertEqual(VerificationRun.objects.count(), 4)  # first check + 3 fix-and-verify rounds

    def test_orphans_are_linked_by_reprocessing_issues_once_their_patient_exists(self):
        job = self.make_export([("output", "Patient", [patient("p1")]), ("output", "Observation", [observation("o1", subject="Patient/p2")])])
        self.migrate(job)
        later = self.make_export([("output", "Patient", [patient("p2")])])
        self.migrate(later)

        result = verify(job)
        issue = result.issues.get()
        self.assertEqual((issue.check_type, issue.line_no), (VerificationIssue.Check.ORPHAN_LINKABLE, 1))
        self.assertEqual(migration.reprocess_issues(result), 1)
        self.assertEqual(Observation.objects.get(fhir_id="o1").patient.fhir_id, "p2")
        self.assertEqual(verify(job).status, VerificationRun.Status.PASSED)

    def test_verification_flags_missing_and_changed_rows(self):
        job = self.make_export([("output", "Patient", [patient("p1"), patient("p2")])])
        self.migrate(job)
        Patient.objects.filter(fhir_id="p1").update(family_name="Changed")
        Patient.objects.filter(fhir_id="p2").delete()

        issues = verify(job).issues.order_by("line_no")
        self.assertEqual([(i.line_no, i.check_type) for i in issues], [(1, "mismatch"), (2, "missing")])

    def test_tombstones_and_id_only_reconcile_remove_local_records(self):
        job = self.make_export([
            ("output", "Patient", [patient("p1"), patient("p2"), patient("p3")]),
            ("deleted", "", [{"resourceType": "Bundle", "entry": [{"request": {"method": "DELETE", "url": "Patient/p2"}}]}]),
        ])
        self.migrate(job)
        self.assertEqual(set(Patient.objects.values_list("fhir_id", flat=True)), {"p1", "p3"})
        self.assertEqual(verify(job).status, VerificationRun.Status.PASSED)  # p2's absence is explained

    @override_settings(FHIR_RECONCILE_MAX_SHARE=0.5)
    def test_delta_ids_files_remove_what_the_source_deleted_silently(self):
        self.migrate(self.make_export([("output", "Patient", [patient("p1"), patient("p2"), patient("p3")])]))
        # A delta with no tombstones: p3 is gone from the source, p4 was deleted right after the data export.
        delta = self.make_export([
            ("output", "Patient", [patient("p1"), patient("p4")]),
            ("ids", "Patient", [{"resourceType": "Patient", "id": "p1"}, {"resourceType": "Patient", "id": "p2"}]),
            ("ids", "Observation", [{"resourceType": "Observation", "id": "x"}]),
        ])
        self.migrate(delta)
        self.assertEqual(set(Patient.objects.values_list("fhir_id", flat=True)), {"p1", "p2"})
        self.assertEqual(verify(delta).status, VerificationRun.Status.PASSED)  # p4's absence is explained

    def test_reconcile_refuses_to_delete_too_much(self):
        self.migrate(self.make_export([("output", "Patient", [patient(f"p{n}") for n in range(5)])]))
        delta = self.make_export([("ids", "Patient", [{"resourceType": "Patient", "id": "p0"}]),
                                  ("ids", "Observation", [{"resourceType": "Observation", "id": "x"}])])
        with self.assertRaises(migration.MigrationFailed):
            self.migrate(delta)
        self.assertEqual(Patient.objects.count(), 5)  # nothing deleted

    def test_subsetted_resources_create_but_never_overwrite(self):
        self.migrate(self.make_export([("output", "Patient", [patient("p1")])]))
        subsetted = {"meta": {"tag": [{"code": "SUBSETTED"}]}, "name": [], "gender": "unknown"}
        job = self.make_export([("output", "Patient", [patient("p1", **subsetted), patient("p2", **subsetted)])])
        self.migrate(job)
        self.assertEqual(Patient.objects.get(fhir_id="p1").family_name, "Doe")  # complete record kept
        self.assertEqual(Patient.objects.get(fhir_id="p2").gender, "unknown")  # new record created
        self.assertEqual(verify(job).status, VerificationRun.Status.PASSED)

    def test_rollback_removes_the_rows_of_a_run(self):
        job = self.make_export([("output", "Patient", [patient("p1")]), ("output", "Observation", [observation("o1")])])
        run = self.migrate(job)
        self.assertEqual(migration.rollback(run), (1, 1))
        self.assertFalse(Patient.objects.exists())

    def test_delete_refuses_until_migrated_and_verified(self):
        job = self.make_export([("output", "Patient", [patient("p1")])])
        with self.assertRaises(export.ExportFailed):
            export.delete_export(job)

        self.migrate(job)
        with mock.patch.object(fhir_client, "delete", return_value=202) as delete:
            export.delete_export(job)
        delete.assert_called_once()
        self.assertFalse(job.files.get().local_path.exists())

    def test_delete_fails_loudly_when_local_files_remain(self):
        job = self.make_export([("output", "Patient", [patient("p1")])])
        self.migrate(job)
        with mock.patch.object(fhir_client, "delete", return_value=202), \
                mock.patch.object(export.shutil, "rmtree", side_effect=PermissionError("denied")):
            with self.assertRaises(export.ExportFailed):
                export.delete_export(job)
        self.assertTrue(job.files.get().local_path.exists())

        with mock.patch.object(fhir_client, "delete") as delete:
            export.delete_export(job)  # the re-run only removes the local files: the server part is done
            export.delete_export(job)  # and running it again is fine
        delete.assert_not_called()
        self.assertFalse(job.files.get().local_path.exists())

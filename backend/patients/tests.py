from django.test import TestCase

from fhir_migration.models import ExportJob, MigrationRun

from .models import Observation, Patient


class ApiTests(TestCase):
    def setUp(self):
        run = MigrationRun.objects.create(export_job=ExportJob.objects.create(), code_version="test")
        self.patient = Patient.objects.create(fhir_id="p1", family_name="Doe", migration_run=run)
        Observation.objects.create(fhir_id="o1", patient=self.patient, status="final", code="777-3", migration_run=run)

    def test_list_patients_with_observation_count(self):
        response = self.client.get("/api/patients/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["observation_count"], 1)

    def test_patient_detail_includes_observations(self):
        response = self.client.get(f"/api/patients/{self.patient.pk}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([observations["code"] for observations in response.json()["observations"]], ["777-3"])

    def test_unknown_patient_returns_404(self):
        self.assertEqual(self.client.get("/api/patients/999/").status_code, 404)

"""The internal service's model. Fields follow the "Data mapping" tables in Plan.md."""

from django.db import models


class Patient(models.Model):
    fhir_id = models.CharField(max_length=64, unique=True)
    mrn = models.CharField(max_length=64, null=True)
    family_name = models.CharField(max_length=255, blank=True)
    given_names = models.CharField(max_length=255, blank=True)
    gender = models.CharField(max_length=16, blank=True)
    birth_date = models.DateField(null=True)
    active = models.BooleanField(null=True)
    managing_org_ref = models.CharField(max_length=255, blank=True)
    migration_run = models.ForeignKey("fhir_migration.MigrationRun", on_delete=models.PROTECT)
    source_file = models.ForeignKey("fhir_migration.ExportFile", null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["family_name", "given_names"]


class Observation(models.Model):
    fhir_id = models.CharField(max_length=64, unique=True)
    # Null for orphans: the subject isn't a Patient, or that patient wasn't migrated.
    # SET_NULL so a patient deleted at the source never silently takes clinical data with it.
    patient = models.ForeignKey(
        Patient, null=True, on_delete=models.SET_NULL, related_name="observations"
    )
    subject_reference = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32)
    code_system = models.CharField(max_length=255, blank=True)
    code = models.CharField(max_length=64, blank=True)
    code_display = models.CharField(max_length=512, blank=True)
    effective_at = models.DateTimeField(null=True)
    issued_at = models.DateTimeField(null=True)
    value_quantity = models.JSONField(null=True)
    interpretation_code = models.CharField(max_length=32, blank=True)
    ref_low = models.FloatField(null=True)
    ref_high = models.FloatField(null=True)
    performer_ref = models.CharField(max_length=255, blank=True)
    migration_run = models.ForeignKey("fhir_migration.MigrationRun", on_delete=models.PROTECT)
    source_file = models.ForeignKey("fhir_migration.ExportFile", null=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-effective_at"]

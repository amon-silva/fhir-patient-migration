from django.apps import AppConfig


class FhirMigrationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'fhir_migration'

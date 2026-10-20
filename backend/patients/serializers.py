from rest_framework import serializers

from .models import Observation, Patient


class ObservationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Observation
        exclude = ["patient", "migration_run", "source_file"]


class PatientListSerializer(serializers.ModelSerializer):
    observation_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Patient
        fields = ["id", "fhir_id", "family_name", "given_names", "gender", "birth_date", "observation_count"]


class PatientDetailSerializer(serializers.ModelSerializer):
    observations = ObservationSerializer(many=True, read_only=True)

    class Meta:
        model = Patient
        exclude = ["migration_run", "source_file"]

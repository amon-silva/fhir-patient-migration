from django.db.models import Count
from rest_framework import generics

from .models import Patient
from .serializers import PatientDetailSerializer, PatientListSerializer


class PatientList(generics.ListAPIView):
    queryset = Patient.objects.annotate(observation_count=Count("observations"))
    serializer_class = PatientListSerializer


class PatientDetail(generics.RetrieveAPIView):
    queryset = Patient.objects.prefetch_related("observations")
    serializer_class = PatientDetailSerializer

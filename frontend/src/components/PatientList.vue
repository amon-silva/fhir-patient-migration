<script setup lang="ts">
import { computed, ref } from 'vue'
import { useFetch } from '@/useFetch'

export interface PatientSummary {
  id: number
  fhir_id: string
  family_name: string
  given_names: string
  gender: string
  birth_date: string | null
  observation_count: number
}

defineProps<{ selectedId: number | null }>()
const emit = defineEmits<{ select: [id: number] }>()

const { data: patients, error, loading } = useFetch<PatientSummary[]>('/api/patients/')

// Client-side only: the API still returns every patient.
const onlyWithObservations = ref(true)
const visiblePatients = computed(() =>
  onlyWithObservations.value
    ? patients.value?.filter((patient) => patient.observation_count > 0)
    : patients.value,
)
</script>

<template>
  <section>
    <h2>Patients</h2>
    <label>
      <input v-model="onlyWithObservations" type="checkbox" />
      Only with observations
    </label>
    <p v-if="patients"><small>Showing {{ visiblePatients?.length }} of {{ patients.length }}</small></p>
    <p v-if="loading">Loading…</p>
    <p v-else-if="error" role="alert">Could not load patients: {{ error }}</p>
    <p v-else-if="!patients?.length">
      No patients yet. Run <code>python manage.py migrate_fhir</code> in the backend.
    </p>
    <p v-else-if="!visiblePatients?.length">No patients with observations.</p>
    <ul v-else>
      <li v-for="patient in visiblePatients" :key="patient.id">
        <button
          :aria-pressed="patient.id === selectedId"
          @click="emit('select', patient.id)"
        >
          {{ patient.family_name || '(no family name)' }}, {{ patient.given_names }}
        </button>
        <small>
          {{ patient.gender || 'unknown' }} · {{ patient.birth_date ?? 'no birth date' }} ·
          {{ patient.observation_count }} obs
        </small>
      </li>
    </ul>
  </section>
</template>

<style scoped>
ul {
  list-style: none;
  padding: 0;
}
li {
  margin-bottom: 0.5rem;
}
button {
  display: block;
  cursor: pointer;
}
button[aria-pressed='true'] {
  font-weight: bold;
}
</style>

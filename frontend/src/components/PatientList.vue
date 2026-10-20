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

const emit = defineEmits<{ select: [id: number] }>()

const { data: patients, error, loading } = useFetch<PatientSummary[]>('/api/patients/')

// Client-side only: the API still returns every patient.
const onlyWithObservations = ref(false)
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
      No patients yet.
    </p>
    <p v-else-if="!visiblePatients?.length">No patients with observations.</p>
    <table v-else>
      <thead>
        <tr>
          <th>Name</th>
          <th>Gender</th>
          <th>Birth date</th>
          <th>Observations</th>
        </tr>
      </thead>
      <tbody>
        <!-- The click on the name button bubbles up to the row, so the row is clickable and the name is keyboard-reachable. -->
        <tr v-for="patient in visiblePatients" :key="patient.id" @click="emit('select', patient.id)">
          <td>
            <button aria-haspopup="dialog">
              {{ patient.family_name || '(no family name)' }}, {{ patient.given_names }}
            </button>
          </td>
          <td>{{ patient.gender || 'unknown' }}</td>
          <td>{{ patient.birth_date ?? '—' }}</td>
          <td>{{ patient.observation_count }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<style scoped>
  tbody tr {
    cursor: pointer;
  }
  button {
    padding: 0;
    border: none;
    background: none;
    font: inherit;
    text-align: left;
    cursor: pointer;
  }
  button:hover {
    color: var(--accent);
    text-decoration: underline;
  }
  small {
    opacity: 0.7;
  }
</style>

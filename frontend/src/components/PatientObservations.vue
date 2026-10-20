<script setup lang="ts">
import { useFetch } from '@/useFetch'

interface Observation {
  id: number
  fhir_id: string
  status: string
  code: string
  code_display: string
  effective_at: string | null
  value_quantity: { value?: number; unit?: string } | null
  interpretation_code: string
  ref_low: number | null
  ref_high: number | null
}

interface PatientDetail {
  id: number
  family_name: string
  given_names: string
  mrn: string | null
  observations: Observation[]
}

const props = defineProps<{ patientId: number }>()

const { data: patient, error, loading } = useFetch<PatientDetail>(
  () => `/api/patients/${props.patientId}/`,
)

function formatValue(obs: Observation) {
  if (obs.value_quantity) return `${obs.value_quantity.value ?? ''} ${obs.value_quantity.unit ?? ''}`
  return '—'
}

function formatRange(obs: Observation) {
  const { ref_low: low, ref_high: high } = obs
  if (low != null && high != null) return `${low}–${high}`
  if (high != null) return `≤ ${high}`
  if (low != null) return `≥ ${low}`
  return ''
}
</script>

<template>
  <section>
    <p v-if="loading">Loading…</p>
    <p v-else-if="error" role="alert">Could not load observations: {{ error }}</p>
    <template v-else-if="patient">
      <h2>{{ patient.family_name }}, {{ patient.given_names }}</h2>
      <p>MRN: {{ patient.mrn ?? 'not recorded' }}</p>
      <p v-if="!patient.observations.length">No observations.</p>
      <table v-else>
        <thead>
          <tr>
            <th>Date</th>
            <th>Observation</th>
            <th>Value</th>
            <th>Ref. range</th>
            <th>Flag</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="obs in patient.observations" :key="obs.id">
            <td>{{ obs.effective_at ? new Date(obs.effective_at).toLocaleDateString() : '—' }}</td>
            <td :title="obs.code">{{ obs.code_display || obs.code }}</td>
            <td>{{ formatValue(obs) }}</td>
            <td>{{ formatRange(obs) }}</td>
            <td>{{ obs.interpretation_code }}</td>
            <td>{{ obs.status }}</td>
          </tr>
        </tbody>
      </table>
    </template>
  </section>
</template>


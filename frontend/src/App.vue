<script setup lang="ts">
import { ref } from 'vue'
import PatientList from './components/PatientList.vue'
import PatientObservations from './components/PatientObservations.vue'

const selectedId = ref<number | null>(null)
</script>

<template>
  <h1>Migrated patients</h1>
  <p><small>Synthetic data from the public HAPI FHIR sandbox.</small></p>
  <main>
    <PatientList :selected-id="selectedId" @select="selectedId = $event" />
    <PatientObservations v-if="selectedId" :patient-id="selectedId" />
    <p v-else>Select a patient to see their observations.</p>
  </main>
</template>

<style scoped>
main {
  display: grid;
  grid-template-columns: minmax(16rem, 1fr) 3fr;
  gap: 2rem;
  align-items: start;
}
</style>

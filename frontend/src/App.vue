<script setup lang="ts">
import { ref, useTemplateRef, watch } from 'vue'
import PatientList from './components/PatientList.vue'
import PatientObservations from './components/PatientObservations.vue'

const selectedId = ref<number | null>(null)
const dialog = useTemplateRef<HTMLDialogElement>('dialog')

// The native <dialog> gives us Esc to close, the backdrop and focus handling.
watch(selectedId, (id) => {
  if (id !== null && !dialog.value?.open) dialog.value?.showModal()
})
</script>

<template>
  <h1>Migrated patients</h1>
  <p><small>Synthetic data from the public HAPI FHIR sandbox.</small></p>
  <PatientList @select="selectedId = $event" />

  <dialog ref="dialog" @close="selectedId = null" @click.self="dialog?.close()">
    <div class="modal">
      <button class="close" aria-label="Close" @click="dialog?.close()">×</button>
      <PatientObservations v-if="selectedId" :patient-id="selectedId" />
    </div>
  </dialog>
</template>

<style>
/* Global: the only unscoped styles. Components style themselves. */
:root {
  color-scheme: light dark;
  --accent: #2563eb;
  --border: color-mix(in srgb, currentColor 20%, transparent);
  font-family: system-ui, sans-serif;
  line-height: 1.5;
}
body {
  max-width: 72rem;
  margin: 0 auto;
  padding: 1.5rem;
}
h1,
h2 {
  margin-top: 0;
}
/* Both the patient list and the observations are plain tables. */
table {
  width: 100%;
  border-collapse: collapse;
}
th,
td {
  border-bottom: 1px solid var(--border);
  padding: 0.5rem 0.75rem;
  text-align: left;
}
th {
  font-size: 0.85rem;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  opacity: 0.7;
}
tbody tr:hover {
  background: color-mix(in srgb, var(--accent) 8%, transparent);
}
</style>

<style scoped>
dialog {
  width: min(60rem, 95vw);
  max-height: 85vh;
  padding: 0; /* the padding lives in .modal, so a click on the backdrop is a click on the dialog */
  border: 1px solid var(--border);
  border-radius: 8px;
}
dialog::backdrop {
  background: rgb(0 0 0 / 0.5);
}
.modal {
  position: relative;
  padding: 1.5rem;
}
.close {
  position: absolute;
  top: 0.5rem;
  right: 0.75rem;
  border: none;
  background: none;
  color: inherit;
  font-size: 1.5rem;
  cursor: pointer;
}
</style>

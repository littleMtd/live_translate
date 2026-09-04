<template>
  <section class="export-card">
    <h2>Export ChatGPT Bundle</h2>
    <p class="description">Export one sanitized runtime session with raw evidence and an LLM-friendly index.</p>

    <label for="bundle-run">Run</label>
    <select id="bundle-run" v-model="selectedRun" :disabled="busy || loading" data-testid="bundle-run">
      <option value="" disabled>{{ loading ? 'Loading runs…' : 'Select a run' }}</option>
      <option v-for="run in runs" :key="run.run_id" :value="run.run_id">
        {{ run.run_id }} · {{ run.event_count }} events{{ run.run_complete ? '' : ' · snapshot' }}
      </option>
    </select>

    <label class="audio-toggle">
      <input v-model="includeAudio" type="checkbox" :disabled="busy" data-testid="include-audio">
      Include retained WAV evidence
    </label>

    <button class="primary" :disabled="busy || !selectedRun" data-testid="export-bundle" @click="exportSelected">
      {{ busy ? 'Exporting…' : 'Export ChatGPT Bundle' }}
    </button>

    <div v-if="result" class="result" data-testid="export-result">
      <strong>Export complete</strong>
      <div>{{ result.output_path }}</div>
      <div>{{ result.file_count }} files · {{ formatBytes(result.total_bytes) }}</div>
    </div>
    <div v-if="error" class="error" data-testid="export-error">{{ error }}</div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { client } from '../api/client'
import type { BundleExportResult, ExportableRun } from '../types/config'

const runs = ref<ExportableRun[]>([])
const selectedRun = ref('')
const includeAudio = ref(false)
const loading = ref(false)
const busy = ref(false)
const result = ref<BundleExportResult | null>(null)
const error = ref('')

const loadRuns = async () => {
  loading.value = true
  error.value = ''
  try {
    runs.value = await client.listExportableRuns()
    if (runs.value.length) selectedRun.value = runs.value[0].run_id
  } catch (reason) {
    error.value = `Run discovery failed: ${reason}`
  } finally {
    loading.value = false
  }
}

const exportSelected = async () => {
  if (!selectedRun.value || busy.value) return
  busy.value = true
  error.value = ''
  result.value = null
  try {
    result.value = await client.exportChatgptBundle(selectedRun.value, includeAudio.value)
  } catch (reason) {
    error.value = `Export failed: ${reason}`
  } finally {
    busy.value = false
  }
}

const formatBytes = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

onMounted(loadRuns)
</script>

<style scoped>
.export-card { max-width: 720px; padding: 20px; border: 1px solid #e5e7eb; border-radius: 8px; background: white; }
h2 { margin: 0 0 6px; font-size: 18px; }
.description { margin: 0 0 20px; color: #6b7280; font-size: 13px; }
label { display: block; margin-bottom: 6px; font-size: 13px; font-weight: 600; }
select { width: 100%; padding: 9px; margin-bottom: 16px; border: 1px solid #d1d5db; border-radius: 6px; }
.audio-toggle { display: flex; gap: 8px; align-items: center; font-weight: 400; margin-bottom: 16px; }
.primary { padding: 9px 16px; border: 0; border-radius: 6px; background: #667eea; color: white; cursor: pointer; }
.primary:disabled { opacity: 0.55; cursor: not-allowed; }
.result, .error { margin-top: 16px; padding: 12px; border-radius: 6px; font-size: 13px; overflow-wrap: anywhere; }
.result { background: #ecfdf5; color: #065f46; }
.error { background: #fef2f2; color: #991b1b; }
</style>

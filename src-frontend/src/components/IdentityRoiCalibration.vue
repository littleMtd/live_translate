<template>
  <section class="calibration-panel">
    <h2>Channel identity ROI</h2>
    <p>The dashboard opens the same native calibration window used by <code>main.py</code>.</p>
    <button class="btn-primary" data-testid="start-calibration" :disabled="busy" @click="start">
      {{ busy ? 'Opening…' : 'Open native calibration' }}
    </button>
    <p v-if="message" data-testid="calibration-message">{{ message }}</p>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { client } from '../api/client'

const busy = ref(false)
const message = ref('')

async function start() {
  busy.value = true
  message.value = ''
  try {
    message.value = await client.launchIdentityRoiCalibration()
  } catch (reason) {
    message.value = String(reason)
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.calibration-panel { max-width: 620px; }
.btn-primary { background: #16b8c4; color: #071116; }
</style>

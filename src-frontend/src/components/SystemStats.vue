<template>
  <div class="system-stats">
    <h2>System Info</h2>

    <div v-if="stats" class="info-grid">
      <div class="info-row">
        <span class="label">Platform</span>
        <span class="value">{{ stats.platform }}</span>
      </div>
      <div class="info-row">
        <span class="label">Architecture</span>
        <span class="value">{{ stats.arch }}</span>
      </div>
      <div class="info-row">
        <span class="label">Unix timestamp</span>
        <span class="value">{{ stats.uptime_seconds.toLocaleString() }}s</span>
      </div>
      <div class="info-row">
        <span class="label">App version</span>
        <span class="value">0.1.0</span>
      </div>
    </div>

    <div v-else class="empty">Loading system info…</div>
  </div>
</template>

<script setup lang="ts">
import type { SystemStats } from '../types/config'
defineProps<{ stats: SystemStats | null }>()
</script>

<style scoped>
.system-stats { max-width: 600px; }

h2 { margin-bottom: 20px; font-size: 20px; }

.info-grid {
  background: white;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  overflow: hidden;
}

.info-row {
  display: flex;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid #f3f4f6;
}
.info-row:last-child { border-bottom: none; }

.label { font-size: 14px; color: #6b7280; }
.value { font-size: 14px; font-weight: 500; color: #111827; }

.empty { color: #9ca3af; text-align: center; margin: 40px 0; }
</style>

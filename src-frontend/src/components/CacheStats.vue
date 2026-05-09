<template>
  <div class="cache-stats">
    <h2>Translation Cache</h2>

    <div v-if="stats" class="stats-grid">
      <div class="stat-card">
        <div class="stat-value">{{ stats.total_entries.toLocaleString() }}</div>
        <div class="stat-label">Total Entries</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.hit_count_sum.toLocaleString() }}</div>
        <div class="stat-label">Total Cache Hits</div>
      </div>
      <div class="stat-card">
        <div class="stat-value">{{ stats.db_size_mb.toFixed(2) }} MB</div>
        <div class="stat-label">DB Size</div>
      </div>
      <div class="stat-card">
        <div class="stat-value last-used">{{ stats.last_used }}</div>
        <div class="stat-label">Last Used</div>
      </div>
    </div>

    <div v-else class="empty">No cache data available</div>

    <div class="actions">
      <button @click="$emit('refresh')" class="secondary">Refresh</button>
      <button @click="doClear" class="danger" :disabled="clearing">
        {{ clearing ? 'Clearing…' : 'Clear Cache' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { client } from '../api/client'
import type { CacheStats } from '../types/config'

defineProps<{ stats: CacheStats | null }>()
const emit = defineEmits<{ refresh: []; cleared: [] }>()

const clearing = ref(false)

const doClear = async () => {
  if (!confirm('清除所有快取記錄？此操作無法復原。')) return
  clearing.value = true
  try {
    const msg = await client.clearCache()
    alert(msg)
    emit('cleared')
  } catch (e) {
    alert(`清除失敗：${e}`)
  } finally {
    clearing.value = false
  }
}
</script>

<style scoped>
.cache-stats { max-width: 800px; }

h2 { margin-bottom: 20px; font-size: 20px; }

.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}

.stat-card {
  padding: 20px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border-radius: 10px;
  text-align: center;
}

.stat-value { font-size: 22px; font-weight: bold; }
.last-used  { font-size: 13px; word-break: break-all; }
.stat-label { font-size: 12px; margin-top: 6px; opacity: 0.85; }

.empty { color: #9ca3af; margin: 40px 0; text-align: center; }

.actions { display: flex; gap: 10px; }

button {
  padding: 9px 20px;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
}
button:disabled { opacity: 0.6; cursor: not-allowed; }

.secondary { background: #e5e7eb; color: #374151; }
.secondary:hover:not(:disabled) { background: #d1d5db; }
.danger { background: #ef4444; color: white; }
.danger:hover:not(:disabled) { background: #dc2626; }
</style>

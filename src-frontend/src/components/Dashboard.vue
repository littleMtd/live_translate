<template>
  <div class="dashboard">
    <header>
      <h1>Live Translate Dashboard</h1>
      <div class="header-right">
        <span class="status-dot" :class="{ online: pythonRunning }">
          {{ pythonRunning ? '● Online' : '● Offline' }}
        </span>
        <button v-if="!pythonRunning" @click="startPython" :disabled="busy" class="btn-start">Start</button>
        <button v-else @click="stopPython" :disabled="busy" class="btn-stop">Stop</button>
      </div>
    </header>

    <nav class="tabs">
      <button
        v-for="tab in tabs"
        :key="tab"
        :class="{ active: activeTab === tab }"
        @click="activeTab = tab"
      >
        {{ tab }}
      </button>
    </nav>

    <main>
      <template v-if="activeTab === 'Settings'">
        <div v-if="configLoadState === 'loading'" class="config-state" data-testid="config-loading">
          Loading settings…
        </div>
        <div v-else-if="configLoadState === 'error'" class="config-state config-error" data-testid="config-error">
          <p>Settings could not be loaded.</p>
          <p class="config-error-detail">{{ configLoadError }}</p>
          <button class="btn-retry" data-testid="config-retry" @click="loadConfig">Retry</button>
        </div>
        <ConfigPanel
          v-else-if="config"
          :config="config"
          @save="saveConfig"
        />
      </template>
      <CacheStats
        v-if="activeTab === 'Cache'"
        :stats="cacheStats"
        @refresh="refreshCacheStats"
        @cleared="refreshCacheStats"
      />
      <SystemStats v-if="activeTab === 'Stats'" :stats="systemStats" />
    </main>

    <div v-if="errorMsg" class="error-banner">{{ errorMsg }}</div>
    <div v-if="noticeMsg" class="notice-banner">{{ noticeMsg }}</div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { client } from '../api/client'
import ConfigPanel from './ConfigPanel.vue'
import CacheStats from './CacheStats.vue'
import SystemStats from './SystemStats.vue'
import type { ConfigDto, CacheStats as CacheStatsType, SystemStats as SystemStatsType } from '../types/config'

const activeTab = ref('Settings')
const tabs = ['Settings', 'Cache', 'Stats']
const config = ref<ConfigDto | null>(null)
const configLoadState = ref<'loading' | 'error' | 'loaded'>('loading')
const configLoadError = ref<string | null>(null)
const cacheStats = ref<CacheStatsType | null>(null)
const systemStats = ref<SystemStatsType | null>(null)
const pythonRunning = ref(false)
const busy = ref(false)
const errorMsg = ref<string | null>(null)
const noticeMsg = ref<string | null>(null)
let refreshInterval: ReturnType<typeof setInterval> | undefined
let errorTimer: ReturnType<typeof setTimeout> | undefined
let noticeTimer: ReturnType<typeof setTimeout> | undefined
let configLoadGeneration = 0

onMounted(async () => {
  // Cheap status + config + cache on mount; system stats are fetched lazily only
  // while the Stats tab is open (see activeTabData).
  await Promise.all([loadConfig(), refreshCacheStats(), pollStatus()])
  startPolling()
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onUnmounted(() => {
  stopPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
  if (errorTimer) clearTimeout(errorTimer)
  if (noticeTimer) clearTimeout(noticeTimer)
})

// Refresh the data the user is actually looking at the moment they switch tabs,
// instead of waiting up to one polling interval.
watch(activeTab, () => { activeTabData() })

const startPolling = () => {
  stopPolling()
  refreshInterval = setInterval(tick, 5000)
}

const stopPolling = () => {
  if (refreshInterval !== undefined) {
    clearInterval(refreshInterval)
    refreshInterval = undefined
  }
}

// Pause polling while the window is hidden/minimised so a backgrounded dashboard
// stops hitting the Tauri/IPC layer; resume with an immediate refresh.
const onVisibilityChange = () => {
  if (document.hidden) {
    stopPolling()
  } else {
    tick()
    startPolling()
  }
}

const tick = async () => {
  await pollStatus()
  await activeTabData()
}

// Only poll the data the active tab renders.
const activeTabData = async () => {
  if (activeTab.value === 'Cache') await refreshCacheStats()
  else if (activeTab.value === 'Stats') await refreshSystemStats()
}

const showError = (msg: string) => {
  errorMsg.value = msg
  if (errorTimer) clearTimeout(errorTimer)
  errorTimer = setTimeout(() => (errorMsg.value = null), 4000)
}

const showNotice = (msg: string) => {
  noticeMsg.value = msg
  if (noticeTimer) clearTimeout(noticeTimer)
  noticeTimer = setTimeout(() => (noticeMsg.value = null), 5000)
}

const cloneConfig = (value: ConfigDto): ConfigDto => JSON.parse(JSON.stringify(value))

const loadConfig = async () => {
  const generation = ++configLoadGeneration
  configLoadState.value = 'loading'
  configLoadError.value = null
  try {
    const loaded = await client.getConfig()
    if (generation !== configLoadGeneration) return
    config.value = cloneConfig(loaded)
    configLoadState.value = 'loaded'
  } catch (e) {
    if (generation !== configLoadGeneration) return
    configLoadError.value = `Config load failed: ${e}`
    configLoadState.value = 'error'
  }
}

const saveConfig = async (newConfig: ConfigDto) => {
  const snapshot = cloneConfig(newConfig)
  try {
    await client.updateConfig(snapshot)
    config.value = cloneConfig(snapshot)
    showNotice(
      pythonRunning.value
        ? 'Config saved. Restart Python to apply runtime changes.'
        : 'Config saved. It will apply when Python starts.',
    )
  } catch (e) {
    showError(`Save failed: ${e}`)
  }
}

const refreshCacheStats = async () => {
  try {
    cacheStats.value = await client.getCacheStats()
  } catch (e) {
    console.error('Cache stats error:', e)
  }
}

const refreshSystemStats = async () => {
  try {
    systemStats.value = await client.getSystemStats()
  } catch (e) {
    console.error('System stats error:', e)
  }
}

const pollStatus = async () => {
  try {
    pythonRunning.value = await client.pythonStatus()
  } catch {
    pythonRunning.value = false
  }
}

const startPython = async () => {
  if (busy.value) return
  busy.value = true
  try {
    await client.startPython()
    // Wait for Python to initialise, then reconcile real status before loading config.
    setTimeout(async () => {
      await pollStatus()
      await loadConfig()
      await activeTabData()
    }, 1500)
  } catch (e) {
    showError(`Start failed: ${e}`)
  } finally {
    busy.value = false
  }
}

const stopPython = async () => {
  if (busy.value) return
  busy.value = true
  try {
    await client.stopPython()
    pythonRunning.value = false
  } catch (e) {
    showError(`Stop failed: ${e}`)
  } finally {
    busy.value = false
  }
}
</script>

<style scoped>
.dashboard {
  display: flex;
  flex-direction: column;
  height: 100vh;
}

header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 24px;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
}

h1 { font-size: 18px; font-weight: 600; }

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.status-dot { font-size: 13px; opacity: 0.8; }
.status-dot.online { opacity: 1; color: #4ade80; }

.btn-start, .btn-stop {
  padding: 6px 14px;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 500;
}
.btn-start { background: #4ade80; color: #1a1a1a; }
.btn-stop  { background: #ef4444; color: white; }
.btn-start:disabled, .btn-stop:disabled { opacity: 0.6; cursor: not-allowed; }

.tabs {
  display: flex;
  border-bottom: 1px solid #e5e7eb;
  background: white;
}

.tabs button {
  flex: 1;
  padding: 12px;
  background: none;
  border: none;
  cursor: pointer;
  font-size: 14px;
  font-weight: 500;
  color: #6b7280;
  border-bottom: 3px solid transparent;
  transition: all 0.15s;
}

.tabs button.active {
  border-bottom-color: #667eea;
  color: #667eea;
}

main {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
}

.error-banner {
  position: fixed;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  background: #ef4444;
  color: white;
  padding: 10px 20px;
  border-radius: 8px;
  font-size: 13px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.2);
}

.notice-banner {
  position: fixed;
  bottom: 16px;
  left: 50%;
  transform: translateX(-50%);
  background: #2563eb;
  color: white;
  padding: 10px 20px;
  border-radius: 8px;
  font-size: 13px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.2);
}

.config-state {
  max-width: 620px;
  padding: 20px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: white;
  color: #4b5563;
}

.config-error {
  border-color: #fecaca;
  color: #991b1b;
}

.config-error-detail {
  margin: 8px 0 16px;
  font-size: 13px;
}

.btn-retry {
  padding: 8px 16px;
  border: none;
  border-radius: 6px;
  background: #667eea;
  color: white;
  cursor: pointer;
}
</style>

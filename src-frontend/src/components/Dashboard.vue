<template>
  <div class="dashboard">
    <header>
      <h1>Live Translate Dashboard</h1>
      <div class="header-right">
        <span class="status-dot" :class="{ online: pythonRunning }">
          {{ pythonRunning ? '● Online' : '● Offline' }}
        </span>
        <button v-if="!pythonRunning" @click="startPython" class="btn-start">Start</button>
        <button v-else @click="stopPython" class="btn-stop">Stop</button>
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
      <ConfigPanel
        v-if="activeTab === 'Settings'"
        :config="config"
        @save="saveConfig"
      />
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
import { ref, onMounted, onUnmounted } from 'vue'
import { client } from '../api/client'
import ConfigPanel from './ConfigPanel.vue'
import CacheStats from './CacheStats.vue'
import SystemStats from './SystemStats.vue'
import type { ConfigDto, CacheStats as CacheStatsType, SystemStats as SystemStatsType } from '../types/config'

const activeTab = ref('Settings')
const tabs = ['Settings', 'Cache', 'Stats']
const config = ref<ConfigDto | null>(null)
const cacheStats = ref<CacheStatsType | null>(null)
const systemStats = ref<SystemStatsType | null>(null)
const pythonRunning = ref(false)
const errorMsg = ref<string | null>(null)
const noticeMsg = ref<string | null>(null)
let refreshInterval: ReturnType<typeof setInterval>

onMounted(async () => {
  await Promise.all([loadConfig(), refreshCacheStats(), checkPythonStatus()])
  refreshInterval = setInterval(async () => {
    await refreshCacheStats()
    await checkPythonStatus()
  }, 5000)
})

onUnmounted(() => clearInterval(refreshInterval))

const showError = (msg: string) => {
  errorMsg.value = msg
  setTimeout(() => (errorMsg.value = null), 4000)
}

const showNotice = (msg: string) => {
  noticeMsg.value = msg
  setTimeout(() => (noticeMsg.value = null), 5000)
}

const loadConfig = async () => {
  try {
    config.value = await client.getConfig()
  } catch (e) {
    showError(`Config load failed: ${e}`)
  }
}

const saveConfig = async (newConfig: ConfigDto) => {
  try {
    await client.updateConfig(newConfig)
    config.value = newConfig
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

const checkPythonStatus = async () => {
  try {
    pythonRunning.value = await client.pythonStatus()
    systemStats.value = await client.getSystemStats()
  } catch {
    pythonRunning.value = false
  }
}

const startPython = async () => {
  try {
    await client.startPython()
    // Wait for Python to initialise, then reconcile real status before loading config.
    setTimeout(async () => {
      await checkPythonStatus()
      await loadConfig()
    }, 1500)
  } catch (e) {
    showError(`Start failed: ${e}`)
  }
}

const stopPython = async () => {
  try {
    await client.stopPython()
    pythonRunning.value = false
  } catch (e) {
    showError(`Stop failed: ${e}`)
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
</style>

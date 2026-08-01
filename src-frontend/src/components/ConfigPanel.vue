<template>
  <div class="config-panel">
    <h2>Settings</h2>
    <p class="restart-note">
      Settings are written to disk. Restart the Python pipeline to apply runtime changes.
    </p>

    <div class="section">
      <h3>Subtitle Display</h3>
      <label>
        Font Size: <strong>{{ local.subtitle.font_size }}px</strong>
        <input v-model.number="local.subtitle.font_size" type="range" min="8" max="48" />
      </label>
      <label>
        Opacity: <strong>{{ Math.round(local.subtitle.alpha * 100) }}%</strong>
        <input v-model.number="local.subtitle.alpha" type="range" min="0.1" max="1" step="0.05" />
      </label>
      <label>
        Auto-hide after:
        <input v-model.number="local.subtitle.idle_hide_ms" type="number" min="1000" max="120000" step="1000" />
        ms
      </label>
      <label>
        Font family:
        <input v-model="local.subtitle.font_family" type="text" />
      </label>
    </div>

    <div class="section">
      <h3>Translation Engine</h3>
      <label>
        Live backend:
        <select v-model="local.live_engine">
          <option value="anthropic">Engine chain</option>
          <option value="nvidia">NVIDIA NIM</option>
          <option value="ollama">Ollama</option>
        </select>
      </label>
      <label>
        Fallback chain:
        <input v-model="engineChainText" type="text" />
      </label>
      <label>
        Mode:
        <select v-model="local.translation.translation_mode">
          <option value="live">Live</option>
          <option value="clip">Clip</option>
        </select>
      </label>
      <label>
        Max tokens:
        <input v-model.number="local.translation.max_tokens" type="number" min="10" max="500" />
      </label>
      <label>
        Target language:
        <input v-model="local.translation.target_lang" type="text" />
      </label>
      <label>
        Current activity:
        <input
          v-model="local.translation.current_activity"
          type="text"
          maxlength="80"
          placeholder="e.g. StarCraft, tier list talk"
        />
      </label>
      <p class="field-note">
        Optional one-line context metadata. It is never translated as subtitle text.
      </p>
    </div>

    <div class="section">
      <h3>Automatic Scene Context</h3>
      <label>
        Publish model-derived activity:
        <input
          v-model="local.scene.publish_open_set_activity"
          data-testid="publish-open-set-activity"
          type="checkbox"
        />
      </label>
      <p class="field-note">
        Allows a confirmed model-derived activity to inform translation context.
        Scene capture and STT terms are unchanged. Restart the Python pipeline after saving.
      </p>
    </div>

    <div class="section">
      <h3>STT Engine</h3>
      <label>
        Primary engine:
        <select v-model="local.stt.primary_engine">
          <option value="sensevoice">SenseVoice (local)</option>
          <option value="groq">Groq Whisper (cloud)</option>
        </select>
      </label>
    </div>

    <div class="section">
      <h3>Audio / VAD</h3>
      <label>
        VAD enabled:
        <input v-model="local.audio.vad_enabled" data-testid="vad-enabled" type="checkbox" />
      </label>
      <label>
        Silence threshold (s):
        <input v-model.number="local.audio.vad_silence_sec" type="number" min="0.1" max="5" step="0.1" />
      </label>
      <label>
        Max speech chunk (s):
        <input v-model.number="local.audio.vad_max_speech_sec" type="number" min="1" max="30" step="0.5" />
      </label>
    </div>

    <div class="actions">
      <button @click="save" class="primary">Save</button>
      <button @click="reset">Cancel</button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ConfigDto } from '../types/config'

const props = defineProps<{ config: ConfigDto }>()
const emit = defineEmits<{ save: [ConfigDto] }>()

const clone = (config: ConfigDto): ConfigDto => JSON.parse(JSON.stringify(config))

const local = ref<ConfigDto>(clone(props.config))

const engineChainText = computed({
  get: () => local.value.translation.engine_chain.join(', '),
  set: (value: string) => {
    local.value.translation.engine_chain = value
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean) as ConfigDto['translation']['engine_chain']
  },
})

watch(() => props.config, (config) => { local.value = clone(config) }, { deep: true })

const save = () => emit('save', clone(local.value))
const reset = () => { local.value = clone(props.config) }
</script>

<style scoped>
.config-panel { max-width: 620px; }

h2 { margin-bottom: 8px; font-size: 20px; }

.restart-note {
  margin: 0 0 20px;
  padding: 10px 12px;
  background: #fff7ed;
  border: 1px solid #fed7aa;
  border-radius: 8px;
  color: #9a3412;
  font-size: 13px;
}

.field-note {
  margin: -4px 0 8px;
  color: #6b7280;
  font-size: 12px;
}

.section {
  margin-bottom: 20px;
  padding: 16px;
  background: white;
  border-radius: 8px;
  box-shadow: 0 1px 3px rgba(0,0,0,0.08);
}

h3 { margin-bottom: 12px; font-size: 14px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; }

label {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 10px 0;
  font-size: 14px;
  color: #374151;
}

label strong { min-width: 48px; text-align: right; color: #111827; }

input[type="range"] { flex: 1; }
input[type="number"], input[type="text"], select {
  padding: 5px 8px;
  border: 1px solid #d1d5db;
  border-radius: 6px;
  font-size: 13px;
}
input[type="number"] { width: 90px; }
input[type="text"]   { width: 180px; }

.actions { display: flex; gap: 10px; margin-top: 8px; }

button {
  padding: 9px 20px;
  font-size: 14px;
  border: none;
  border-radius: 6px;
  cursor: pointer;
  font-weight: 500;
}

.primary { background: #667eea; color: white; }
.primary:hover { background: #5568d3; }
button:not(.primary) { background: #e5e7eb; color: #374151; }
button:not(.primary):hover { background: #d1d5db; }
</style>

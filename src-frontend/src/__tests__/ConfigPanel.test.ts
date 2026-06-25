import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ConfigPanel from '../components/ConfigPanel.vue'
import type { ConfigDto } from '../types/config'

function makeConfig(overrides: Partial<ConfigDto> = {}): ConfigDto {
  return {
    audio: { sample_rate: 16000, channels: 1, chunk_seconds: 3, device_name: 'CABLE Output', volume_threshold: 0.01,
             vad_enabled: true, vad_silence_sec: 0.6, vad_min_speech_sec: 0.4,
             vad_max_speech_sec: 8.0, vad_silero_threshold: 0.5, queue_maxsize: 10 },
    stt: { primary_engine: 'groq', sensevoice_model: 'iic/SenseVoiceSmall', sensevoice_device: 'cuda',
           groq_model: 'whisper-large-v3', language: 'ko', groq_prompt: '', batch_size_s: 60,
           queue_maxsize: 20, no_speech_threshold: 0.6, avg_logprob_threshold: -1.0,
           max_japanese_chars: 2, max_repeat_ratio: 0.7 },
    splitter: { min_wait_seconds: 3, force_cut_seconds: 8 },
    translation: { engine_chain: ['openrouter', 'groq'], model: 'claude-sonnet-4-6',
                   google_translate_lang: 'zh-TW',
                   target_lang: 'zh-TW', max_tokens: 80, temperature: 0.0, queue_maxsize: 2,
                   context_window: 10, translation_mode: 'live', streamer_profile: 'hades_chxxnnx',
                   use_profile: true, slang: {} },
    subtitle: { idle_hide_ms: 30000, font_family: 'Microsoft JhengHei', font_size: 22, font_style: 'bold',
                bg: '#010101', ctrl_bg: '#1a1a1a', fg: '#FFFFFF', outline_color: '#000000',
                outline_width: 2, alpha: 0.82, max_width_chars: 36, wraplength: 700,
                padx: 16, pady: 8, init_offset_x: 400, init_offset_y: 160,
                poll_interval_ms: 100, min_display_ms: 1500, ms_per_char: 80, queue_maxsize: 10 },
    database: { db_path: 'logs/live_translate.db', db_cache_max_rows: 50000 },
    live_engine: 'nvidia',
    clip_engine: 'nvidia',
    ollama: { base_url: 'http://localhost:11434', model: 'qwen2.5:3b', timeout: 60 },
    nvidia: { model: 'qwen/qwen3.5-122b-a10b', timeout: 60 },
    ...overrides,
  }
}

describe('ConfigPanel', () => {
  it('renders without crash when config is null', () => {
    const wrapper = mount(ConfigPanel, { props: { config: null } })
    expect(wrapper.find('h2').text()).toBe('Settings')
  })

  it('renders with provided config and shows font size', () => {
    const wrapper = mount(ConfigPanel, { props: { config: makeConfig() } })
    expect(wrapper.html()).toContain('22')
  })

  it('shows restart-required note for runtime settings', () => {
    const wrapper = mount(ConfigPanel, { props: { config: makeConfig() } })
    expect(wrapper.text()).toContain('Restart the Python pipeline')
  })

  it('shows translation engine settings', () => {
    const wrapper = mount(ConfigPanel, { props: { config: makeConfig() } })
    const vm = wrapper.vm as any
    expect(wrapper.html()).toContain('nvidia')
    expect(vm.engineChainText).toBe('openrouter, groq')
  })

  it('emits save event with current config when Save clicked', async () => {
    const cfg = makeConfig()
    const wrapper = mount(ConfigPanel, { props: { config: cfg } })
    await wrapper.find('button.primary').trigger('click')
    const emitted = wrapper.emitted('save')
    expect(emitted).toBeTruthy()
    expect((emitted![0][0] as ConfigDto).subtitle.font_size).toBe(22)
  })

  it('emitted save config is a deep clone not the original', async () => {
    const cfg = makeConfig()
    const wrapper = mount(ConfigPanel, { props: { config: cfg } })
    await wrapper.find('button.primary').trigger('click')
    const saved = wrapper.emitted('save')![0][0] as ConfigDto
    expect(saved).not.toBe(cfg)
    expect(saved.subtitle).not.toBe(cfg.subtitle)
  })

  it('Cancel button resets local state to original config', async () => {
    const cfg = makeConfig()
    const wrapper = mount(ConfigPanel, { props: { config: cfg } })

    // Simulate changing max_tokens via the component's reactive data
    const vm = wrapper.vm as any
    vm.local.translation.max_tokens = 999

    await wrapper.find('button:not(.primary)').trigger('click')
    expect(vm.local.translation.max_tokens).toBe(80)
  })

  it('shows default values when config is null', () => {
    const wrapper = mount(ConfigPanel, { props: { config: null } })
    const vm = wrapper.vm as any
    expect(vm.local.subtitle.font_size).toBe(22)
    expect(vm.local.live_engine).toBe('nvidia')
  })

  it('updates local state when config prop changes', async () => {
    const cfg = makeConfig()
    const wrapper = mount(ConfigPanel, { props: { config: cfg } })
    await wrapper.setProps({ config: makeConfig({ subtitle: { ...cfg.subtitle, font_size: 36 } }) })
    const vm = wrapper.vm as any
    expect(vm.local.subtitle.font_size).toBe(36)
  })

  it('vad_enabled checkbox reflects config', () => {
    const wrapper = mount(ConfigPanel, { props: { config: makeConfig() } })
    const checkbox = wrapper.find('input[type="checkbox"]')
    expect((checkbox.element as HTMLInputElement).checked).toBe(true)
  })
})

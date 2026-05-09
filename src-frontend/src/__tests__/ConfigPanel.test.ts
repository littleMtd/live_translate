import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ConfigPanel from '../components/ConfigPanel.vue'
import type { ConfigDto } from '../types/config'

function makeConfig(overrides: Partial<ConfigDto> = {}): ConfigDto {
  return {
    audio: { sample_rate: 16000, channels: 1, volume_threshold: 0.01,
             vad_enabled: true, vad_silence_sec: 0.6, vad_min_speech_sec: 0.4,
             vad_max_speech_sec: 8.0, queue_maxsize: 10 },
    stt: { primary_engine: 'groq', language: 'ko', queue_maxsize: 20 },
    splitter: { min_wait_seconds: 3, force_cut_seconds: 8 },
    translation: { primary_engine: 'gemini', target_lang: 'zh-TW',
                   max_tokens: 80, temperature: 0.0, queue_maxsize: 2, slang: {} },
    subtitle: { font_family: 'Microsoft JhengHei', font_size: 22, font_style: 'bold',
                idle_hide_ms: 30000, alpha: 0.82, queue_maxsize: 10 },
    database: { db_path: 'logs/live_translate.db', db_cache_max_rows: 50000 },
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

  it('shows translation engine select with gemini selected', () => {
    const wrapper = mount(ConfigPanel, { props: { config: makeConfig() } })
    const select = wrapper.find('select[id], select').element as HTMLSelectElement
    // The first select is the translation engine
    expect(wrapper.html()).toContain('gemini')
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
    expect(vm.local.translation.primary_engine).toBe('gemini')
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

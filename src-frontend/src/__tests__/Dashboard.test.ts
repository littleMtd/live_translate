import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { invoke } from '@tauri-apps/api/core'
import Dashboard from '../components/Dashboard.vue'

const mockInvoke = vi.mocked(invoke)

const fakeConfig = {
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
}

const fakeStats = { total_entries: 0, hit_count_sum: 0, last_used: 'Never', db_size_mb: 0 }
const fakeSysStats = { uptime_seconds: 1000, platform: 'windows', arch: 'x86_64' }

function setupDefaultMocks() {
  mockInvoke.mockImplementation((cmd: string) => {
    if (cmd === 'get_config') return Promise.resolve(fakeConfig)
    if (cmd === 'get_cache_stats') return Promise.resolve(fakeStats)
    if (cmd === 'python_status') return Promise.resolve(false)
    if (cmd === 'get_system_stats') return Promise.resolve(fakeSysStats)
    return Promise.resolve(null)
  })
}

describe('Dashboard', () => {
  beforeEach(() => {
    mockInvoke.mockReset()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders three tab buttons', async () => {
    setupDefaultMocks()
    const wrapper = mount(Dashboard)
    await flushPromises()
    const tabs = wrapper.findAll('.tabs button')
    expect(tabs).toHaveLength(3)
    expect(tabs[0].text()).toBe('Settings')
    expect(tabs[1].text()).toBe('Cache')
    expect(tabs[2].text()).toBe('Stats')
  })

  it('shows Settings tab content by default', async () => {
    setupDefaultMocks()
    const wrapper = mount(Dashboard)
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ConfigPanel' }).exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'CacheStats' }).exists()).toBe(false)
  })

  it('switches to Cache tab when clicked', async () => {
    setupDefaultMocks()
    const wrapper = mount(Dashboard)
    await flushPromises()
    await wrapper.findAll('.tabs button')[1].trigger('click')
    expect(wrapper.findComponent({ name: 'CacheStats' }).exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'ConfigPanel' }).exists()).toBe(false)
  })

  it('switches to Stats tab when clicked', async () => {
    setupDefaultMocks()
    const wrapper = mount(Dashboard)
    await flushPromises()
    await wrapper.findAll('.tabs button')[2].trigger('click')
    expect(wrapper.findComponent({ name: 'SystemStats' }).exists()).toBe(true)
  })

  it('calls get_config on mount', async () => {
    setupDefaultMocks()
    mount(Dashboard)
    await flushPromises()
    expect(mockInvoke).toHaveBeenCalledWith('get_config')
  })

  it('calls get_cache_stats on mount', async () => {
    setupDefaultMocks()
    mount(Dashboard)
    await flushPromises()
    expect(mockInvoke).toHaveBeenCalledWith('get_cache_stats')
  })

  it('shows Offline status when python_status returns false', async () => {
    setupDefaultMocks()
    const wrapper = mount(Dashboard)
    await flushPromises()
    expect(wrapper.html()).toContain('Offline')
    expect(wrapper.html()).not.toContain('Online')
  })

  it('shows Online status when python_status returns true', async () => {
    mockInvoke.mockImplementation((cmd: string) => {
      if (cmd === 'get_config') return Promise.resolve(fakeConfig)
      if (cmd === 'get_cache_stats') return Promise.resolve(fakeStats)
      if (cmd === 'python_status') return Promise.resolve(true)
      if (cmd === 'get_system_stats') return Promise.resolve(fakeSysStats)
      return Promise.resolve(null)
    })
    const wrapper = mount(Dashboard)
    await flushPromises()
    expect(wrapper.html()).toContain('Online')
  })

  it('shows Start button when python is offline', async () => {
    setupDefaultMocks()
    const wrapper = mount(Dashboard)
    await flushPromises()
    expect(wrapper.find('button.btn-start').exists()).toBe(true)
    expect(wrapper.find('button.btn-stop').exists()).toBe(false)
  })

  it('shows Stop button when python is online', async () => {
    mockInvoke.mockImplementation((cmd: string) => {
      if (cmd === 'get_config') return Promise.resolve(fakeConfig)
      if (cmd === 'get_cache_stats') return Promise.resolve(fakeStats)
      if (cmd === 'python_status') return Promise.resolve(true)
      if (cmd === 'get_system_stats') return Promise.resolve(fakeSysStats)
      return Promise.resolve(null)
    })
    const wrapper = mount(Dashboard)
    await flushPromises()
    expect(wrapper.find('button.btn-stop').exists()).toBe(true)
    expect(wrapper.find('button.btn-start').exists()).toBe(false)
  })

  it('shows error banner when get_config fails', async () => {
    mockInvoke.mockImplementation((cmd: string) => {
      if (cmd === 'get_config') return Promise.reject(new Error('not found'))
      if (cmd === 'get_cache_stats') return Promise.resolve(fakeStats)
      if (cmd === 'python_status') return Promise.resolve(false)
      if (cmd === 'get_system_stats') return Promise.resolve(fakeSysStats)
      return Promise.resolve(null)
    })
    const wrapper = mount(Dashboard)
    await flushPromises()
    expect(wrapper.find('.error-banner').exists()).toBe(true)
    expect(wrapper.find('.error-banner').text()).toContain('not found')
  })

  it('active tab button has active class', async () => {
    setupDefaultMocks()
    const wrapper = mount(Dashboard)
    await flushPromises()
    expect(wrapper.findAll('.tabs button')[0].classes()).toContain('active')
    expect(wrapper.findAll('.tabs button')[1].classes()).not.toContain('active')
  })
})

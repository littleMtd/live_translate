import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { invoke } from '@tauri-apps/api/core'
import Dashboard from '../components/Dashboard.vue'

const mockInvoke = vi.mocked(invoke)

const fakeConfig = {
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
                 use_profile: true, current_activity: '', slang: {} },
  scene: { publish_open_set_activity: false },
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
}

const fakeStats = { total_entries: 0, hit_count_sum: 0, last_used: 'Never', db_size_mb: 0 }
const fakeSysStats = { unix_timestamp_seconds: 1000, platform: 'windows', arch: 'x86_64' }

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

const cloneConfig = (overrides: Record<string, unknown> = {}) => ({
  ...JSON.parse(JSON.stringify(fakeConfig)),
  ...overrides,
})

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

  it('renders four tab buttons', async () => {
    setupDefaultMocks()
    const wrapper = mount(Dashboard)
    await flushPromises()
    const tabs = wrapper.findAll('.tabs button')
    expect(tabs).toHaveLength(4)
    expect(tabs[0].text()).toBe('Settings')
    expect(tabs[1].text()).toBe('Cache')
    expect(tabs[2].text()).toBe('Stats')
    expect(tabs[3].text()).toBe('Export')
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

  it('switches to Export tab when clicked', async () => {
    setupDefaultMocks()
    const wrapper = mount(Dashboard)
    await flushPromises()
    await wrapper.findAll('.tabs button')[3].trigger('click')
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ExportBundle' }).exists()).toBe(true)
    expect(mockInvoke).toHaveBeenCalledWith('list_exportable_runs')
  })

  it('does not mount an editable settings form while config is loading', async () => {
    const pending = deferred<typeof fakeConfig>()
    mockInvoke.mockImplementation((cmd: string) => {
      if (cmd === 'get_config') return pending.promise
      if (cmd === 'get_cache_stats') return Promise.resolve(fakeStats)
      if (cmd === 'python_status') return Promise.resolve(false)
      return Promise.resolve(null)
    })

    const wrapper = mount(Dashboard)
    await flushPromises()

    expect(wrapper.find('[data-testid="config-loading"]').exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'ConfigPanel' }).exists()).toBe(false)
    expect(mockInvoke).not.toHaveBeenCalledWith('update_config', expect.anything())
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

  it('keeps config load failure fail-closed and retries authoritative values', async () => {
    const authoritative = cloneConfig({
      audio: { ...fakeConfig.audio, vad_silence_sec: 0.9, vad_max_speech_sec: 6.5 },
      translation: {
        ...fakeConfig.translation,
        engine_chain: ['openrouter', 'deepl', 'groq'],
        max_tokens: 200,
      },
    })
    let configCalls = 0
    mockInvoke.mockImplementation((cmd: string) => {
      if (cmd === 'get_config') {
        configCalls += 1
        return configCalls === 1
          ? Promise.reject(new Error('not found'))
          : Promise.resolve(authoritative)
      }
      if (cmd === 'get_cache_stats') return Promise.resolve(fakeStats)
      if (cmd === 'python_status') return Promise.resolve(false)
      if (cmd === 'get_system_stats') return Promise.resolve(fakeSysStats)
      return Promise.resolve(null)
    })
    const wrapper = mount(Dashboard)
    await flushPromises()

    expect(wrapper.find('[data-testid="config-error"]').text()).toContain('not found')
    expect(wrapper.findComponent({ name: 'ConfigPanel' }).exists()).toBe(false)
    await vi.advanceTimersByTimeAsync(5000)
    expect(wrapper.find('[data-testid="config-error"]').exists()).toBe(true)
    expect(mockInvoke).not.toHaveBeenCalledWith('update_config', expect.anything())

    await wrapper.find('[data-testid="config-retry"]').trigger('click')
    await flushPromises()

    const panel = wrapper.findComponent({ name: 'ConfigPanel' })
    expect(panel.exists()).toBe(true)
    expect((panel.props('config') as typeof fakeConfig).translation.engine_chain).toEqual([
      'openrouter', 'deepl', 'groq',
    ])
    expect((panel.props('config') as typeof fakeConfig).translation.max_tokens).toBe(200)
    expect((panel.props('config') as typeof fakeConfig).audio.vad_silence_sec).toBe(0.9)
  })

  it('ignores an old failure after a newer config load succeeds', async () => {
    const oldLoad = deferred<typeof fakeConfig>()
    const newest = cloneConfig({ subtitle: { ...fakeConfig.subtitle, font_size: 31 } })
    let configCalls = 0
    mockInvoke.mockImplementation((cmd: string) => {
      if (cmd === 'get_config') {
        configCalls += 1
        return configCalls === 1 ? oldLoad.promise : Promise.resolve(newest)
      }
      if (cmd === 'get_cache_stats') return Promise.resolve(fakeStats)
      if (cmd === 'python_status') return Promise.resolve(false)
      return Promise.resolve(null)
    })
    const wrapper = mount(Dashboard)
    await flushPromises()

    await (wrapper.vm as any).loadConfig()
    await flushPromises()
    oldLoad.reject(new Error('stale failure'))
    await flushPromises()

    const panel = wrapper.findComponent({ name: 'ConfigPanel' })
    expect(panel.exists()).toBe(true)
    expect((panel.props('config') as typeof fakeConfig).subtitle.font_size).toBe(31)
    expect(wrapper.find('[data-testid="config-error"]').exists()).toBe(false)
  })

  it('ignores an old success after a newer config load fails', async () => {
    const oldLoad = deferred<typeof fakeConfig>()
    let configCalls = 0
    mockInvoke.mockImplementation((cmd: string) => {
      if (cmd === 'get_config') {
        configCalls += 1
        return configCalls === 1
          ? oldLoad.promise
          : Promise.reject(new Error('latest failure'))
      }
      if (cmd === 'get_cache_stats') return Promise.resolve(fakeStats)
      if (cmd === 'python_status') return Promise.resolve(false)
      return Promise.resolve(null)
    })
    const wrapper = mount(Dashboard)
    await flushPromises()

    await (wrapper.vm as any).loadConfig()
    await flushPromises()
    oldLoad.resolve(cloneConfig({ subtitle: { ...fakeConfig.subtitle, font_size: 44 } }))
    await flushPromises()

    expect(wrapper.find('[data-testid="config-error"]').text()).toContain('latest failure')
    expect(wrapper.findComponent({ name: 'ConfigPanel' }).exists()).toBe(false)
  })

  it('keeps the newer success when two config loads complete out of order', async () => {
    const oldLoad = deferred<typeof fakeConfig>()
    const newLoad = deferred<typeof fakeConfig>()
    let configCalls = 0
    mockInvoke.mockImplementation((cmd: string) => {
      if (cmd === 'get_config') {
        configCalls += 1
        return configCalls === 1 ? oldLoad.promise : newLoad.promise
      }
      if (cmd === 'get_cache_stats') return Promise.resolve(fakeStats)
      if (cmd === 'python_status') return Promise.resolve(false)
      return Promise.resolve(null)
    })
    const wrapper = mount(Dashboard)
    await flushPromises()

    const newestPromise = (wrapper.vm as any).loadConfig()
    newLoad.resolve(cloneConfig({ subtitle: { ...fakeConfig.subtitle, font_size: 35 } }))
    await newestPromise
    await flushPromises()
    oldLoad.resolve(cloneConfig({ subtitle: { ...fakeConfig.subtitle, font_size: 19 } }))
    await flushPromises()

    const panel = wrapper.findComponent({ name: 'ConfigPanel' })
    expect((panel.props('config') as typeof fakeConfig).subtitle.font_size).toBe(35)
  })

  it('reports profile hot reload after saving config while Python is online', async () => {
    mockInvoke.mockImplementation((cmd: string) => {
      if (cmd === 'get_config') return Promise.resolve(fakeConfig)
      if (cmd === 'get_cache_stats') return Promise.resolve(fakeStats)
      if (cmd === 'python_status') return Promise.resolve(true)
      if (cmd === 'get_system_stats') return Promise.resolve(fakeSysStats)
      if (cmd === 'update_config') return Promise.resolve(undefined)
      return Promise.resolve(null)
    })
    const wrapper = mount(Dashboard)
    await flushPromises()

    wrapper.findComponent({ name: 'ConfigPanel' }).vm.$emit('save', fakeConfig)
    await flushPromises()

    expect(mockInvoke).toHaveBeenCalledWith('update_config', { newConfig: fakeConfig })
    expect(wrapper.find('.notice-banner').text()).toContain('Profile selection will hot-reload')
  })

  it('saves a stable snapshot even if the emitted object is mutated in flight', async () => {
    const update = deferred<void>()
    mockInvoke.mockImplementation((cmd: string) => {
      if (cmd === 'get_config') return Promise.resolve(fakeConfig)
      if (cmd === 'get_cache_stats') return Promise.resolve(fakeStats)
      if (cmd === 'python_status') return Promise.resolve(false)
      if (cmd === 'update_config') return update.promise
      return Promise.resolve(null)
    })
    const wrapper = mount(Dashboard)
    await flushPromises()
    const emitted = cloneConfig({ subtitle: { ...fakeConfig.subtitle, font_size: 28 } })

    wrapper.findComponent({ name: 'ConfigPanel' }).vm.$emit('save', emitted)
    emitted.subtitle.font_size = 47
    update.resolve()
    await flushPromises()

    const updateCall = mockInvoke.mock.calls.find(([cmd]) => cmd === 'update_config')
    expect((updateCall![1] as any).newConfig.subtitle.font_size).toBe(28)
    const panel = wrapper.findComponent({ name: 'ConfigPanel' })
    expect((panel.props('config') as typeof fakeConfig).subtitle.font_size).toBe(28)
  })

  it('active tab button has active class', async () => {
    setupDefaultMocks()
    const wrapper = mount(Dashboard)
    await flushPromises()
    expect(wrapper.findAll('.tabs button')[0].classes()).toContain('active')
    expect(wrapper.findAll('.tabs button')[1].classes()).not.toContain('active')
  })

  it('fetches system stats lazily — not on mount, only when the Stats tab opens', async () => {
    setupDefaultMocks()
    const wrapper = mount(Dashboard)
    await flushPromises()
    // Settings is the default tab; system stats must not be polled yet.
    expect(mockInvoke).not.toHaveBeenCalledWith('get_system_stats')

    await wrapper.findAll('.tabs button')[2].trigger('click')
    await flushPromises()
    expect(mockInvoke).toHaveBeenCalledWith('get_system_stats')
  })

  it('refreshes cache stats immediately when the Cache tab is opened', async () => {
    setupDefaultMocks()
    const wrapper = mount(Dashboard)
    await flushPromises()
    const onMount = mockInvoke.mock.calls.filter(([cmd]) => cmd === 'get_cache_stats').length

    await wrapper.findAll('.tabs button')[1].trigger('click')
    await flushPromises()
    const afterSwitch = mockInvoke.mock.calls.filter(([cmd]) => cmd === 'get_cache_stats').length
    expect(afterSwitch).toBeGreaterThan(onMount)
  })

  it('disables the Start button while a start request is in flight', async () => {
    let resolveStart: () => void = () => {}
    mockInvoke.mockImplementation((cmd: string) => {
      if (cmd === 'get_config') return Promise.resolve(fakeConfig)
      if (cmd === 'get_cache_stats') return Promise.resolve(fakeStats)
      if (cmd === 'python_status') return Promise.resolve(false)
      if (cmd === 'get_system_stats') return Promise.resolve(fakeSysStats)
      if (cmd === 'start_python') return new Promise<void>((r) => { resolveStart = r })
      return Promise.resolve(null)
    })
    const wrapper = mount(Dashboard)
    await flushPromises()

    await wrapper.find('button.btn-start').trigger('click')
    await flushPromises()
    expect(wrapper.find('button.btn-start').attributes('disabled')).toBeDefined()

    resolveStart()
    await flushPromises()
    expect(wrapper.find('button.btn-start').attributes('disabled')).toBeUndefined()
  })
})

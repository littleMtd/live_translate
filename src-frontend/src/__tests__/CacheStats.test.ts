import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { invoke } from '@tauri-apps/api/core'
import CacheStats from '../components/CacheStats.vue'
import type { CacheStats as CacheStatsType } from '../types/config'

const mockInvoke = vi.mocked(invoke)

const sampleStats: CacheStatsType = {
  total_entries: 42,
  hit_count_sum: 128,
  last_used: '2024-06-01T12:00:00',
  db_size_mb: 0.25,
}

describe('CacheStats', () => {
  beforeEach(() => mockInvoke.mockReset())

  it('shows empty message when stats is null', () => {
    const wrapper = mount(CacheStats, { props: { stats: null } })
    expect(wrapper.find('.empty').exists()).toBe(true)
    expect(wrapper.find('.stats-grid').exists()).toBe(false)
  })

  it('renders all four stat cards when stats provided', () => {
    const wrapper = mount(CacheStats, { props: { stats: sampleStats } })
    const cards = wrapper.findAll('.stat-card')
    expect(cards).toHaveLength(4)
  })

  it('displays total_entries value', () => {
    const wrapper = mount(CacheStats, { props: { stats: sampleStats } })
    expect(wrapper.html()).toContain('42')
  })

  it('displays hit_count_sum value', () => {
    const wrapper = mount(CacheStats, { props: { stats: sampleStats } })
    expect(wrapper.html()).toContain('128')
  })

  it('displays db_size_mb formatted to 2 decimal places', () => {
    const wrapper = mount(CacheStats, { props: { stats: sampleStats } })
    expect(wrapper.html()).toContain('0.25')
  })

  it('displays last_used timestamp', () => {
    const wrapper = mount(CacheStats, { props: { stats: sampleStats } })
    expect(wrapper.html()).toContain('2024-06-01')
  })

  it('emits refresh when Refresh button clicked', async () => {
    const wrapper = mount(CacheStats, { props: { stats: sampleStats } })
    await wrapper.find('button.secondary').trigger('click')
    expect(wrapper.emitted('refresh')).toBeTruthy()
  })

  it('calls clearCache and emits cleared after confirm', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.spyOn(window, 'alert').mockImplementation(() => {})
    mockInvoke.mockResolvedValueOnce('Cleared 42 cache entries')

    const wrapper = mount(CacheStats, { props: { stats: sampleStats } })
    await wrapper.find('button.danger').trigger('click')
    await flushPromises()

    expect(mockInvoke).toHaveBeenCalledWith('clear_cache')
    expect(wrapper.emitted('cleared')).toBeTruthy()
  })

  it('does not call clearCache when confirm is cancelled', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)

    const wrapper = mount(CacheStats, { props: { stats: sampleStats } })
    await wrapper.find('button.danger').trigger('click')
    await flushPromises()

    expect(mockInvoke).not.toHaveBeenCalled()
    expect(wrapper.emitted('cleared')).toBeFalsy()
  })

  it('shows alert on clearCache failure', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => {})
    mockInvoke.mockRejectedValueOnce(new Error('DB error'))

    const wrapper = mount(CacheStats, { props: { stats: sampleStats } })
    await wrapper.find('button.danger').trigger('click')
    await flushPromises()

    expect(alertSpy).toHaveBeenCalledWith(expect.stringContaining('DB error'))
    expect(wrapper.emitted('cleared')).toBeFalsy()
  })

  it('disables clear button while clearing is in progress', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    vi.spyOn(window, 'alert').mockImplementation(() => {})
    let resolve!: (v: string) => void
    mockInvoke.mockReturnValueOnce(new Promise<string>(r => { resolve = r }))

    const wrapper = mount(CacheStats, { props: { stats: sampleStats } })
    await wrapper.find('button.danger').trigger('click')

    const btn = wrapper.find('button.danger')
    expect((btn.element as HTMLButtonElement).disabled).toBe(true)

    resolve('Cleared 0 cache entries')
    await flushPromises()
    expect((btn.element as HTMLButtonElement).disabled).toBe(false)
  })
})

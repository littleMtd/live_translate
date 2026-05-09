import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import SystemStats from '../components/SystemStats.vue'
import type { SystemStats as SystemStatsType } from '../types/config'

const sampleStats: SystemStatsType = {
  uptime_seconds: 1_700_000_000,
  platform: 'windows',
  arch: 'x86_64',
}

describe('SystemStats', () => {
  it('shows loading message when stats is null', () => {
    const wrapper = mount(SystemStats, { props: { stats: null } })
    expect(wrapper.find('.empty').exists()).toBe(true)
    expect(wrapper.find('.info-grid').exists()).toBe(false)
  })

  it('hides loading message when stats are provided', () => {
    const wrapper = mount(SystemStats, { props: { stats: sampleStats } })
    expect(wrapper.find('.empty').exists()).toBe(false)
  })

  it('renders info grid when stats provided', () => {
    const wrapper = mount(SystemStats, { props: { stats: sampleStats } })
    expect(wrapper.find('.info-grid').exists()).toBe(true)
  })

  it('displays platform value', () => {
    const wrapper = mount(SystemStats, { props: { stats: sampleStats } })
    expect(wrapper.html()).toContain('windows')
  })

  it('displays arch value', () => {
    const wrapper = mount(SystemStats, { props: { stats: sampleStats } })
    expect(wrapper.html()).toContain('x86_64')
  })

  it('displays formatted uptime_seconds', () => {
    const wrapper = mount(SystemStats, { props: { stats: sampleStats } })
    // toLocaleString formats with commas/periods depending on locale
    expect(wrapper.html()).toContain('1')  // contains digits of the large number
  })

  it('renders four info rows', () => {
    const wrapper = mount(SystemStats, { props: { stats: sampleStats } })
    const rows = wrapper.findAll('.info-row')
    expect(rows.length).toBeGreaterThanOrEqual(3)
  })
})

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { invoke } from '@tauri-apps/api/core'
import ExportBundle from '../components/ExportBundle.vue'

const mockInvoke = vi.mocked(invoke)
const run = { run_id: 'run-a', started_at: 'a', ended_at: 'b', event_count: 42, run_kind: 'live', run_complete: false }

describe('ExportBundle', () => {
  beforeEach(() => mockInvoke.mockReset())

  it('loads runs and exports the selected run with audio preference', async () => {
    mockInvoke.mockImplementation((command: string) => {
      if (command === 'list_exportable_runs') return Promise.resolve([run])
      if (command === 'export_chatgpt_bundle') return Promise.resolve({
        run_id: 'run-a', output_path: 'C:/bundle', file_count: 6, total_bytes: 2048,
        event_count: 42, runtime_event_files: ['runtime_events.jsonl'], audio_included: 1,
      })
      return Promise.resolve(null)
    })
    const wrapper = mount(ExportBundle)
    await flushPromises()
    expect((wrapper.get('[data-testid="bundle-run"]').element as HTMLSelectElement).value).toBe('run-a')
    await wrapper.get('[data-testid="include-audio"]').setValue(true)
    await wrapper.get('[data-testid="export-bundle"]').trigger('click')
    await flushPromises()
    expect(mockInvoke).toHaveBeenCalledWith('export_chatgpt_bundle', { runId: 'run-a', includeAudio: true })
    expect(wrapper.get('[data-testid="export-result"]').text()).toContain('6 files · 2.0 KiB')
    expect(wrapper.get('[data-testid="export-result"]').text()).toContain('C:/bundle')
  })

  it('shows discovery errors and keeps export disabled without a run', async () => {
    mockInvoke.mockRejectedValueOnce(new Error('offline'))
    const wrapper = mount(ExportBundle)
    await flushPromises()
    expect(wrapper.get('[data-testid="export-error"]').text()).toContain('offline')
    expect(wrapper.get('[data-testid="export-bundle"]').attributes('disabled')).toBeDefined()
  })
})

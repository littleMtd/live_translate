import { describe, it, expect, vi, beforeEach } from 'vitest'
import { invoke } from '@tauri-apps/api/core'
import { TauriClient } from '../api/client'

const mockInvoke = vi.mocked(invoke)

describe('TauriClient', () => {
  let client: TauriClient

  beforeEach(() => {
    client = new TauriClient()
    mockInvoke.mockReset()
  })

  it('getConfig calls get_config command', async () => {
    const fakeConfig = { audio: {}, stt: {}, translation: {} }
    mockInvoke.mockResolvedValueOnce(fakeConfig)
    const result = await client.getConfig()
    expect(mockInvoke).toHaveBeenCalledWith('get_config')
    expect(result).toBe(fakeConfig)
  })

  it('updateConfig calls update_config with newConfig arg', async () => {
    mockInvoke.mockResolvedValueOnce(undefined)
    const cfg = { subtitle: { font_size: 22 } } as any
    await client.updateConfig(cfg)
    expect(mockInvoke).toHaveBeenCalledWith('update_config', { newConfig: cfg })
  })

  it('reloadConfig calls reload_config command', async () => {
    const fakeConfig = { stt: {} }
    mockInvoke.mockResolvedValueOnce(fakeConfig)
    const result = await client.reloadConfig()
    expect(mockInvoke).toHaveBeenCalledWith('reload_config')
    expect(result).toBe(fakeConfig)
  })

  it('getCacheStats calls get_cache_stats command', async () => {
    const stats = { total_entries: 5, hit_count_sum: 10, last_used: '2024-01-01', db_size_mb: 0.1 }
    mockInvoke.mockResolvedValueOnce(stats)
    const result = await client.getCacheStats()
    expect(mockInvoke).toHaveBeenCalledWith('get_cache_stats')
    expect(result).toBe(stats)
  })

  it('clearCache calls clear_cache command', async () => {
    mockInvoke.mockResolvedValueOnce('Cleared 5 cache entries')
    const result = await client.clearCache()
    expect(mockInvoke).toHaveBeenCalledWith('clear_cache')
    expect(result).toBe('Cleared 5 cache entries')
  })

  it('getSystemStats calls get_system_stats command', async () => {
    const stats = { uptime_seconds: 1234, platform: 'windows', arch: 'x86_64' }
    mockInvoke.mockResolvedValueOnce(stats)
    const result = await client.getSystemStats()
    expect(mockInvoke).toHaveBeenCalledWith('get_system_stats')
    expect(result).toBe(stats)
  })

  it('startPython calls start_python command', async () => {
    mockInvoke.mockResolvedValueOnce('Python started (PID: 1234)')
    const result = await client.startPython()
    expect(mockInvoke).toHaveBeenCalledWith('start_python')
    expect(result).toContain('Python started')
  })

  it('stopPython calls stop_python command', async () => {
    mockInvoke.mockResolvedValueOnce('Python process stopped')
    const result = await client.stopPython()
    expect(mockInvoke).toHaveBeenCalledWith('stop_python')
    expect(result).toBe('Python process stopped')
  })

  it('pythonStatus calls python_status command', async () => {
    mockInvoke.mockResolvedValueOnce(true)
    const result = await client.pythonStatus()
    expect(mockInvoke).toHaveBeenCalledWith('python_status')
    expect(result).toBe(true)
  })

  it('propagates invoke rejection', async () => {
    mockInvoke.mockRejectedValueOnce(new Error('Tauri error'))
    await expect(client.getCacheStats()).rejects.toThrow('Tauri error')
  })
})

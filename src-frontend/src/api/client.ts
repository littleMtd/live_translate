import { invoke } from '@tauri-apps/api/core'
import type { ConfigDto, CacheStats, SystemStats } from '../types/config'

export class TauriClient {
  async getConfig(): Promise<ConfigDto> {
    return invoke<ConfigDto>('get_config')
  }

  async updateConfig(config: ConfigDto): Promise<void> {
    return invoke('update_config', { newConfig: config })
  }

  async reloadConfig(): Promise<ConfigDto> {
    return invoke<ConfigDto>('reload_config')
  }

  async getCacheStats(): Promise<CacheStats> {
    return invoke<CacheStats>('get_cache_stats')
  }

  async clearCache(): Promise<string> {
    return invoke<string>('clear_cache')
  }

  async getSystemStats(): Promise<SystemStats> {
    return invoke<SystemStats>('get_system_stats')
  }

  async startPython(): Promise<string> {
    return invoke<string>('start_python')
  }

  async stopPython(): Promise<string> {
    return invoke<string>('stop_python')
  }

  async pythonStatus(): Promise<boolean> {
    return invoke<boolean>('python_status')
  }
}

export const client = new TauriClient()

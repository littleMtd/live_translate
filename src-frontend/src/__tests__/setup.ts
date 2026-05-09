import { vi } from 'vitest'

// Mock the entire Tauri API so tests run outside of a Tauri window.
vi.mock('@tauri-apps/api/core', () => ({
  invoke: vi.fn(),
}))

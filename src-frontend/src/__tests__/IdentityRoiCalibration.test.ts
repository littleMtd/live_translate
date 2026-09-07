import { mount, flushPromises } from '@vue/test-utils'
import { invoke } from '@tauri-apps/api/core'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import IdentityRoiCalibration from '../components/IdentityRoiCalibration.vue'

const mockInvoke = vi.mocked(invoke)

describe('IdentityRoiCalibration', () => {
  beforeEach(() => mockInvoke.mockReset())

  it('launches the shared native calibration UI', async () => {
    mockInvoke.mockResolvedValue('started')
    const wrapper = mount(IdentityRoiCalibration)
    await wrapper.find('[data-testid="start-calibration"]').trigger('click')
    await flushPromises()
    expect(mockInvoke).toHaveBeenCalledWith('launch_identity_roi_calibration')
    expect(wrapper.find('[data-testid="calibration-message"]').text()).toBe('started')
  })
})

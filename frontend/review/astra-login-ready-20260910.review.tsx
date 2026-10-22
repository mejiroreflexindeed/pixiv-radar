import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import App from '../src/App'

vi.mock('../src/echartsSetup', () => ({ initChart: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it('recovers running-state polling after one transient GET failure without focus changes', async () => {
  vi.useFakeTimers()
  let statusReads = 0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const path = new URL(String(input), 'http://localhost').pathname
    const ok = (data: unknown) => ({ ok: true, status: 200, json: async () => data })
    if (path === '/api/discovery/status') {
      statusReads += 1
      if (statusReads === 2) throw new TypeError('fake transient network failure')
      const state = statusReads === 1 ? 'running' : 'succeeded'
      return ok({ state, source: 'mock', current_run: {
        run_key: 'independent-probe', status: state,
        started_at: '2026-09-10T00:00:00Z', error_code: null,
      }, schedule: null })
    }
    if (path === '/api/auth/status') return ok({
      state: 'not_configured', login_available: false, validate_available: false,
      unavailable_reason: 'demo_mode', provider_mode: 'unavailable', can_disconnect: false,
      last_success_at: null, error_code: null, cooldown_until: null, session_expires_at: null,
    })
    if (path === '/api/targets') return ok({ items: [], total: 0, track_total: 0, latest_scan: null })
    return ok({ items: [] })
  }))
  await act(async () => { render(<App />) })
  expect(screen.getByRole('button', { name: '取消发现' })).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
  expect(statusReads).toBe(2)
  await act(async () => { await vi.advanceTimersByTimeAsync(10000) })
  expect(statusReads, 'one failed GET must not permanently stop the running-state watcher').toBeGreaterThan(2)
  expect(screen.getByRole('button', { name: '运行发现' })).toBeEnabled()
})

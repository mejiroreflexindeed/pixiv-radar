import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ConnectionStatus from '../src/components/ConnectionStatus'
import ContentAnalysisPanel from '../src/components/ContentAnalysisPanel'

afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })
const ok = (data: unknown) => ({ ok: true, status: 200, json: async () => data })

it('allows an explicit saved-login retry after cooldown expires without automatically posting', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-09-14T00:00:00Z'))
  let posts = 0
  vi.stubGlobal('fetch', vi.fn(async (_url: unknown, init?: RequestInit) => {
    if (init?.method === 'POST') posts += 1
    return ok({state:'rate_limited', login_available:true, validate_available:true,
      provider_mode:'supervised_browser', can_disconnect:false, last_success_at:null,
      error_code:'authorization_rate_limited', cooldown_until:'2026-09-14T00:00:01Z',
      session_expires_at:null, unavailable_reason:null})
  }))
  await act(async () => { render(<ConnectionStatus />) })
  expect(screen.getByRole('button', {name:'使用已保存登录'})).toBeDisabled()
  await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
  expect(posts).toBe(0)
  expect(screen.getByRole('button', {name:'使用已保存登录'}), 'expired cooldown must expose a manual recovery action').toBeEnabled()
})

it('offers a GET retry when loading local analysis fails, without posting a new text fetch', async () => {
  let cacheReads=0, posts=0
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname
    if (init?.method === 'POST') posts += 1
    if (path === '/api/content/status') return ok({available:true})
    if (path === '/api/content/21') {
      cacheReads += 1
      return {ok:false, status:503, json:async()=>({detail:{code:'pixiv_content_unavailable'}})}
    }
    return ok({items:[]})
  }))
  render(<ContentAnalysisPanel novelId={21} targetState="track" />)
  await screen.findByText('正文暂时无法读取，请稍后重试。')
  const retry = screen.queryByRole('button', {name:/重试|重新读取/})
  expect(retry, 'a transient cache GET failure must not leave the panel with no recovery action').not.toBeNull()
  if (retry) fireEvent.click(retry)
  await waitFor(() => expect(cacheReads).toBeGreaterThan(1))
  expect(posts).toBe(0)
})

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ContentAnalysisPanel from '../src/components/ContentAnalysisPanel'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

const ok = (data: unknown) => ({ ok: true, status: 200, json: async () => data })
const cached = {
  novel_id: 21, analyzer_version: 'content-v1', language: 'ja', char_count: 1200,
  paragraph_count: 8, dialogue_ratio: 0.1, image_marker_count: 0, structure_score: 70,
  is_clutter: false, is_suspect: false, keyword_hits: {}, reason_codes: [],
  analyzed_at: '2026-09-21T00:00:00Z',
}

function installTransport() {
  const calls = { reads: 0, posts: 0 }
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname
    if (init?.method === 'POST') {
      calls.posts += 1
      throw new TypeError('Failed to fetch')
    }
    if (path === '/api/content/status') return ok({ available: true })
    if (path === '/api/content/21') {
      calls.reads += 1
      if (calls.reads === 2) {
        return { ok: false, status: 503, json: async () => ({ detail: { code: 'pixiv_content_unavailable' } }) }
      }
      return ok({ ...cached, char_count: calls.reads > 2 ? 2400 : 1200 })
    }
    return ok({ items: [] })
  }))
  return calls
}

async function reachFailedConfirmation() {
  render(<ContentAnalysisPanel novelId={21} targetState="track" />)
  fireEvent.click(await screen.findByRole('button', { name: '重新抓取并分析' }))
  const confirm = await screen.findByRole('button', { name: '确认本地结果' })
  expect(screen.getByText('1,200')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '重新抓取并分析' })).toBeDisabled()
  fireEvent.click(confirm)
  await screen.findByRole('button', { name: '重试本地读取' })
}

it('keeps trusted cached analysis after an uncertain force POST and a failed confirmation GET', async () => {
  const calls = installTransport()
  await reachFailedConfirmation()
  expect.soft(screen.queryByText('1,200'), 'a failed confirmation GET must retain the last trusted analysis').not.toBeNull()
  expect.soft(screen.queryByText(/抓取结果未确认/), '503 cannot resolve an uncertain POST').not.toBeNull()
  expect(calls).toEqual({ reads: 2, posts: 1 })

  fireEvent.click(screen.getByRole('button', { name: '重试本地读取' }))
  await screen.findByText('2,400')
  expect(screen.getByRole('button', { name: '重新抓取并分析' })).toBeEnabled()
  expect(calls).toEqual({ reads: 3, posts: 1 })
})

it('does not allow a second body POST after the confirmation GET fails, and still recovers with GET', async () => {
  const calls = installTransport()
  await reachFailedConfirmation()
  const postButton = screen.getByRole('button', { name: /^(重新抓取并分析|分析这篇正文)$/ })
  expect.soft(postButton, 'an unresolved POST must stay locked after a transient GET error').toBeDisabled()
  fireEvent.click(postButton)
  await waitFor(() => expect(screen.queryByText('正在分析…')).not.toBeInTheDocument())
  expect.soft(calls.posts, 'manual retry before confirmation must not issue a second upstream body request').toBe(1)

  fireEvent.click(screen.getByRole('button', { name: '重试本地读取' }))
  await screen.findByText('2,400')
  expect(screen.getByRole('button', { name: '重新抓取并分析' })).toBeEnabled()
  expect(calls.reads).toBe(3)
})

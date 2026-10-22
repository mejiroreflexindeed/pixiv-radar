import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import ContentAnalysisPanel from '../src/components/ContentAnalysisPanel'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('settles the analyze button when a local GET retry overlaps an in-flight body POST', async () => {
  const cached = {
    novel_id: 21, analyzer_version: 'content-v1', language: 'ja', char_count: 1200,
    paragraph_count: 8, dialogue_ratio: 0.1, image_marker_count: 0, structure_score: 70,
    is_clutter: false, is_suspect: false, keyword_hits: {}, reason_codes: [],
    analyzed_at: '2026-09-22T00:00:00Z',
  }
  const ok = (data: unknown) => ({ ok: true, status: 200, json: async () => data })
  let reads = 0, posts = 0
  let finishPost: (value: unknown) => void = () => undefined
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input), 'http://localhost').pathname
    if (init?.method === 'POST') {
      posts += 1
      return new Promise((resolve) => { finishPost = resolve })
    }
    if (path === '/api/content/status') return ok({ available: true })
    if (path === '/api/content/21') {
      reads += 1
      if (reads === 1) return { ok: false, status: 503, json: async () => ({ detail: { code: 'pixiv_content_unavailable' } }) }
      return ok(cached)
    }
    return ok({ items: [] })
  }))

  render(<ContentAnalysisPanel novelId={21} targetState="track" />)
  await screen.findByRole('button', { name: '重试本地读取' })
  fireEvent.click(screen.getByRole('button', { name: '分析这篇正文' }))
  expect(screen.getByRole('button', { name: '正在分析…' })).toBeDisabled()
  // Either reject the read while POST is busy, or safely complete both operations.
  // Do not require a concurrent GET: disabling this retry is a valid minimal fix.
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: '重试本地读取' })) })
  await act(async () => { finishPost(ok({ ...cached, char_count: 2400, cached: false })) })
  expect(posts).toBe(1)
  expect([1, 2]).toContain(reads)
  // Both HTTP requests have completed. A manual action must no longer claim ongoing work.
  expect(screen.queryByRole('button', { name: '正在分析…' })).not.toBeInTheDocument()
})

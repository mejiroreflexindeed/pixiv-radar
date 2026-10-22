import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { FeedbackEvent, FeedbackSummary } from '../src/api'
import FeedbackPanel from '../src/components/FeedbackPanel'
import { fetchPreferenceTags, fetchTargetFeedback, submitFeedback } from '../src/api'

vi.mock('../src/api', () => ({
  fetchPreferenceTags: vi.fn(), fetchTargetFeedback: vi.fn(), submitFeedback: vi.fn(),
}))

const getFeedback = vi.mocked(fetchTargetFeedback)
const getTags = vi.mocked(fetchPreferenceTags)
const postFeedback = vi.mocked(submitFeedback)

function summary(id: number): FeedbackSummary {
  return {
    novel_id: id, effective_feedback: null, active_feedback_event_id: null,
    explicit_score: 0, weak_signal_score: 0, score: 0, good_count: 0, exclude_count: 0,
    undo_count: 0, exposure_count: 0, click_count: 0, dwell_count: 0,
    scoring_version: 'feedback-v1', active_event_count: 0, reverted_event_count: 0,
  }
}
function event(id: number, novelId: number, kind: FeedbackEvent['event_type'] = 'click'): FeedbackEvent {
  return {
    id, novel_id: novelId, event_type: kind, reverts_event_id: null,
    scoring_version: 'feedback-v1', weight: 0.05,
    created_at: '2026-09-02T04:00:00+00:00', is_reverted: false,
  }
}
function page(id: number, ids = [10, 9, 8, 7, 6], kind: FeedbackEvent['event_type'] = 'click') {
  return { summary: summary(id), events: ids.map(i => event(i, id, kind)), limit: 5, offset: 0 }
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(r => { resolve = r })
  return { promise, resolve }
}
afterEach(() => { cleanup(); vi.resetAllMocks() })

it('switching targets cancels the old audit page and leaves the new page usable', async () => {
  const pending = deferred<ReturnType<typeof page>>()
  let oldSignal: AbortSignal | undefined
  getFeedback.mockImplementation((id, _limit, offset, signal) => {
    if (id === 1 && offset === 5) { oldSignal = signal; return pending.promise }
    return Promise.resolve(page(id))
  })
  getTags.mockResolvedValue({ items: [] })
  const view = render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '加载更多审计记录' }))
  view.rerender(<FeedbackPanel novelId={2} />)
  await screen.findByRole('button', { name: '标记好文' })
  await act(async () => { pending.resolve(page(1, [5, 4, 3, 2, 1])) })
  expect(oldSignal?.aborted, 'Previous target audit GET was not cancelled').toBe(true)
  expect(screen.getByRole('button', { name: '加载更多审计记录' })).toBeEnabled()
})

it('old audit completion cannot leave the next target permanently loading', async () => {
  const pending = deferred<ReturnType<typeof page>>()
  getFeedback.mockImplementation((id, _limit, offset) =>
    id === 1 && offset === 5 ? pending.promise : Promise.resolve(page(id)),
  )
  getTags.mockResolvedValue({ items: [] })
  const view = render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '加载更多审计记录' }))
  view.rerender(<FeedbackPanel novelId={2} />)
  await screen.findByRole('button', { name: '标记好文' })
  await act(async () => { pending.resolve(page(1, [5, 4, 3, 2, 1])) })
  expect(screen.queryByRole('button', { name: '正在加载…' })).toBeNull()
  expect(screen.getByRole('button', { name: '加载更多审计记录' })).toBeEnabled()
})

it('a successful mutation invalidates pages from the earlier audit snapshot', async () => {
  const pending = deferred<ReturnType<typeof page>>()
  let initialReads = 0
  getFeedback.mockImplementation((id, _limit, offset) => {
    if (offset === 5) return pending.promise
    initialReads++
    return Promise.resolve(page(id, initialReads === 1 ? [10, 9, 8, 7, 6] : [11, 10, 9, 8, 7]))
  })
  getTags.mockResolvedValue({ items: [] })
  postFeedback.mockResolvedValue({
    event: event(11, 1, 'good'),
    summary: { ...summary(1), effective_feedback: 'good', active_feedback_event_id: 11 },
  })
  render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '加载更多审计记录' }))
  fireEvent.click(screen.getByRole('button', { name: '标记好文' }))
  await waitFor(() => expect(postFeedback).toHaveBeenCalledOnce())
  await waitFor(() => expect(screen.getByRole('button', { name: '标记好文' })).toBeEnabled())
  await act(async () => { pending.resolve(page(1, [5, 4, 3, 2, 1], 'exclude')) })
  expect(screen.queryAllByText(/^标记排除 ·/)).toHaveLength(0)
  expect(screen.getAllByRole('listitem')).toHaveLength(5)
})

it('manual retry invalidates an in-flight audit page after an uncertain save', async () => {
  const pending = deferred<ReturnType<typeof page>>()
  let oldSignal: AbortSignal | undefined
  let initialReads = 0
  getFeedback.mockImplementation((id, _limit, offset, signal) => {
    if (offset === 5) { oldSignal = signal; return pending.promise }
    initialReads++
    return Promise.resolve(page(id, initialReads === 1 ? [10, 9, 8, 7, 6] : [11, 10, 9, 8, 7]))
  })
  getTags.mockResolvedValue({ items: [] })
  postFeedback.mockRejectedValue(new Error('response lost after possible commit'))
  render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '加载更多审计记录' }))
  fireEvent.click(screen.getByRole('button', { name: '标记好文' }))
  fireEvent.click(await screen.findByRole('button', { name: '重试读取' }))
  await screen.findByRole('button', { name: '标记好文' })
  await act(async () => { pending.resolve(page(1, [5, 4, 3, 2, 1], 'exclude')) })
  expect.soft(oldSignal?.aborted, 'Retry must cancel the old pagination GET').toBe(true)
  expect.soft(screen.queryAllByText(/^标记排除 ·/), 'Old page must not join the refreshed first page').toHaveLength(0)
  expect(screen.getAllByRole('listitem')).toHaveLength(5)
})

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { FeedbackEvent, FeedbackSummary, FeedbackTag } from '../api'
import FeedbackPanel from './FeedbackPanel'
import {
  fetchPreferenceTags,
  fetchTargetFeedback,
  submitFeedback,
} from '../api'

vi.mock('../api', () => ({
  fetchPreferenceTags: vi.fn(),
  fetchTargetFeedback: vi.fn(),
  submitFeedback: vi.fn(),
}))

const mockedFetchTags = vi.mocked(fetchPreferenceTags)
const mockedFetchFeedback = vi.mocked(fetchTargetFeedback)
const mockedSubmit = vi.mocked(submitFeedback)

function summary(novelId: number, effective: FeedbackSummary['effective_feedback'] = null): FeedbackSummary {
  return {
    novel_id: novelId,
    effective_feedback: effective,
    active_feedback_event_id: effective ? 1 : null,
    explicit_score: effective === 'exclude' ? -1 : effective === 'good' ? 1 : 0,
    weak_signal_score: 0,
    score: effective === 'exclude' ? -1 : effective === 'good' ? 1 : 0,
    good_count: effective === 'good' ? 1 : 0,
    exclude_count: effective === 'exclude' ? 1 : 0,
    undo_count: 0,
    exposure_count: 0,
    click_count: 0,
    dwell_count: 0,
    scoring_version: 'feedback-v1',
    active_event_count: effective ? 1 : 0,
    reverted_event_count: 0,
  }
}

function event(id: number, novelId: number, eventType: FeedbackEvent['event_type']): FeedbackEvent {
  return {
    id,
    novel_id: novelId,
    event_type: eventType,
    reverts_event_id: null,
    scoring_version: 'feedback-v1',
    weight: eventType === 'exclude' ? -1 : eventType === 'good' ? 1 : 0.05,
    created_at: `2026-09-02T04:00:${String(id).padStart(2, '0')}+00:00`,
    is_reverted: false,
  }
}

const tags: FeedbackTag[] = [
  { tag: 'good-tag', translated_name: '正向标签', positive_count: 1, negative_count: 0, weak_signal_count: 0, score: 1 },
  { tag: 'bad-tag', translated_name: '负向标签', positive_count: 0, negative_count: 1, weak_signal_count: 0, score: -1 },
  { tag: 'weak-tag', translated_name: '弱信号标签', positive_count: 0, negative_count: 0, weak_signal_count: 1, score: 0.05 },
]

function resolvedFeedback(novelId: number, items: FeedbackEvent[] = []): { summary: FeedbackSummary; events: FeedbackEvent[]; limit: number; offset: number } {
  return { summary: summary(novelId), events: items, limit: 5, offset: 0 }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('ignores a late response from the previous target', async () => {
  let resolveA: (value: ReturnType<typeof resolvedFeedback>) => void = () => undefined
  const feedbackA = new Promise<ReturnType<typeof resolvedFeedback>>((resolve) => { resolveA = resolve })
  mockedFetchFeedback.mockImplementation((novelId) =>
    novelId === 1 ? feedbackA : Promise.resolve(resolvedFeedback(2)),
  )
  mockedFetchTags.mockResolvedValue({ items: [] })

  const view = render(<FeedbackPanel novelId={1} />)
  view.rerender(<FeedbackPanel novelId={2} />)
  resolveA(resolvedFeedback(1, [event(1, 1, 'good')]))

  expect(await screen.findByText(/当前标记：/)).toBeInTheDocument()
  expect(screen.getByText(/暂无明确反馈/)).toBeInTheDocument()
  expect(screen.queryByText('标记好文 · 2026/09/02 12:00')).toBeNull()
})

it('keeps the successful POST result when the follow-up refresh fails', async () => {
  mockedFetchFeedback
    .mockResolvedValueOnce(resolvedFeedback(1))
    .mockRejectedValueOnce(new Error('read failed'))
  mockedFetchTags
    .mockResolvedValueOnce({ items: [] })
    .mockRejectedValueOnce(new Error('read failed'))
  mockedSubmit.mockResolvedValue({
    event: event(1, 1, 'good'),
    summary: summary(1, 'good'),
  })

  render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '标记好文' }))
  expect(await screen.findByText('已保存，刷新失败，请重试读取')).toBeInTheDocument()
  expect(screen.getByText('好文', { selector: 'strong' })).toBeInTheDocument()
})

it('calls onConfirmed once with the confirmed novel_id after a successful POST', async () => {
  mockedFetchFeedback.mockResolvedValue(resolvedFeedback(1))
  mockedFetchTags.mockResolvedValue({ items: [] })
  mockedSubmit.mockResolvedValue({
    event: event(1, 1, 'good'),
    summary: summary(1, 'good'),
  })
  const onConfirmed = vi.fn()
  render(<FeedbackPanel novelId={1} onConfirmed={onConfirmed} />)
  fireEvent.click(await screen.findByRole('button', { name: '标记好文' }))
  await waitFor(() => expect(onConfirmed).toHaveBeenCalledWith(1))
  expect(onConfirmed).toHaveBeenCalledTimes(1)
})

it('prevents duplicate POST while the first submission is in flight', async () => {
  let resolveSubmit: (value: { event: FeedbackEvent; summary: FeedbackSummary }) => void = () => undefined
  const pending = new Promise<{ event: FeedbackEvent; summary: FeedbackSummary }>((resolve) => { resolveSubmit = resolve })
  mockedFetchFeedback.mockResolvedValue(resolvedFeedback(1))
  mockedFetchTags.mockResolvedValue({ items: [] })
  mockedSubmit.mockReturnValue(pending)

  render(<FeedbackPanel novelId={1} />)
  const button = await screen.findByRole('button', { name: '标记好文' })
  fireEvent.click(button)
  fireEvent.click(button)
  expect(mockedSubmit).toHaveBeenCalledOnce()
  resolveSubmit({ event: event(1, 1, 'good'), summary: summary(1, 'good') })
  await waitFor(() => expect(mockedSubmit).toHaveBeenCalledOnce())
})

it('loads audit pages once and labels positive, negative and weak tags separately', async () => {
  const firstPage = [1, 2, 3, 4, 5].map((id) => event(id, 1, id === 1 ? 'good' : 'click'))
  const secondPage = [5, 6].map((id) => event(id, 1, id === 6 ? 'exclude' : 'click'))
  mockedFetchFeedback
    .mockResolvedValueOnce(resolvedFeedback(1, firstPage))
    .mockResolvedValueOnce({ ...resolvedFeedback(1, secondPage), offset: 5 })
  mockedFetchTags.mockResolvedValue({ items: tags })

  render(<FeedbackPanel novelId={1} />)
  expect(await screen.findByTestId('feedback-tags-positive')).toHaveTextContent('正向标签')
  expect(screen.getByTestId('feedback-tags-negative')).toHaveTextContent('负向标签')
  expect(screen.getByTestId('feedback-tags-neutral')).toHaveTextContent('弱信号标签')
  fireEvent.click(screen.getByRole('button', { name: '加载更多审计记录' }))
  await waitFor(() => expect(screen.getAllByText(/点击弱信号/)).toHaveLength(4))
  expect(screen.getAllByRole('listitem')).toHaveLength(6)
  expect(mockedFetchFeedback).toHaveBeenLastCalledWith(1, 5, 5, expect.any(AbortSignal))
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

it.each(['resolve', 'reject'] as const)('retry discards an old page that later %ss without unlocking the new page', async (completion) => {
  const oldPage = deferred<ReturnType<typeof resolvedFeedback>>()
  const newPage = deferred<ReturnType<typeof resolvedFeedback>>()
  let oldSignal: AbortSignal | undefined
  let reads = 0
  let pages = 0
  mockedFetchFeedback.mockImplementation((id, _limit, offset, signal) => {
    if (offset === 5) {
      pages++
      if (pages === 1) { oldSignal = signal; return oldPage.promise }
      return newPage.promise
    }
    reads++
    return Promise.resolve(resolvedFeedback(id, (reads === 1 ? [10, 9, 8, 7, 6] : [11, 10, 9, 8, 7]).map(i => event(i, id, 'click'))))
  })
  mockedFetchTags.mockResolvedValue({ items: [] })
  mockedSubmit.mockRejectedValue(new Error('response lost'))
  render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '加载更多审计记录' }))
  fireEvent.click(screen.getByRole('button', { name: '标记好文' }))
  fireEvent.click(await screen.findByRole('button', { name: '重试读取' }))
  fireEvent.click(await screen.findByRole('button', { name: '加载更多审计记录' }))
  expect(oldSignal?.aborted).toBe(true)
  await act(async () => {
    if (completion === 'resolve') oldPage.resolve(resolvedFeedback(1, [5, 4, 3, 2, 1].map(i => event(i, 1, 'exclude'))))
    else oldPage.reject(new Error('stale page failed'))
  })
  expect(screen.getByRole('button', { name: '正在加载…' })).toBeDisabled()
  expect(screen.queryByText('审计记录加载失败，请重试读取')).toBeNull()
  expect(screen.queryByText(/^标记排除 ·/)).toBeNull()
  expect(screen.getAllByRole('listitem')).toHaveLength(5)
  await act(async () => { newPage.resolve(resolvedFeedback(1, [6, 5, 4, 3, 2].map(i => event(i, 1, 'click')))) })
  expect(screen.getByRole('button', { name: '加载更多审计记录' })).toBeEnabled()
  expect(screen.getAllByRole('listitem')).toHaveLength(10)
  expect(mockedSubmit).toHaveBeenCalledOnce()
})

it('cannot fetch a page using the old offset while a post-success first page is refreshing', async () => {
  const refreshed = deferred<ReturnType<typeof resolvedFeedback>>()
  let reads = 0
  mockedFetchFeedback.mockImplementation((id, _limit, offset) => {
    if (offset === 5) return Promise.resolve(resolvedFeedback(id, [5, 4, 3, 2, 1].map(i => event(i, id, 'click'))))
    reads++
    return reads === 1
      ? Promise.resolve(resolvedFeedback(id, [10, 9, 8, 7, 6].map(i => event(i, id, 'click'))))
      : refreshed.promise
  })
  mockedFetchTags.mockResolvedValue({ items: [] })
  mockedSubmit.mockResolvedValue({ event: event(11, 1, 'good'), summary: summary(1, 'good') })
  render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '加载更多审计记录' }))
  await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(10))
  fireEvent.click(screen.getByRole('button', { name: '标记好文' }))
  await waitFor(() => expect(reads).toBe(2))
  expect(screen.queryByRole('button', { name: '加载更多审计记录' })).toBeNull()
  expect(screen.queryAllByRole('listitem')).toHaveLength(0)
  await act(async () => { refreshed.resolve(resolvedFeedback(1, [11, 10, 9, 8, 7].map(i => event(i, 1, 'click')))) })
  fireEvent.click(screen.getByRole('button', { name: '加载更多审计记录' }))
  await waitFor(() => expect(mockedFetchFeedback).toHaveBeenLastCalledWith(1, 5, 5, expect.any(AbortSignal)))
  expect(mockedFetchFeedback.mock.calls.some(([, , offset]) => offset === 10)).toBe(false)
})

it('A→B→A switch cancels the old audit page and leaves the new page usable', async () => {
  const pending = deferred<ReturnType<typeof resolvedFeedback>>()
  let oldSignal: AbortSignal | undefined
  mockedFetchFeedback.mockImplementation((id, _limit, offset, signal) => {
    if (id === 1 && offset === 5) { oldSignal = signal; return pending.promise }
    return Promise.resolve(resolvedFeedback(id, [event(10, id, 'click'), event(9, id, 'click'), event(8, id, 'click'), event(7, id, 'click'), event(6, id, 'click')]))
  })
  mockedFetchTags.mockResolvedValue({ items: [] })
  const view = render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '加载更多审计记录' }))
  view.rerender(<FeedbackPanel novelId={2} />)
  await screen.findByRole('button', { name: '标记好文' })
  // 回到 A：新世代的第一页
  view.rerender(<FeedbackPanel novelId={1} />)
  await screen.findByRole('button', { name: '标记好文' })
  pending.resolve(resolvedFeedback(1, [event(5, 1, 'exclude'), event(4, 1, 'click'), event(3, 1, 'click'), event(2, 1, 'click'), event(1, 1, 'click')]))
  expect(oldSignal?.aborted, 'Previous target audit GET was not cancelled').toBe(true)
  // 旧分页结果被丢弃：不出现旧页的排除事件
  expect(screen.queryByText(/^标记排除 ·/)).toBeNull()
  expect(screen.getByRole('button', { name: '加载更多审计记录' })).toBeEnabled()
})

it('unmount during load-more aborts the in-flight audit request', async () => {
  const pending = deferred<ReturnType<typeof resolvedFeedback>>()
  let moreSignal: AbortSignal | undefined
  mockedFetchFeedback.mockImplementation((_id, _limit, offset, signal) => {
    if (offset === 5) { moreSignal = signal; return pending.promise }
    return Promise.resolve(resolvedFeedback(1, [event(10, 1, 'click'), event(9, 1, 'click'), event(8, 1, 'click'), event(7, 1, 'click'), event(6, 1, 'click')]))
  })
  mockedFetchTags.mockResolvedValue({ items: [] })
  const view = render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '加载更多审计记录' }))
  view.unmount()
  expect(moreSignal?.aborted).toBe(true)
})

it('old audit failure does not surface an error on the new target', async () => {
  const pending = deferred<ReturnType<typeof resolvedFeedback>>()
  mockedFetchFeedback.mockImplementation((id, _limit, offset) => {
    if (id === 1 && offset === 5) return pending.promise
    return Promise.resolve(
      resolvedFeedback(id, [20, 19, 18, 17, 16].map((i) => event(i, id, 'click'))),
    )
  })
  mockedFetchTags.mockResolvedValue({ items: [] })
  const view = render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '加载更多审计记录' }))
  view.rerender(<FeedbackPanel novelId={2} />)
  await screen.findByRole('button', { name: '标记好文' })
  pending.reject(new Error('read failed'))
  expect(screen.queryByText('审计记录加载失败，请重试读取')).toBeNull()
  expect(screen.getByRole('button', { name: '加载更多审计记录' })).toBeEnabled()
})

it('submit success then refresh failure retries only the read path', async () => {
  mockedFetchFeedback
    .mockResolvedValueOnce(resolvedFeedback(1))
    .mockRejectedValueOnce(new Error('read failed'))
    .mockResolvedValueOnce(resolvedFeedback(1, [event(1, 1, 'good')]))
  mockedFetchTags
    .mockResolvedValueOnce({ items: [] })
    .mockRejectedValueOnce(new Error('read failed'))
    .mockResolvedValueOnce({ items: [] })
  mockedSubmit.mockResolvedValue({ event: event(1, 1, 'good'), summary: summary(1, 'good') })

  render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '标记好文' }))
  expect(await screen.findByText('已保存，刷新失败，请重试读取')).toBeInTheDocument()
  // 只重试读取，不重发 POST
  fireEvent.click(screen.getByRole('button', { name: '重试读取' }))
  await waitFor(() => expect(screen.getByText(/标记好文 ·/)).toBeInTheDocument())
  expect(mockedSubmit).toHaveBeenCalledOnce()
})

it('a successful mutation invalidates pages from the earlier audit snapshot', async () => {
  const pending = deferred<ReturnType<typeof resolvedFeedback>>()
  let initialReads = 0
  mockedFetchFeedback.mockImplementation((_id, _limit, offset) => {
    if (offset === 5) return pending.promise
    initialReads += 1
    return Promise.resolve(
      resolvedFeedback(1, initialReads === 1
        ? [10, 9, 8, 7, 6].map((i) => event(i, 1, 'click'))
        : [11, 10, 9, 8, 7].map((i) => event(i, 1, i === 11 ? 'good' : 'click'))),
    )
  })
  mockedFetchTags.mockResolvedValue({ items: [] })
  mockedSubmit.mockResolvedValue({ event: event(11, 1, 'good'), summary: summary(1, 'good') })
  render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '加载更多审计记录' }))
  fireEvent.click(screen.getByRole('button', { name: '标记好文' }))
  await waitFor(() => expect(mockedSubmit).toHaveBeenCalledOnce())
  await waitFor(() => expect(screen.getByRole('button', { name: '标记好文' })).toBeEnabled())
  pending.resolve(resolvedFeedback(1, [5, 4, 3, 2, 1].map((i) => event(i, 1, 'exclude'))))
  await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(5))
  expect(screen.queryByText(/^标记排除 ·/)).toBeNull()
})

it('pagination completing before feedback refresh is also discarded', async () => {
  const pending = deferred<ReturnType<typeof resolvedFeedback>>()
  const pendingSubmit = deferred<{ event: FeedbackEvent; summary: FeedbackSummary }>()
  let firstPageReads = 0
  mockedFetchFeedback.mockImplementation((_id, _limit, offset) => {
    if (offset === 5) return pending.promise
    firstPageReads += 1
    return Promise.resolve(
      resolvedFeedback(1, firstPageReads === 1
        ? [10, 9, 8, 7, 6].map((i) => event(i, 1, 'click'))
        : [11, 10, 9, 8, 7].map((i) => event(i, 1, i === 11 ? 'good' : 'click'))),
    )
  })
  mockedFetchTags.mockResolvedValue({ items: [] })
  mockedSubmit.mockReturnValue(pendingSubmit.promise)
  render(<FeedbackPanel novelId={1} />)
  fireEvent.click(await screen.findByRole('button', { name: '加载更多审计记录' }))
  fireEvent.click(screen.getByRole('button', { name: '标记好文' }))
  await waitFor(() => expect(mockedSubmit).toHaveBeenCalledOnce())
  // 旧分页先完成 → 应被随后 POST 成功后的刷新失效，不得混入
  pending.resolve(resolvedFeedback(1, [5, 4, 3, 2, 1].map((i) => event(i, 1, 'exclude'))))
  await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(5))
  pendingSubmit.resolve({ event: event(11, 1, 'good'), summary: summary(1, 'good') })
  await waitFor(() => expect(screen.getAllByRole('listitem')).toHaveLength(5))
  expect(screen.queryByText(/^标记排除 ·/)).toBeNull()
})

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import {
  ContentAnalysis,
  analyzeContent,
  fetchContentAnalysis,
  fetchContentStatus,
  fetchSimilarContent,
} from '../api'
import ContentAnalysisPanel from './ContentAnalysisPanel'

vi.mock('../api', () => ({
  analyzeContent: vi.fn(),
  fetchContentAnalysis: vi.fn(),
  fetchContentStatus: vi.fn(),
  fetchSimilarContent: vi.fn(),
}))

const mAnalyze = vi.mocked(analyzeContent)
const mFetch = vi.mocked(fetchContentAnalysis)
const mStatus = vi.mocked(fetchContentStatus)
const mSimilar = vi.mocked(fetchSimilarContent)

const RESULT = {
  novel_id: 1,
  analyzer_version: 'content-v1',
  language: 'ja',
  char_count: 1234,
  paragraph_count: 8,
  dialogue_ratio: 0.25,
  image_marker_count: 0,
  structure_score: 72.5,
  is_clutter: false,
  is_suspect: false,
  keyword_hits: { トレーナー: 3 },
  reason_codes: [],
  analyzed_at: '2026-09-04T00:00:00+00:00',
}

beforeEach(() => {
  mStatus.mockResolvedValue({ available: true })
  mFetch.mockRejectedValue(new Error('analysis_not_found'))
  mAnalyze.mockResolvedValue({ ...RESULT, cached: false })
  mSimilar.mockResolvedValue({
    items: [],
    method: 'char-ngram-tfidf-v1',
    note: 'text_similarity_not_quality_or_semantic_judgment',
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

it('never starts automatically and analyzes one track target on click', async () => {
  render(<ContentAnalysisPanel novelId={1} targetState="track" />)
  const button = await screen.findByRole('button', { name: '分析这篇正文' })
  expect(mAnalyze).not.toHaveBeenCalled()
  fireEvent.click(button)
  expect(await screen.findByText('1,234')).toBeInTheDocument()
  expect(screen.getByText(/这不是文学质量评分/)).toBeInTheDocument()
  expect(mAnalyze).toHaveBeenCalledWith(1, false)
})

it('shows cached analysis and force-refreshes only after user click', async () => {
  mFetch.mockResolvedValue(RESULT)
  render(<ContentAnalysisPanel novelId={1} targetState="track" />)
  const button = await screen.findByRole('button', { name: '重新抓取并分析' })
  expect(mAnalyze).not.toHaveBeenCalled()
  fireEvent.click(button)
  await waitFor(() => expect(mAnalyze).toHaveBeenCalledWith(1, true))
})

it('does not offer network action for broad targets or demo mode', async () => {
  const view = render(<ContentAnalysisPanel novelId={1} targetState="broad" />)
  expect(await screen.findByText(/尚未进入追尾队列/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /分析/ })).not.toBeInTheDocument()

  mStatus.mockResolvedValue({ available: false })
  view.rerender(<ContentAnalysisPanel novelId={2} targetState="track" />)
  expect(await screen.findByText(/演示模式不请求正文/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /分析/ })).not.toBeInTheDocument()
})

it('drops a stale response after target changes', async () => {
  let resolveFirst: (value: typeof RESULT) => void = () => undefined
  mFetch.mockImplementation((id) => id === 1
    ? new Promise((resolve) => { resolveFirst = resolve })
    : Promise.reject(new Error('analysis_not_found')))
  const view = render(<ContentAnalysisPanel novelId={1} targetState="track" />)
  view.rerender(<ContentAnalysisPanel novelId={2} targetState="track" />)
  resolveFirst(RESULT)
  expect(await screen.findByRole('button', { name: '分析这篇正文' })).toBeInTheDocument()
  expect(screen.queryByText('1,234')).not.toBeInTheDocument()
})

it('offers a GET-only retry when the cached read temporarily fails', async () => {
  mFetch
    .mockRejectedValueOnce(new Error('pixiv_content_unavailable'))
    .mockResolvedValueOnce(RESULT)
  render(<ContentAnalysisPanel novelId={21} targetState="track" />)
  expect(
    await screen.findByText('正文暂时无法读取，请稍后重试。'),
  ).toBeInTheDocument()
  expect(mFetch).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: '重试本地读取' }))
  expect(await screen.findByText('1,234')).toBeInTheDocument()
  expect(mFetch).toHaveBeenCalledTimes(2)
  expect(mAnalyze).not.toHaveBeenCalled()
})

it('shows an empty state for a missing cache without a retry button', async () => {
  mFetch.mockRejectedValue(new Error('HTTP 404'))
  render(<ContentAnalysisPanel novelId={22} targetState="track" />)
  expect(await screen.findByText(/尚未分析/)).toBeInTheDocument()
  expect(
    screen.queryByRole('button', { name: /重试/ }),
  ).not.toBeInTheDocument()
})

it('keeps capability failures separate from cache state with a GET retry', async () => {
  mStatus.mockRejectedValue(new Error('HTTP 503'))
  mFetch.mockResolvedValue(RESULT)
  render(<ContentAnalysisPanel novelId={23} targetState="track" />)
  expect(
    await screen.findByText(/无法读取正文分析能力状态/),
  ).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '重试读取' }))
  await waitFor(() => expect(mStatus).toHaveBeenCalledTimes(2))
  expect(mAnalyze).not.toHaveBeenCalled()
})

it('reconciles an uncertain force result through a read-only confirm before refetching', async () => {
  mFetch.mockResolvedValue(RESULT)
  mAnalyze.mockRejectedValue(new TypeError('Failed to fetch'))
  render(<ContentAnalysisPanel novelId={1} targetState="track" />)
  fireEvent.click(await screen.findByRole('button', { name: '重新抓取并分析' }))
  expect(await screen.findByText('分析失败，请稍后重试。')).toBeInTheDocument()
  // 结果未确认前禁止再次 force
  expect(screen.getByRole('button', { name: '重新抓取并分析' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '确认本地结果' }))
  await waitFor(() => expect(mFetch).toHaveBeenCalledTimes(2))
  expect(mAnalyze).toHaveBeenCalledTimes(1)
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '重新抓取并分析' })).toBeEnabled(),
  )
})

it('keeps trusted cache and POST lock when the confirmation GET fails, then recovers read-only', async () => {
  let reads = 0
  mStatus.mockResolvedValue({ available: true })
  mAnalyze.mockRejectedValue(new TypeError('Failed to fetch'))
  mFetch.mockImplementation(async () => {
    reads += 1
    if (reads === 1) return { ...RESULT, char_count: 1200 }
    if (reads === 2) throw new Error('pixiv_content_unavailable')
    return { ...RESULT, char_count: 2400 }
  })
  render(<ContentAnalysisPanel novelId={1} targetState="track" />)
  fireEvent.click(await screen.findByRole('button', { name: '重新抓取并分析' }))
  fireEvent.click(await screen.findByRole('button', { name: '确认本地结果' }))
  // 503 不能解除不确定结果：保留可信缓存、待确认提示与 POST 锁
  expect(
    await screen.findByRole('button', { name: '重试本地读取' }),
  ).toBeInTheDocument()
  expect(screen.getByText('1,200')).toBeInTheDocument()
  expect(screen.getByText(/抓取结果未确认/)).toBeInTheDocument()
  const locked = screen.getByRole('button', { name: '重新抓取并分析' })
  expect(locked).toBeDisabled()
  fireEvent.click(locked)
  expect(mAnalyze).toHaveBeenCalledTimes(1)
  // 只读重试成功后才解锁并更新为可信结果
  fireEvent.click(screen.getByRole('button', { name: '重试本地读取' }))
  expect(await screen.findByText('2,400')).toBeInTheDocument()
  await waitFor(() =>
    expect(screen.getByRole('button', { name: '重新抓取并分析' })).toBeEnabled(),
  )
})

it('settles after a read retry is attempted during an in-flight analyze POST (R16)', async () => {
  let reads = 0
  let finishPost: (value: ContentAnalysis) => void = () => undefined
  mStatus.mockResolvedValue({ available: true })
  mFetch.mockImplementation(async () => {
    reads += 1
    if (reads === 1) throw new Error('pixiv_content_unavailable')
    return { ...RESULT, novel_id: 24 }
  })
  mAnalyze.mockImplementation(
    () =>
      new Promise((resolve) => {
        finishPost = resolve
      }),
  )
  render(<ContentAnalysisPanel novelId={24} targetState="track" />)
  const retry = await screen.findByRole('button', { name: '重试本地读取' })
  fireEvent.click(screen.getByRole('button', { name: '分析这篇正文' }))
  expect(screen.getByRole('button', { name: '正在分析…' })).toBeDisabled()
  // POST busy 期间重试被禁用且事件入口拦截：不得产生新 GET、不得递增世代
  expect(retry).toBeDisabled()
  fireEvent.click(retry)
  expect(reads).toBe(1)
  await act(async () => {
    finishPost({ ...RESULT, novel_id: 24, char_count: 2400 })
  })
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: '正在分析…' })).toBeNull(),
  )
  expect(mAnalyze).toHaveBeenCalledTimes(1)
  expect(await screen.findByText('2,400')).toBeInTheDocument()
})

it('drops the pending confirmation when switching to another target', async () => {
  mFetch.mockResolvedValue(RESULT)
  mAnalyze.mockRejectedValue(new TypeError('Failed to fetch'))
  const view = render(<ContentAnalysisPanel novelId={1} targetState="track" />)
  fireEvent.click(await screen.findByRole('button', { name: '重新抓取并分析' }))
  await screen.findByText('分析失败，请稍后重试。')
  view.rerender(<ContentAnalysisPanel novelId={2} targetState="track" />)
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: '确认本地结果' })).toBeNull(),
  )
  expect(
    await screen.findByRole('button', { name: '重新抓取并分析' }),
  ).toBeEnabled()
})

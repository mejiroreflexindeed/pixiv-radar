import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import DiscoveryPanel from './DiscoveryPanel'
import { fetchDiscoveryCandidates } from '../api'

vi.mock('../api', () => ({
  fetchDiscoveryCandidates: vi.fn(),
}))

const mCandidates = vi.mocked(fetchDiscoveryCandidates)

const RUN = {
  id: 1,
  run_key: 'demo-1',
  status: 'partial',
  started_at: '2026-09-03T00:00:00+00:00',
  finished_at: '2026-09-03T00:00:05+00:00',
  planned_requests: 8,
  executed_requests: 7,
  results_seen: 12,
  unique_candidates: 5,
  error_code: 'temporary_failure',
}

const STATUS_IDLE = {
  state: 'idle',
  source: 'mock',
  current_run: null,
  schedule: {
    enabled: false,
    tag: null,
    interval_minutes: null,
    next_run_at: null,
    last_attempt_at: null,
    last_error_code: null,
  },
}

const CANDIDATE = {
  novel_id: 7001,
  title: 'メジロアルダンの朝',
  author_name: '作者7001',
  total_bookmarks: 40,
  total_view: 300,
  total_comments: 0,
  sightings: [
    { source_kind: 'exact_tag', query_key: 'exact_tag:ウマ娘', query_text: 'ウマ娘', source_rank: 1, discovered_at: '2026-09-03T00:00:00+00:00' },
    { source_kind: 'novel_new', query_key: 'novel_new:all', query_text: null, source_rank: 1, discovered_at: '2026-09-03T00:00:00+00:00' },
  ],
  group: 'small_gem',
  group_label: '小众新发现',
  group_reason: '小众新发现：阅读量不高但相对收藏与增长信号较强',
  explanations: ['2 个来源发现（exact_tag、novel_new）', '收藏/阅读比 0.0800'],
  score_fields: { rate: 0.08, velocity: 12, snapshots: 3, sources: 2, feedback: null },
  filtered_reason: null,
}

const CANDIDATES = {
  items: [CANDIDATE],
  total: 1,
  limit: 100,
  offset: 0,
}

function renderPanel(overrides: Record<string, unknown> = {}) {
  const props = {
    status: STATUS_IDLE,
    latestRun: RUN,
    busy: false,
    error: null,
    candidatesVersion: 0,
    onStart: vi.fn(),
    onCancel: vi.fn(),
    onSelect: vi.fn(),
    ...overrides,
  }
  render(<DiscoveryPanel {...props} />)
  return props
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

beforeEach(() => {
  mCandidates.mockResolvedValue(CANDIDATES)
})

describe('DiscoveryPanel', () => {
  it('shows the mock source label and renders candidates from the latest run', async () => {
    renderPanel()
    expect(await screen.findByText(/来源：mock/)).toBeInTheDocument()
    expect(await screen.findByText('メジロアルダンの朝')).toBeInTheDocument()
    // 行内 badge 与筛选 option 必须分别存在，不能只靠任意文本存在掩盖候选缺失
    const row = screen.getByRole('row', { name: /メジロアルダンの朝/ })
    expect(within(row).getByText(/exact_tag#1、novel_new#1/)).toBeInTheDocument()
    expect(within(row).getByText('小众新发现')).toBeInTheDocument()
    const rowText = row.textContent ?? ''
    expect(rowText).toMatch(/2 个来源发现/)
    expect(
      screen.getByRole('option', { name: '小众新发现' }),
    ).toBeInTheDocument()
    expect(screen.getAllByText(/部分成功/).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText(/本次共 1 个候选/)).toBeInTheDocument()
  })

  it('clips long author and source text inside fixed-width cells (R19)', async () => {
    const longAuthor = '超長作者名テスト'.repeat(20)
    mCandidates.mockResolvedValue({
      ...CANDIDATES,
      items: [{ ...CANDIDATE, author_name: longAuthor }],
    })
    renderPanel()
    const row = await screen.findByRole('row', { name: /メジロアルダンの朝/ })
    const authorClip = within(row).getByText(longAuthor)
    expect(authorClip.className).toContain('cell-clip')
    expect(authorClip.getAttribute('title')).toBe(longAuthor)
    const sourceClip = within(row).getByText(/exact_tag#1、novel_new#1/)
    expect(sourceClip.className).toContain('cell-clip')
    expect(sourceClip.getAttribute('title')).toBe('exact_tag#1、novel_new#1')
  })

  it('reads at most two pages and flags truncation beyond 200', async () => {
    const firstPage = Array.from({ length: 100 }, (_, i) => ({ ...CANDIDATE, novel_id: i + 1 }))
    const secondPage = Array.from({ length: 100 }, (_, i) => ({ ...CANDIDATE, novel_id: i + 101 }))
    mCandidates.mockImplementation(async (_id, _limit, offset) =>
      offset === 0
        ? { items: firstPage, total: 250, limit: 100, offset: 0 }
        : { items: secondPage, total: 250, limit: 100, offset: 100 },
    )
    renderPanel()
    expect(await screen.findByText(/超过 200，已按上限截断/)).toBeInTheDocument()
    expect(mCandidates).toHaveBeenCalledTimes(2)
    expect(mCandidates).toHaveBeenNthCalledWith(2, 1, 100, 100)
  })

  it('starts discovery only from an explicit click and passes the trimmed tag', async () => {
    const props = renderPanel()
    fireEvent.change(await screen.findByLabelText('发现标签'), {
      target: { value: '  メジロ  ' },
    })
    expect(props.onStart).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '运行发现' }))
    expect(props.onStart).toHaveBeenCalledWith('メジロ')
  })

  it('does not start a second run while one is in progress and can cancel', async () => {
    const props = renderPanel({
      status: { ...STATUS_IDLE, state: 'running' },
      busy: true,
    })
    const button = await screen.findByRole('button', { name: '运行发现' })
    await waitFor(() => expect(button).toBeDisabled())
    fireEvent.click(button)
    expect(props.onStart).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '取消发现' }))
    expect(props.onCancel).toHaveBeenCalledTimes(1)
  })

  it('shows the scheduled next run from the app-level status', async () => {
    renderPanel({
      status: {
        ...STATUS_IDLE,
        state: 'running',
        source: 'pixiv',
        schedule: {
          enabled: true,
          tag: 'ウマ娘',
          interval_minutes: 30,
          next_run_at: '2026-09-04T08:00:00+00:00',
          last_attempt_at: null,
          last_error_code: null,
        },
      },
      busy: true,
    })
    expect(await screen.findByText(/自动发现已启用：标签 ウマ娘，每 30 分钟/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '运行发现' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '取消发现' })).toBeInTheDocument()
  })

  it('reloads candidates when the app bumps candidatesVersion', async () => {
    const { rerender } = render(
      <DiscoveryPanel
        status={STATUS_IDLE}
        latestRun={RUN}
        busy={false}
        error={null}
        candidatesVersion={0}
        onStart={vi.fn()}
        onCancel={vi.fn()}
        onSelect={vi.fn()}
      />,
    )
    await waitFor(() => expect(mCandidates).toHaveBeenCalledTimes(1))
    rerender(
      <DiscoveryPanel
        status={STATUS_IDLE}
        latestRun={RUN}
        busy={false}
        error={null}
        candidatesVersion={1}
        onStart={vi.fn()}
        onCancel={vi.fn()}
        onSelect={vi.fn()}
      />,
    )
    await waitFor(() => expect(mCandidates).toHaveBeenCalledTimes(2))
  })

  it('filters candidates by title and group over the complete set', async () => {
    const other = { ...CANDIDATE, novel_id: 7002, title: '別の作品', author_name: '別作者', group: 'popular_reference', group_label: '热门参考' }
    mCandidates.mockResolvedValue({ items: [CANDIDATE, other], total: 2, limit: 100, offset: 0 })
    renderPanel()
    expect(await screen.findByText('メジロアルダンの朝')).toBeInTheDocument()
    expect(screen.getByText('別の作品')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('候选搜索'), { target: { value: 'メジロ' } })
    expect(screen.queryByText('別の作品')).toBeNull()
    fireEvent.change(screen.getByLabelText('候选搜索'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('候选分组筛选'), { target: { value: 'popular_reference' } })
    expect(screen.queryByText('メジロアルダンの朝')).toBeNull()
    expect(screen.getByText('別の作品')).toBeInTheDocument()
  })

  it('shows the shared-gate pause reason with cooldown countdown', async () => {
    renderPanel({
      status: {
        ...STATUS_IDLE,
        gate: {
          busy: false,
          paused_reason: 'rate_limited',
          cooldown_until: 12345.0,
          cooldown_remaining_seconds: 125,
          min_interval_seconds: 3,
        },
      },
    })
    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent(/共享出站已暂停：触发限流/)
    expect(banner).toHaveTextContent(/冷却剩余 2 分 5 秒/)
    expect(banner).toHaveTextContent(/不会自动重试/)
  })

  it('shows an auth pause without a countdown and points to reconnect', async () => {
    renderPanel({
      status: {
        ...STATUS_IDLE,
        gate: {
          busy: false,
          paused_reason: 'auth_failed',
          cooldown_until: null,
          cooldown_remaining_seconds: null,
          min_interval_seconds: 3,
        },
      },
    })
    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent(/需要重新授权/)
    expect(banner).toHaveTextContent(/使用已保存登录/)
  })

  it('keeps showing an active cooldown after a fresh login cleared the pause', async () => {
    renderPanel({
      status: {
        ...STATUS_IDLE,
        gate: {
          busy: false,
          paused_reason: null,
          cooldown_until: 999.0,
          cooldown_remaining_seconds: 42,
          min_interval_seconds: 3,
        },
      },
    })
    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent(/共享出站处于限流冷却：剩余 42 秒/)
    expect(banner).toHaveTextContent(/不会自动重试/)
  })

  it('shows running progress from the app-level status', async () => {
    renderPanel({
      status: {
        ...STATUS_IDLE,
        state: 'running',
        current_run: {
          run_key: 'k-progress',
          status: 'running',
          started_at: '2026-09-13T00:00:00+00:00',
          error_code: null,
          planned_requests: 3,
          executed_requests: 1,
        },
      },
      busy: true,
    })
    expect(
      await screen.findByText(/运行中：已完成 1\/3 个来源/),
    ).toBeInTheDocument()
  })

  it('selects a target when a candidate row is clicked', async () => {
    const props = renderPanel()
    const row = await screen.findByRole('row', { name: /メジロアルダンの朝/ })
    fireEvent.click(row)
    expect(props.onSelect).toHaveBeenCalledWith(7001)
  })

  it('shows a bounded error and retries the candidate read', async () => {
    mCandidates
      .mockRejectedValueOnce(new Error('boom'))
      .mockResolvedValueOnce(CANDIDATES)
    renderPanel()
    expect(await screen.findByText(/候选读取失败/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByText('メジロアルダンの朝')).toBeInTheDocument()
  })
})

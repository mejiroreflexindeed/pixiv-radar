import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const chartState = vi.hoisted(() => ({
  handlers: {} as Record<string, ((p: unknown) => void)[]>,
  setOption: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn(),
}))

vi.mock('./echartsSetup', () => ({
  initChart: vi.fn(() => ({
    setOption: chartState.setOption,
    resize: chartState.resize,
    dispose: chartState.dispose,
    on: vi.fn((event: string, handler: (p: unknown) => void) => {
      ;(chartState.handlers[event] ??= []).push(handler)
    }),
  })),
}))

const AUTH_STATUS = {
  state: 'not_configured',
  login_available: false,
  provider_mode: 'unavailable',
  can_disconnect: false,
  last_success_at: null,
  error_code: null,
  cooldown_until: null,
  session_expires_at: null,
  validate_available: false,
  unavailable_reason: 'demo_mode',
}

const LATEST_SCAN = {
  id: 7,
  layer: 'broad',
  status: 'succeeded',
  started_at: '2026-08-30T06:00:00+00:00',
  finished_at: '2026-08-30T06:00:01+00:00',
  error: null,
  results_seen: 12,
  results_written: 12,
  source: 'mock',
  run_key: 'k6',
}

const TARGET_A = {
  novel_id: 1,
  title: 'novel-01',
  author_name: 'author-1',
  text_length: 1000,
  total_bookmarks: 40,
  total_view: 700,
  total_comments: 0,
  last_seen_at: '2026-08-30T06:00:00+00:00',
  snapshot_at: '2026-08-30T06:00:00+00:00',
  state: 'track',
  observation_count: 3,
  promotion_streak: 0,
  demotion_streak: 0,
  latest_result: 'qualified',
  latest_ewma: 200,
  latest_threshold: 5,
  latest_threshold_source: 'fallback',
  signal_ratio: 40,
  data_source: 'mock',
}

const TARGET_B = {
  ...TARGET_A,
  novel_id: 2,
  title: 'novel-02',
  author_name: 'author-2',
  state: 'cold_start',
  latest_result: 'unknown',
  latest_ewma: null,
  latest_threshold: null,
  latest_threshold_source: null,
  signal_ratio: null,
  total_view: 15,
  total_bookmarks: 1,
}

const TARGETS_RESPONSE = {
  items: [TARGET_A, TARGET_B],
  total: 2,
  track_total: 1,
  limit: 50,
  offset: 0,
  latest_scan: LATEST_SCAN,
}

const HEATMAP_RESPONSE = {
  items: [
    {
      novel_id: 1,
      title: 'novel-01',
      author_name: 'author-1',
      state: 'track',
      points: [
        {
          scan_run_id: 1,
          captured_at: '2026-08-30T00:00:00+00:00',
          ewma: 100,
          threshold: 5,
          result: 'qualified',
          reason_codes: [],
        },
      ],
    },
    {
      novel_id: 2,
      title: 'novel-02',
      author_name: 'author-2',
      state: 'cold_start',
      points: [],
    },
  ],
}

const RUNS_RESPONSE = { items: [LATEST_SCAN] }
const EVENTS_RESPONSE = {
  items: [
    {
      id: 1,
      event_key: 'target_entered_track:1:3',
      type: 'target_entered_track',
      severity: 'info',
      payload: {},
      novel_id: 1,
      scan_run_id: 3,
      created_at: '2026-08-30T02:00:00+00:00',
    },
  ],
}

const DISCOVERY_RUN = {
  id: 11,
  run_key: 'mock-run-1',
  status: 'succeeded',
  started_at: '2026-08-30T05:00:00+00:00',
  finished_at: '2026-08-30T05:00:02+00:00',
  planned_requests: 8,
  executed_requests: 8,
  results_seen: 10,
  unique_candidates: 2,
  error_code: null,
}

const DISCOVERY_STATUS_IDLE = {
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
  novel_id: 1,
  title: 'novel-01',
  author_name: 'author-1',
  total_bookmarks: 40,
  total_view: 700,
  total_comments: 0,
  sightings: [
    {
      source_kind: 'exact_tag',
      query_key: 'exact_tag:ウマ娘',
      query_text: 'ウマ娘',
      source_rank: 1,
      discovered_at: '2026-08-30T05:00:00+00:00',
    },
  ],
  group: 'small_gem',
  group_label: '小众新发现',
  group_reason: '小众新发现',
  explanations: ['2 个来源发现'],
  score_fields: { rate: 0.08, velocity: 12, snapshots: 3, sources: 2, feedback: null },
  filtered_reason: null,
}

const HISTORY_RESPONSE = {
  items: [
    {
      novel_id: 1,
      scan_run_id: 1,
      captured_at: '2026-08-30T00:00:00+00:00',
      total_view: 300,
      total_bookmarks: 10,
      result: 'unknown',
      ewma_view_velocity: null,
      threshold: 5,
    },
    {
      novel_id: 1,
      scan_run_id: 7,
      captured_at: '2026-08-30T06:00:00+00:00',
      total_view: 700,
      total_bookmarks: 40,
      result: 'qualified',
      ewma_view_velocity: 200,
      threshold: 5,
    },
  ],
}

const FEEDBACK_RESPONSE = {
  summary: {
    novel_id: 1,
    effective_feedback: null,
    active_feedback_event_id: null,
    explicit_score: 0,
    weak_signal_score: 0,
    score: 0,
    good_count: 0,
    exclude_count: 0,
    undo_count: 0,
    exposure_count: 0,
    click_count: 0,
    dwell_count: 0,
    scoring_version: 'feedback-v1',
    active_event_count: 0,
    reverted_event_count: 0,
  },
  events: [],
  limit: 5,
  offset: 0,
}

const fetchCalls: string[] = []
const fetchSignals: AbortSignal[] = []

function jsonResponse(data: unknown) {
  return { ok: true, status: 200, json: async () => data }
}

function routeOf(input: RequestInfo | URL): URL {
  return new URL(String(input), 'http://localhost')
}

function mockFetchRoutes(routes: Record<string, unknown>, options?: { delayMs?: number }) {
  const delayMs = options?.delayMs ?? 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      fetchCalls.push(url)
      if (init?.signal) fetchSignals.push(init.signal as AbortSignal)
      if (init?.signal?.aborted) {
        throw new DOMException('The operation was aborted.', 'AbortError')
      }
      if (delayMs > 0) {
        await new Promise((resolve) => setTimeout(resolve, delayMs))
      }
      if (init?.signal?.aborted) {
        throw new DOMException('The operation was aborted.', 'AbortError')
      }
      // pathname 精确匹配：详情/历史/分页列表互不误匹配（列表 query 不影响）
      const pathname = routeOf(input).pathname
      const value = routes[pathname]
      if (value !== undefined) return jsonResponse(value)
      return { ok: false, status: 404, json: async () => ({}) }
    }),
  )
}

const DEFAULT_ROUTES = {
  '/api/health': {
    status: 'ok',
    app: 'pixiv-radar',
    version: '0.1.0',
    time: '2026-09-12T00:00:00+00:00',
    runtime: {
      mode: 'demo',
      db_path: 'D:\\codex\\12\\data\\local-preview\\radar.db',
      db_scope: 'preview',
      schedule_minutes: 0,
      schedule_tag: 'ウマ娘',
    },
  },
  '/api/auth/status': AUTH_STATUS,
  '/api/targets/1/history': HISTORY_RESPONSE,
  '/api/targets/2/history': { items: [] },
  '/api/targets/1': TARGET_A,
  '/api/targets/2': TARGET_B,
  '/api/targets': TARGETS_RESPONSE,
  '/api/dashboard/heatmap': HEATMAP_RESPONSE,
  '/api/scan-runs': RUNS_RESPONSE,
  '/api/events': EVENTS_RESPONSE,
  '/api/discovery/status': DISCOVERY_STATUS_IDLE,
  '/api/discovery/runs': { items: [] },
  '/api/discovery/candidates': { items: [CANDIDATE], total: 1, limit: 100, offset: 0 },
  '/api/feedback/targets/1': FEEDBACK_RESPONSE,
  '/api/feedback/targets/2': {
    ...FEEDBACK_RESPONSE,
    summary: { ...FEEDBACK_RESPONSE.summary, novel_id: 2 },
  },
  '/api/feedback/tags': { items: [] },
  '/api/content/status': { available: false },
}

function openTargetsView() {
  fireEvent.click(screen.getByRole('button', { name: '我的作品' }))
}

function openCharts() {
  fireEvent.click(screen.getByRole('button', { name: '展开雷达与热力图' }))
}

beforeEach(() => {
  fetchCalls.length = 0
  fetchSignals.length = 0
  chartState.handlers = {}
  chartState.setOption.mockClear()
  chartState.resize.mockClear()
  chartState.dispose.mockClear()
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('App（登录即可用版）', () => {
  it('shows loading then renders discovery view with demo banner and charts', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    render(<App />)
    expect(screen.getByText('正在加载数据…')).toBeInTheDocument()
    expect(await screen.findByTestId('discovery-panel')).toBeInTheDocument()
    expect(await screen.findByRole('alert')).toHaveTextContent(/演示数据/)
    openCharts()
    expect(screen.getByTestId('ppi-radar')).toBeInTheDocument()
    expect(screen.getByTestId('score-heatmap')).toBeInTheDocument()
  })

  it('renders the target table and badges in the targets view', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    render(<App />)
    openTargetsView()
    expect(await screen.findByRole('table', { name: /目标列表/ })).toBeInTheDocument()
    expect(screen.getAllByText('追尾').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('合格').length).toBeGreaterThanOrEqual(1)
  })

  it('renders the run history and event log in the runs view', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    render(<App />)
    fireEvent.click(screen.getByRole('button', { name: '运行与设置' }))
    expect(await screen.findByText('扫描历史')).toBeInTheDocument()
    expect(screen.getByText('事件日志')).toBeInTheDocument()
    const runtimeCard = await screen.findByTestId('runtime-card')
    expect(within(runtimeCard).getByText(/演示模式 · 不连接 Pixiv/)).toBeInTheDocument()
    expect(
      within(runtimeCard).getByText(/data\\local-preview\\radar\.db（演示预览库）/),
    ).toBeInTheDocument()
    expect(within(runtimeCard).getByText(/自动发现：关闭/)).toBeInTheDocument()
  })

  it('shows empty state when no targets', async () => {
    mockFetchRoutes({
      ...DEFAULT_ROUTES,
      '/api/targets': {
        items: [],
        total: 0,
        track_total: 0,
        limit: 50,
        offset: 0,
        latest_scan: null,
      },
    })
    render(<App />)
    openTargetsView()
    expect(await screen.findByText(/共 0 条/)).toBeInTheDocument()
    expect(screen.getByText(/暂无目标/)).toBeInTheDocument()
    expect(screen.getByText(/尚无扫描/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '下一页' })).toBeNull()
    expect(screen.queryByRole('button', { name: '上一页' })).toBeNull()
  })

  it('keeps login, navigation and retry visible when the first load fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('connection refused')
      }),
    )
    render(<App />)
    expect(screen.getByRole('button', { name: '发现' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '我的作品' })).toBeInTheDocument()
    openTargetsView()
    expect(await screen.findByText(/目标列表加载失败/)).toBeInTheDocument()
    expect(
      screen.getAllByRole('button', { name: '重试' }).length,
    ).toBeGreaterThanOrEqual(1)
  })

  it('applies state filter and refetches with query param', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    render(<App />)
    openTargetsView()
    await screen.findByText('novel-01')
    fetchCalls.length = 0
    fireEvent.change(screen.getByLabelText('状态筛选'), {
      target: { value: 'track' },
    })
    await waitFor(() => {
      expect(fetchCalls.some((url) => url.includes('state=track'))).toBe(true)
    })
  })

  it('debounces title/author query and refetches', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    render(<App />)
    openTargetsView()
    await screen.findByText('novel-01')
    fetchCalls.length = 0
    fireEvent.change(screen.getByLabelText('标题或作者查询'), {
      target: { value: 'author-1' },
    })
    await waitFor(
      () => {
        expect(
          fetchCalls.some((url) => url.includes('q=author-1')),
        ).toBe(true)
      },
      { timeout: 2000 },
    )
  })

  it('sorts by title via table header and refetches with sort params', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    render(<App />)
    openTargetsView()
    await screen.findByText('novel-01')
    fetchCalls.length = 0
    fireEvent.click(screen.getByRole('button', { name: /标题/ }))
    await waitFor(() => {
      expect(
        fetchCalls.some(
          (url) => url.includes('sort=title') && url.includes('sort_dir=asc'),
        ),
      ).toBe(true)
    })
  })

  it('paginates with limit/offset and disables boundary buttons', async () => {
    let total = 51
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = routeOf(input)
        fetchCalls.push(String(input))
        if (url.pathname === '/api/targets') {
          const offset = Number(url.searchParams.get('offset') ?? '0')
          const items = offset === 0 ? [TARGET_A, TARGET_B] : [TARGET_A]
          return jsonResponse({
            items,
            total,
            track_total: 1,
            limit: 50,
            offset,
            latest_scan: LATEST_SCAN,
          })
        }
        const value = (DEFAULT_ROUTES as Record<string, unknown>)[url.pathname]
        if (value !== undefined) return jsonResponse(value)
        return { ok: false, status: 404, json: async () => ({}) }
      }),
    )
    render(<App />)
    openTargetsView()
    await screen.findByText('novel-01')
    const next = screen.getByRole('button', { name: '下一页' })
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled()
    fireEvent.click(next)
    await waitFor(() => {
      expect(
        fetchCalls.some(
          (call) => call.includes('/api/targets') && call.includes('offset=50'),
        ),
      ).toBe(true)
    })
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled()
    })
    // 数据收缩后页码越界自动回到最后合法页（不无限重试）
    total = 5
    fireEvent.change(screen.getByLabelText('状态筛选'), {
      target: { value: 'track' },
    })
    await waitFor(() => {
      expect(
        fetchCalls.some(
          (call) => call.includes('/api/targets') && call.includes('offset=0'),
        ),
      ).toBe(true)
    })
  })

  it('selects target by row click and loads detail and history', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    render(<App />)
    openTargetsView()
    const row = await screen.findByRole('row', { name: /novel-01/ })
    fireEvent.click(row)
    expect(
      await screen.findByRole('heading', { name: /详情曲线：novel-01/ }),
    ).toBeInTheDocument()
    expect(screen.getByTestId('history-chart')).toBeInTheDocument()
    await waitFor(() => {
      expect(
        fetchCalls.some((url) => url.includes('/api/targets/1/history')),
      ).toBe(true)
    })
  })

  it('drops a late detail response from the previous target (A slow, B fast)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = routeOf(input)
        fetchCalls.push(String(input))
        if (url.pathname === '/api/targets/1') {
          await new Promise((resolve) => setTimeout(resolve, 150))
          return jsonResponse(TARGET_A)
        }
        const value = (DEFAULT_ROUTES as Record<string, unknown>)[url.pathname]
        if (value !== undefined) return jsonResponse(value)
        return { ok: false, status: 404, json: async () => ({}) }
      }),
    )
    render(<App />)
    openTargetsView()
    fireEvent.click(await screen.findByRole('row', { name: /novel-01/ }))
    fireEvent.click(await screen.findByRole('row', { name: /novel-02/ }))
    expect(
      await screen.findByRole('heading', { name: /详情曲线：novel-02/ }),
    ).toBeInTheDocument()
    await new Promise((resolve) => setTimeout(resolve, 220))
    expect(
      screen.queryByRole('heading', { name: /详情曲线：novel-01/ }),
    ).toBeNull()
    expect(
      screen.getByRole('heading', { name: /详情曲线：novel-02/ }),
    ).toBeInTheDocument()
  })

  it('retries a failed detail GET without re-running discovery', async () => {
    let detailCalls = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = routeOf(input)
        fetchCalls.push(String(input))
        if (url.pathname === '/api/targets/1') {
          detailCalls += 1
          if (detailCalls === 1) {
            return { ok: false, status: 500, json: async () => ({}) }
          }
          return jsonResponse(TARGET_A)
        }
        const value = (DEFAULT_ROUTES as Record<string, unknown>)[url.pathname]
        if (value !== undefined) return jsonResponse(value)
        return { ok: false, status: 404, json: async () => ({}) }
      }),
    )
    render(<App />)
    openTargetsView()
    fireEvent.click(await screen.findByRole('row', { name: /novel-01/ }))
    expect(await screen.findByText(/详情读取失败/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(
      await screen.findByRole('heading', { name: /详情曲线：novel-01/ }),
    ).toBeInTheDocument()
    expect(fetchCalls.some((url) => url.includes('/api/discovery/start'))).toBe(false)
  })

  it('shows feedback actions and posts an explicit good signal', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    render(<App />)
    openTargetsView()
    const row = await screen.findByRole('row', { name: /novel-01/ })
    fireEvent.click(row)
    expect(await screen.findByTestId('feedback-panel')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '标记好文' }))
    await waitFor(() => {
      expect(fetchCalls.some((url) => url.endsWith('/api/feedback'))).toBe(true)
    })
  })

  it('reloads candidate ranking after a confirmed feedback POST (single POST)', async () => {
    mockFetchRoutes({
      ...DEFAULT_ROUTES,
      '/api/discovery/runs': { items: [DISCOVERY_RUN] },
      '/api/feedback': {
        event: {
          id: 9,
          novel_id: 1,
          event_type: 'good',
          reverts_event_id: null,
          scoring_version: 'feedback-v1',
          weight: 1,
          created_at: '2026-09-09T00:00:00+00:00',
        },
        summary: { ...FEEDBACK_RESPONSE.summary, effective_feedback: 'good' },
      },
    })
    render(<App />)
    await waitFor(() => {
      expect(
        fetchCalls.filter((url) => url.includes('/api/discovery/candidates')).length,
      ).toBe(1)
    })
    fireEvent.click(
      await screen.findByRole('row', { name: /novel-01/ }),
    )
    fireEvent.click(await screen.findByRole('button', { name: '标记好文' }))
    await waitFor(() => {
      expect(
        fetchCalls.filter((url) => url.includes('/api/discovery/candidates')).length,
      ).toBeGreaterThanOrEqual(2)
    })
    const posts = fetchCalls.filter((url) => url.endsWith('/api/feedback'))
    expect(posts.length).toBe(1)
  })

  it('renders detail without crashing when metric fields are missing', async () => {
    const sparse = {
      ...TARGET_A,
      signal_ratio: undefined,
      latest_ewma: undefined,
      latest_threshold: undefined,
      latest_threshold_source: undefined,
    }
    mockFetchRoutes({ ...DEFAULT_ROUTES, '/api/targets/1': sparse })
    render(<App />)
    openTargetsView()
    fireEvent.click(await screen.findByRole('row', { name: /novel-01/ }))
    expect(
      await screen.findByRole('heading', { name: /详情曲线：novel-01/ }),
    ).toBeInTheDocument()
    expect(screen.getByText(/增长信号\/门限比：—/)).toBeInTheDocument()
  })

  it('shows not-in-this-run note when the reason response belongs to another novel', async () => {
    mockFetchRoutes({
      ...DEFAULT_ROUTES,
      '/api/discovery/runs': { items: [DISCOVERY_RUN] },
      // 故意返回 novel 1 的理由，即使选中的是 novel 2
      '/api/discovery/candidates': {
        items: [CANDIDATE],
        total: 1,
        limit: 1,
        offset: 0,
      },
    })
    render(<App />)
    openTargetsView()
    fireEvent.click(await screen.findByRole('row', { name: /novel-02/ }))
    const reason = await screen.findByTestId('candidate-reason')
    expect(
      await within(reason).findByText(/不在本轮候选列表中/),
    ).toBeInTheDocument()
    expect(within(reason).queryByText(/2 个来源发现/)).toBeNull()
  })

  it('selects target by keyboard Enter on row', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    render(<App />)
    openTargetsView()
    const row = await screen.findByRole('row', { name: /novel-01/ })
    fireEvent.keyDown(row, { key: 'Enter' })
    expect(
      await screen.findByRole('heading', { name: /详情曲线：novel-01/ }),
    ).toBeInTheDocument()
  })

  it('selects target from radar chart click event', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    render(<App />)
    await screen.findByTestId('discovery-panel')
    openCharts()
    act(() => {
      const handler = chartState.handlers['click']?.[0]
      expect(handler).toBeDefined()
      handler?.({ data: { novel_id: 2 } })
    })
    expect(
      await screen.findByRole('heading', { name: /详情曲线：novel-02/ }),
    ).toBeInTheDocument()
  })

  it('selects target from heatmap chart click event', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    render(<App />)
    await screen.findByTestId('discovery-panel')
    openCharts()
    act(() => {
      const handler = chartState.handlers['click']?.[1]
      expect(handler).toBeDefined()
      handler?.({ data: { novel_id: 2 } })
    })
    expect(
      await screen.findByRole('heading', { name: /详情曲线：novel-02/ }),
    ).toBeInTheDocument()
  })

  it('recovers from an initial discovery status failure via retry without stopping data', async () => {
    let statusReads = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = routeOf(input)
        fetchCalls.push(String(input))
        if (url.pathname === '/api/discovery/status') {
          statusReads += 1
          if (statusReads === 1) {
            throw new TypeError('fake transient network failure')
          }
          return jsonResponse(DISCOVERY_STATUS_IDLE)
        }
        const value = (DEFAULT_ROUTES as Record<string, unknown>)[url.pathname]
        if (value !== undefined) return jsonResponse(value)
        return { ok: false, status: 404, json: async () => ({}) }
      }),
    )
    render(<App />)
    expect(await screen.findByText(/发现状态读取失败/)).toBeInTheDocument()
    // 失败不清空可信数据：候选面板仍然可用
    expect(screen.getByRole('button', { name: '运行发现' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    await waitFor(() => {
      expect(statusReads).toBeGreaterThanOrEqual(2)
    })
    await waitFor(() => {
      expect(screen.queryByText(/发现状态读取失败/)).toBeNull()
    })
  })

  it('polls discovery status while running and refreshes once at terminal', async () => {
    let statusReads = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) => {
        const url = routeOf(input)
        fetchCalls.push(String(input))
        if (url.pathname === '/api/discovery/status') {
          statusReads += 1
          if (statusReads === 1) {
            return jsonResponse({
              ...DISCOVERY_STATUS_IDLE,
              state: 'running',
              current_run: {
                run_key: 'run-1',
                status: 'running',
                started_at: '2026-09-09T00:00:00+00:00',
                error_code: null,
              },
            })
          }
          return jsonResponse({
            ...DISCOVERY_STATUS_IDLE,
            state: 'succeeded',
            current_run: {
              run_key: 'run-1',
              status: 'succeeded',
              started_at: '2026-09-09T00:00:00+00:00',
              error_code: null,
            },
          })
        }
        const value = (DEFAULT_ROUTES as Record<string, unknown>)[url.pathname]
        if (value !== undefined) return jsonResponse(value)
        return { ok: false, status: 404, json: async () => ({}) }
      }),
    )
    render(<App />)
    expect(
      await screen.findByRole('button', { name: '取消发现' }),
    ).toBeInTheDocument()
    const targetsBefore = fetchCalls.filter((url) => url.includes('/api/targets?')).length
    await waitFor(
      () => {
        expect(
          fetchCalls.filter((url) => url.includes('/api/targets?')).length,
        ).toBeGreaterThan(targetsBefore)
      },
      { timeout: 5000 },
    )
    expect(screen.getByRole('button', { name: '运行发现' })).toBeEnabled()
  })

  it('disposes charts on unmount (no resource leak)', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    const { unmount } = render(<App />)
    await screen.findByTestId('discovery-panel')
    openCharts()
    const before = chartState.dispose.mock.calls.length
    unmount()
    expect(chartState.dispose.mock.calls.length).toBeGreaterThan(before)
  })

  it('stops polling after unmount and clears timers', async () => {
    vi.useFakeTimers()
    try {
      let statusReads = 0
      vi.stubGlobal(
        'fetch',
        vi.fn(async (input: RequestInfo | URL) => {
          const url = routeOf(input)
          if (url.pathname === '/api/discovery/status') {
            statusReads += 1
            return jsonResponse({
              ...DISCOVERY_STATUS_IDLE,
              state: 'running',
              current_run: {
                run_key: 'run-mount',
                status: 'running',
                started_at: '2026-09-12T00:00:00+00:00',
                error_code: null,
              },
            })
          }
          const value = (DEFAULT_ROUTES as Record<string, unknown>)[url.pathname]
          if (value !== undefined) return jsonResponse(value)
          return { ok: false, status: 404, json: async () => ({}) }
        }),
      )
      const { unmount } = render(<App />)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000)
      })
      const readsBeforeUnmount = statusReads
      expect(readsBeforeUnmount).toBeGreaterThanOrEqual(1)
      unmount()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(6000)
      })
      expect(statusReads).toBe(readsBeforeUnmount)
    } finally {
      vi.useRealTimers()
    }
  })

  it('pauses idle scheduled polling while hidden and reads once when visible again', async () => {
    vi.useFakeTimers()
    try {
      let statusReads = 0
      vi.stubGlobal(
        'fetch',
        vi.fn(async (input: RequestInfo | URL) => {
          const url = routeOf(input)
          if (url.pathname === '/api/discovery/status') {
            statusReads += 1
            return jsonResponse({
              ...DISCOVERY_STATUS_IDLE,
              schedule: {
                enabled: true,
                tag: 'ウマ娘',
                interval_minutes: 30,
                next_run_at: '2026-09-16T00:00:00+00:00',
                last_attempt_at: null,
                last_error_code: null,
              },
            })
          }
          const value = (DEFAULT_ROUTES as Record<string, unknown>)[url.pathname]
          if (value !== undefined) return jsonResponse(value)
          return { ok: false, status: 404, json: async () => ({}) }
        }),
      )
      render(<App />)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(statusReads).toBe(1)
      // 可见 + 调度开启：idle 每 5 秒只读轮询
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000)
      })
      expect(statusReads).toBe(2)

      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        value: 'hidden',
      })
      document.dispatchEvent(new Event('visibilitychange'))
      const hiddenReads = statusReads
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15000)
      })
      expect(statusReads).toBe(hiddenReads)

      Object.defineProperty(document, 'visibilityState', {
        configurable: true,
        value: 'visible',
      })
      document.dispatchEvent(new Event('visibilitychange'))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(statusReads).toBe(hiddenReads + 1)
    } finally {
      delete (document as unknown as { visibilityState?: string }).visibilityState
      vi.useRealTimers()
    }
  })

  it('does not render NaN or Infinity for missing metrics', async () => {
    mockFetchRoutes(DEFAULT_ROUTES)
    render(<App />)
    openTargetsView()
    await screen.findByText('novel-02')
    const text = document.body.textContent ?? ''
    expect(text).not.toMatch(/NaN|Infinity/)
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })

  it('aborts in-flight requests on rapid filter change without error UI', async () => {
    mockFetchRoutes(DEFAULT_ROUTES, { delayMs: 120 })
    render(<App />)
    openTargetsView()
    await screen.findByRole('row', { name: /novel-01/ })
    fetchSignals.length = 0
    const stateSelect = screen.getByLabelText('状态筛选')
    fireEvent.change(stateSelect, { target: { value: 'track' } })
    fireEvent.change(stateSelect, { target: { value: 'broad' } })
    await waitFor(() => {
      expect(
        fetchSignals.some((s) => s.aborted === true),
      ).toBe(true)
    })
    expect(screen.queryByText(/加载失败/)).toBeNull()
  })

  it('aborts in-flight requests on unmount', async () => {
    mockFetchRoutes(DEFAULT_ROUTES, { delayMs: 120 })
    const { unmount } = render(<App />)
    await waitFor(() => {
      expect(fetchSignals.length).toBeGreaterThan(0)
    })
    unmount()
    await waitFor(() => {
      expect(fetchSignals.some((s) => s.aborted === true)).toBe(true)
    })
  })

  it('sort header click produces no cross-component update warning', async () => {
    const consoleError = vi
      .spyOn(console, 'error')
      .mockImplementation(() => undefined)
    try {
      mockFetchRoutes(DEFAULT_ROUTES)
      render(<App />)
      openTargetsView()
      await screen.findByRole('row', { name: /novel-01/ })
      fetchCalls.length = 0
      fireEvent.click(screen.getByRole('button', { name: /标题/ }))
      await waitFor(() => {
        expect(
          fetchCalls.some(
            (url) => url.includes('sort=title') && url.includes('sort_dir=asc'),
          ),
        ).toBe(true)
      })
      // 点击一次只产生一次排序请求
      const sortCalls = fetchCalls.filter((url) => url.includes('sort=title'))
      expect(sortCalls.length).toBe(1)
      const warning = consoleError.mock.calls.find((args) =>
        String(args[0]).includes('Cannot update a component'),
      )
      expect(warning).toBeUndefined()
    } finally {
      consoleError.mockRestore()
    }
  })
})

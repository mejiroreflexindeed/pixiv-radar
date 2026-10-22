import { useCallback, useEffect, useRef, useState } from 'react'
import {
  DiscoveryRun,
  DiscoveryStatus,
  EventItem,
  HeatmapItem,
  HistoryItem,
  RuntimeInfo,
  ScanRun,
  SortDir,
  Target,
  TargetFilters,
  TargetSort,
  TargetsResponse,
  cancelDiscovery,
  fetchDiscoveryCandidates,
  fetchDiscoveryRuns,
  fetchDiscoveryStatus,
  fetchEvents,
  fetchHealth,
  fetchHeatmap,
  fetchScanRuns,
  fetchTargetDetail,
  fetchTargetHistory,
  fetchTargets,
  startDiscovery,
} from './api'
import AppShell from './components/AppShell'
import ConnectionStatus from './components/ConnectionStatus'
import DiscoveryPanel from './components/DiscoveryPanel'
import EventLog from './components/EventLog'
import PpiRadar from './components/PpiRadar'
import RadarFilters from './components/RadarFilters'
import RunHistory from './components/RunHistory'
import ScanSummaryCards from './components/ScanSummaryCards'
import ScoreHeatmap from './components/ScoreHeatmap'
import SelectedTargetPanel, {
  CandidateReason,
} from './components/SelectedTargetPanel'
import TrackTable from './components/TrackTable'
import { timeZoneLabel } from './format'
import { radarScaleMax } from './radarMapping'

type LoadState = 'loading' | 'error' | 'ready'
type ViewKey = 'discovery' | 'targets' | 'runs'

export const PAGE_SIZE = 50

const EMPTY_FILTERS: TargetFilters = {
  q: '',
  state: null,
  result: null,
  minSignalRatio: null,
}

const VIEW_LABELS: Record<ViewKey, string> = {
  discovery: '发现',
  targets: '我的作品',
  runs: '运行与设置',
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

function App() {
  const [activeView, setActiveView] = useState<ViewKey>('discovery')
  const [filters, setFilters] = useState<TargetFilters>(EMPTY_FILTERS)
  const [sort, setSort] = useState<TargetSort>('time')
  const [sortDir, setSortDir] = useState<SortDir>('desc')
  const [page, setPage] = useState(1)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [selectedTarget, setSelectedTarget] = useState<Target | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState<'not_found' | 'failed' | null>(null)
  const [targets, setTargets] = useState<TargetsResponse | null>(null)
  const [heatmap, setHeatmap] = useState<HeatmapItem[]>([])
  const [runs, setRuns] = useState<ScanRun[]>([])
  const [discoveryRuns, setDiscoveryRuns] = useState<DiscoveryRun[]>([])
  const [events, setEvents] = useState<EventItem[]>([])
  const [history, setHistory] = useState<HistoryItem[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [loadState, setLoadState] = useState<LoadState>('loading')
  const [listError, setListError] = useState<string | null>(null)
  const [heatmapError, setHeatmapError] = useState<string | null>(null)
  const [runsError, setRunsError] = useState<string | null>(null)
  const [targetsVersion, setTargetsVersion] = useState(0)
  const [runsVersion, setRunsVersion] = useState(0)
  const [detailVersion, setDetailVersion] = useState(0)
  const [candidatesVersion, setCandidatesVersion] = useState(0)
  const [chartsOpen, setChartsOpen] = useState(false)
  const [discoveryStatus, setDiscoveryStatus] = useState<DiscoveryStatus | null>(null)
  const [discoveryError, setDiscoveryError] = useState<string | null>(null)
  const [discoveryBusy, setDiscoveryBusy] = useState(false)
  const [statusEpoch, setStatusEpoch] = useState(0)
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null)
  const [candidateReason, setCandidateReason] = useState<CandidateReason>({
    status: 'loading',
    item: null,
  })

  const detailGeneration = useRef(0)
  const listGeneration = useRef(0)
  const heatmapGeneration = useRef(0)
  const runsGeneration = useRef(0)
  const runtimeGeneration = useRef(0)
  const candidateReasonGeneration = useRef(0)
  const lastTerminalRunKey = useRef<string | null>(null)

  const latestDiscoveryRunId = discoveryRuns[0]?.id ?? null

  const refreshTargets = useCallback(() => {
    setTargetsVersion((value) => value + 1)
  }, [])

  // 发现运行结束（含取消/失败）后：刷新 targets/热力图/运行历史/事件/候选与
  // 当前详情+曲线；只做 GET，不重新发现，也不重放任何反馈 POST。
  const handleDiscoveryFinished = useCallback(() => {
    setTargetsVersion((value) => value + 1)
    setRunsVersion((value) => value + 1)
    setCandidatesVersion((value) => value + 1)
    setDetailVersion((value) => value + 1)
  }, [])

  // W05：反馈 POST 已确认后重读候选排序与解释，不重新扫描、不增加 Pixiv 请求。
  const handleFeedbackConfirmed = useCallback(() => {
    setCandidatesVersion((value) => value + 1)
  }, [])

  const onSortChange = useCallback((nextSort: TargetSort, nextDir: SortDir) => {
    setSort(nextSort)
    setSortDir(nextDir)
    setPage(1)
  }, [])

  const onFiltersChange = useCallback((next: TargetFilters) => {
    setFilters(next)
    setPage(1)
  }, [])

  const handleSelect = useCallback((id: number) => {
    setSelectedId(id)
  }, [])

  const handleCloseDetail = useCallback(() => {
    setSelectedId(null)
  }, [])

  // 主目标列表：50 条/页；成功回调也校验世代，取消未遵循 signal 的迟到响应。
  useEffect(() => {
    const generation = ++listGeneration.current
    const controller = new AbortController()
    const signal = controller.signal
    fetchTargets(
      { ...filters, sort, sortDir, limit: PAGE_SIZE, offset: (page - 1) * PAGE_SIZE },
      signal,
    )
      .then((data) => {
        if (generation !== listGeneration.current || signal.aborted) return
        setTargets(data)
        setListError(null)
        setLoadState('ready')
        const lastPage = Math.max(1, Math.ceil(data.total / PAGE_SIZE))
        if (page > lastPage) setPage(lastPage)
      })
      .catch((error: unknown) => {
        if (
          generation !== listGeneration.current ||
          signal.aborted ||
          isAbortError(error)
        ) {
          return
        }
        setListError(String(error))
        setLoadState((current) => (current === 'loading' ? 'error' : current))
      })
    return () => {
      controller.abort()
    }
  }, [filters, sort, sortDir, page, targetsVersion])

  // 热力图：独立请求、独立失败提示（保留最后成功数据）
  useEffect(() => {
    const generation = ++heatmapGeneration.current
    const controller = new AbortController()
    const signal = controller.signal
    fetchHeatmap(filters, signal)
      .then((data) => {
        if (generation !== heatmapGeneration.current || signal.aborted) return
        setHeatmap(data.items)
        setHeatmapError(null)
      })
      .catch((error: unknown) => {
        if (
          generation !== heatmapGeneration.current ||
          signal.aborted ||
          isAbortError(error)
        ) {
          return
        }
        setHeatmapError(String(error))
      })
    return () => {
      controller.abort()
    }
  }, [filters, targetsVersion])

  // 扫描历史、发现历史与事件：首次加载 + 发现完成/手动刷新时更新
  useEffect(() => {
    const generation = ++runsGeneration.current
    let cancelled = false
    Promise.all([fetchScanRuns(), fetchEvents(), fetchDiscoveryRuns()])
      .then(([scanRuns, eventItems, discoveryItems]) => {
        if (cancelled || generation !== runsGeneration.current) return
        setRuns(scanRuns.items)
        setEvents(eventItems.items)
        setDiscoveryRuns(discoveryItems.items)
        setRunsError(null)
      })
      .catch((error: unknown) => {
        if (cancelled || generation !== runsGeneration.current) return
        setRunsError(String(error))
      })
    return () => {
      cancelled = true
    }
  }, [runsVersion])

  // A1：实际运行模式与数据库路径来自进程只读 health，不从内容/token 推断。
  useEffect(() => {
    const generation = ++runtimeGeneration.current
    fetchHealth()
      .then((health) => {
        if (generation !== runtimeGeneration.current) return
        setRuntime(health.runtime ?? null)
      })
      .catch(() => {
        if (generation !== runtimeGeneration.current) return
        setRuntime(null)
      })
    return () => {
      runtimeGeneration.current += 1
    }
  }, [])

  // A5：详情按同 novel_id 读取本轮候选原因；不在本轮时明确说明，不编造理由。
  useEffect(() => {
    if (selectedId === null) {
      candidateReasonGeneration.current += 1
      setCandidateReason({ status: 'loading', item: null })
      return
    }
    if (latestDiscoveryRunId === null) {
      candidateReasonGeneration.current += 1
      setCandidateReason({ status: 'not_in_run', item: null })
      return
    }
    const generation = ++candidateReasonGeneration.current
    const controller = new AbortController()
    setCandidateReason({ status: 'loading', item: null })
    fetchDiscoveryCandidates(
      latestDiscoveryRunId,
      1,
      0,
      selectedId,
      controller.signal,
    )
      .then((response) => {
        if (
          generation !== candidateReasonGeneration.current ||
          controller.signal.aborted
        ) {
          return
        }
        const item = response.items[0] ?? null
        // 响应必须属于当前作品；ID 不符按“不在本轮”处理，绝不显示别人的理由。
        const matches = item !== null && item.novel_id === selectedId
        setCandidateReason(
          matches ? { status: 'loaded', item } : { status: 'not_in_run', item: null },
        )
      })
      .catch((error: unknown) => {
        if (
          generation !== candidateReasonGeneration.current ||
          controller.signal.aborted ||
          isAbortError(error)
        ) {
          return
        }
        setCandidateReason({ status: 'error', item: null })
      })
    return () => {
      controller.abort()
    }
  }, [selectedId, latestDiscoveryRunId, candidatesVersion])

  // 选中目标详情：独立于分页/筛选，通过 GET /api/targets/{id} 获取。
  // A→B 切换立即清空 A 的数据；世代 + AbortSignal 双重保护迟到响应。
  useEffect(() => {
    if (selectedId === null) {
      detailGeneration.current += 1
      setSelectedTarget(null)
      setDetailError(null)
      setDetailLoading(false)
      setHistory([])
      setHistoryLoading(false)
      return
    }
    const generation = ++detailGeneration.current
    const controller = new AbortController()
    const signal = controller.signal
    setSelectedTarget(null)
    setDetailError(null)
    setDetailLoading(true)
    setHistory([])
    fetchTargetDetail(selectedId, signal)
      .then((detail) => {
        if (generation !== detailGeneration.current || signal.aborted) return
        setSelectedTarget(detail)
        setDetailError(null)
      })
      .catch((error: unknown) => {
        if (
          generation !== detailGeneration.current ||
          signal.aborted ||
          isAbortError(error)
        ) {
          return
        }
        setSelectedTarget(null)
        setDetailError(String(error).includes('404') ? 'not_found' : 'failed')
      })
      .finally(() => {
        if (generation === detailGeneration.current && !signal.aborted) {
          setDetailLoading(false)
        }
      })
    return () => {
      controller.abort()
    }
  }, [selectedId, detailVersion])

  // 历史曲线：与详情同世代；A→B 切换清空旧数据，迟到结果不回写
  useEffect(() => {
    if (selectedId === null) {
      setHistory([])
      setHistoryLoading(false)
      return
    }
    const generation = detailGeneration.current
    const controller = new AbortController()
    const signal = controller.signal
    setHistory([])
    setHistoryLoading(true)
    fetchTargetHistory(selectedId, 50, signal)
      .then((data) => {
        if (generation !== detailGeneration.current || signal.aborted) return
        setHistory(data.items)
      })
      .catch((error: unknown) => {
        if (
          generation !== detailGeneration.current ||
          signal.aborted ||
          isAbortError(error)
        ) {
          return
        }
        setHistory([])
      })
      .finally(() => {
        if (generation === detailGeneration.current && !signal.aborted) {
          setHistoryLoading(false)
        }
      })
    return () => {
      controller.abort()
    }
  }, [selectedId, detailVersion])

  // App 是发现状态的唯一轮询所有者：
  // - 运行中每 1 秒；显式调度开启且页面可见时 idle 每 5 秒只读轮询；
  // - 未启用调度的 idle 不长期轮询；恢复可见/焦点时立即读一次；
  // - 单次 GET 失败保留最后可信状态并做有界退避重试，不永久停掉观察。
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    let inFlight = false
    let consecutiveFailures = 0
    let lastTrusted: DiscoveryStatus | null = null
    const MAX_CONSECUTIVE_FAILURES = 6

    const clearTimer = () => {
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
    }

    function scheduleFailureRetry() {
      clearTimer()
      if (cancelled || consecutiveFailures > MAX_CONSECUTIVE_FAILURES) return
      const running = lastTrusted?.state === 'running'
      const base = running ? 1000 : 2000
      const delay = Math.min(base * 2 ** (consecutiveFailures - 1), 5000)
      timer = setTimeout(() => void tick(), delay)
    }

    function scheduleNext(status: DiscoveryStatus) {
      clearTimer()
      if (cancelled) return
      if (status.state === 'running') {
        timer = setTimeout(() => void tick(), 1000)
      } else if (status.schedule?.enabled && document.visibilityState === 'visible') {
        timer = setTimeout(() => void tick(), 5000)
      }
    }

    async function readStatus(): Promise<DiscoveryStatus | null> {
      try {
        const next = await fetchDiscoveryStatus()
        if (!cancelled) {
          setDiscoveryStatus(next)
          setDiscoveryError(null)
        }
        return next
      } catch {
        if (!cancelled) setDiscoveryError('发现状态读取失败；本地数据仍可查看。')
        return null
      }
    }

    async function tick() {
      if (cancelled || inFlight) return
      inFlight = true
      try {
        const status = await readStatus()
        if (cancelled) return
        if (status === null) {
          consecutiveFailures += 1
          scheduleFailureRetry()
          return
        }
        consecutiveFailures = 0
        lastTrusted = status
        setDiscoveryBusy(status.state === 'running')
        const runKey = status.current_run?.run_key ?? null
        if (
          status.state !== 'running' &&
          runKey !== null &&
          lastTerminalRunKey.current !== runKey
        ) {
          lastTerminalRunKey.current = runKey
          handleDiscoveryFinished()
        }
        scheduleNext(status)
      } finally {
        inFlight = false
      }
    }

    const onVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        consecutiveFailures = 0
        void tick()
        return
      }
      // 隐藏：清理空闲计时器（运行中轮询保留，终态感知不中断）；恢复可见立即读一次。
      if (lastTrusted?.state !== 'running') clearTimer()
    }

    void tick()
    document.addEventListener('visibilitychange', onVisibilityChange)
    window.addEventListener('focus', onVisibilityChange)
    return () => {
      cancelled = true
      clearTimer()
      document.removeEventListener('visibilitychange', onVisibilityChange)
      window.removeEventListener('focus', onVisibilityChange)
    }
  }, [statusEpoch, handleDiscoveryFinished])

  const handleStartDiscovery = useCallback(async (tag: string) => {
    setDiscoveryBusy(true)
    setDiscoveryError(null)
    try {
      const started = await startDiscovery(tag)
      setDiscoveryStatus((previous) => ({
        ...(previous ?? { state: 'running', current_run: null }),
        state: 'running',
        source: started.source || previous?.source || 'mock',
      }))
      setStatusEpoch((value) => value + 1)
    } catch (error: unknown) {
      setDiscoveryBusy(false)
      setDiscoveryError(String(error))
    }
  }, [])

  const handleCancelDiscovery = useCallback(async () => {
    setDiscoveryError(null)
    try {
      await cancelDiscovery()
      setStatusEpoch((value) => value + 1)
    } catch {
      setDiscoveryError('取消失败，请重试。')
    }
  }, [])

  const latestScan = targets?.latest_scan ?? null
  const isDemo =
    runtime !== null
      ? runtime.mode === 'demo'
      : targets?.items.some((target) => target.data_source === 'mock') ||
        latestScan?.source === 'mock' ||
        discoveryStatus?.source === 'mock'
  const total = targets?.total ?? 0
  const selectionVisible =
    selectedId !== null &&
    (targets?.items.some((target) => target.novel_id === selectedId) ?? false)

  const detailPanel = (
    <SelectedTargetPanel
      selected={selectedId !== null}
      target={selectedTarget}
      loading={detailLoading}
      error={detailError}
      history={history}
      historyLoading={historyLoading}
      candidateReason={candidateReason}
      onRetry={() => setDetailVersion((value) => value + 1)}
      onClose={handleCloseDetail}
      onFeedbackConfirmed={handleFeedbackConfirmed}
    />
  )

  return (
    <AppShell
      title="Pixiv 小说雷达"
      subtitle="本地单用户 · 只监听回环"
      badge={
        <span className={`badge ${isDemo ? 'badge--warning' : 'badge--info'}`}>
          {isDemo ? '演示模式 · 不连接 Pixiv' : '真实模式'}
        </span>
      }
      nav={(Object.keys(VIEW_LABELS) as ViewKey[]).map((view) => (
        <button
          key={view}
          type="button"
          className={`view-tabs__tab${activeView === view ? ' view-tabs__tab--active' : ''}`}
          aria-current={activeView === view ? 'page' : undefined}
          onClick={() => setActiveView(view)}
        >
          {VIEW_LABELS[view]}
        </button>
      ))}
    >
      <div className="connection-strip">
        <ConnectionStatus />
      </div>
      {loadState === 'loading' && <p aria-busy="true">正在加载数据…</p>}
      {isDemo && activeView !== 'runs' && (
        <div className="demo-banner" role="alert">
          演示数据：当前显示 Mock 扫描结果，非真实 Pixiv 数据。
        </div>
      )}

      {activeView === 'discovery' && (
        <div className="workspace">
          <div className="workspace__main">
            <DiscoveryPanel
              status={discoveryStatus}
              latestRun={discoveryRuns[0] ?? null}
              busy={discoveryBusy}
              error={discoveryError}
              candidatesVersion={candidatesVersion}
              onStart={(tag) => void handleStartDiscovery(tag)}
              onCancel={() => void handleCancelDiscovery()}
              onSelect={handleSelect}
              onRetryStatus={() => setStatusEpoch((value) => value + 1)}
            />
            <div className="charts-toggle">
              <button
                type="button"
                className="button button--secondary"
                aria-expanded={chartsOpen}
                onClick={() => setChartsOpen((value) => !value)}
              >
                {chartsOpen ? '收起雷达与热力图' : '展开雷达与热力图'}
              </button>
              {heatmapError && (
                <span className="error-text">
                  热力图刷新失败（保留上次数据）。
                  <button
                    className="button button--secondary"
                    onClick={refreshTargets}
                  >
                    重试
                  </button>
                </span>
              )}
            </div>
            {chartsOpen && (
              <div className="dashboard-grid">
                <PpiRadar
                  targets={targets?.items ?? []}
                  selectedId={selectedId}
                  onSelect={handleSelect}
                />
                <div>
                  <ScoreHeatmap
                    items={heatmap}
                    scaleMax={radarScaleMax(targets?.items ?? [])}
                    onSelect={handleSelect}
                  />
                </div>
              </div>
            )}
          </div>
          <aside className="workspace__detail">{detailPanel}</aside>
        </div>
      )}

      {activeView === 'targets' && (
        <div className="workspace">
          <div className="workspace__main">
            <ScanSummaryCards
              total={total}
              trackCount={targets?.track_total ?? 0}
              latestScan={latestScan}
              timeZone={timeZoneLabel()}
            />
            <RadarFilters filters={filters} onChange={onFiltersChange} />
            {loadState === 'error' && (
              <p className="error-text">
                目标列表加载失败：{listError}{' '}
                <button className="button button--secondary" onClick={refreshTargets}>
                  重试
                </button>
              </p>
            )}
            {loadState === 'ready' && (
              <>
                {listError && (
                  <p className="error-text">
                    目标列表刷新失败（保留上次数据）：{listError}{' '}
                    <button
                      className="button button--secondary"
                      onClick={refreshTargets}
                    >
                      重试
                    </button>
                  </p>
                )}
                <TrackTable
                  items={targets?.items ?? []}
                  selectedId={selectedId}
                  onSelect={handleSelect}
                  sort={sort}
                  sortDir={sortDir}
                  onSortChange={onSortChange}
                  total={total}
                  page={page}
                  pageSize={PAGE_SIZE}
                  onPageChange={setPage}
                />
                {selectedId !== null && !selectionVisible && (
                  <p className="muted">
                    当前选中的作品不在此筛选结果中；详情仍可查看，翻页或调整筛选不会改变选择。
                  </p>
                )}
              </>
            )}
          </div>
          <aside className="workspace__detail">{detailPanel}</aside>
        </div>
      )}

      {activeView === 'runs' && (
        <div className="runs-view">
          <ScanSummaryCards
            total={total}
            trackCount={targets?.track_total ?? 0}
            latestScan={latestScan}
            timeZone={timeZoneLabel()}
          />
          {runsError && (
            <p className="error-text">
              运行历史读取失败。{' '}
              <button
                className="button button--secondary"
                onClick={() => setRunsVersion((value) => value + 1)}
              >
                重试
              </button>
            </p>
          )}
          <RunHistory runs={runs} />
          <div className="card">
            <h2 className="card__title">发现历史</h2>
            {discoveryRuns.length === 0 ? (
              <p className="muted">暂无发现记录</p>
            ) : (
              <ul className="run-list">
                {discoveryRuns.map((run) => (
                  <li key={run.id}>
                    #{run.id} {run.status} · 执行 {run.executed_requests}/
                    {run.planned_requests} · 候选 {run.unique_candidates}
                    {run.error_code ? ` · ${run.error_code}` : ''} ·{' '}
                    {run.started_at}
                  </li>
                ))}
              </ul>
            )}
          </div>
          <EventLog events={events} />
          <div className="card" data-testid="runtime-card">
            <h2 className="card__title">当前模式与数据</h2>
            <p className="muted">
              模式：
              {runtime === null
                ? '正在读取运行信息…'
                : runtime.mode === 'real'
                  ? '真实模式（只有你在页面显式点击才会访问 Pixiv）'
                  : '演示模式 · 不连接 Pixiv（Mock 数据）'}
            </p>
            <p className="muted">
              数据库：
              {runtime === null
                ? '—'
                : `${runtime.db_path}（${
                    runtime.db_scope === 'formal'
                      ? '正式库'
                      : runtime.db_scope === 'preview'
                        ? '演示预览库'
                        : '自定义库'
                  }）`}
            </p>
            <p className="muted">
              自动发现：
              {runtime !== null && runtime.schedule_minutes > 0
                ? `每 ${runtime.schedule_minutes} 分钟，标签 ${runtime.schedule_tag}（启动先等完整周期）`
                : '关闭（只有你手动点击“运行发现/扫描”）'}
            </p>
            <p className="muted">
              凭据由 Windows 凭据管理器保存，页面与数据库都不保存 token；发现与正文均需你
              显式点击，请求之间保持本地保守间隔。
            </p>
            <p className="muted">
              备份：双击根目录“备份我的数据.cmd”，只读备份到 data\backups\。恢复用
              `\.\backend\.venv\Scripts\python.exe scripts\backup_restore.py restore
              &lt;备份文件&gt; &lt;新目录&gt;`，再用 --db-path 启动新库核对；备份不包含
              账号登录信息。
            </p>
          </div>
          {selectedId !== null && detailPanel}
        </div>
      )}
    </AppShell>
  )
}

export default App

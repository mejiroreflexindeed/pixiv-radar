import { useEffect, useRef, useState } from 'react'
import {
  DiscoveryCandidate,
  DiscoveryGateState,
  DiscoveryRun,
  DiscoveryStatus,
  fetchDiscoveryCandidates,
} from '../api'
import { formatLocalTime } from '../format'

interface Props {
  status: DiscoveryStatus | null
  latestRun: DiscoveryRun | null
  busy: boolean
  error: string | null
  candidatesVersion: number
  onStart: (tag: string) => void
  onCancel: () => void
  onSelect: (id: number) => void
  onRetryStatus?: () => void
}

const STATUS_LABELS: Record<string, string> = {
  running: '运行中',
  succeeded: '成功',
  partial: '部分成功',
  failed: '失败',
  cancelled: '已取消',
}

const ERROR_LABELS: Record<string, string> = {
  rate_limited: '触发限流（已停止）',
  auth_failed: '认证失败（已停止）',
  temporary_failure: '部分来源临时失败',
  malformed_response: '部分来源响应异常',
  cancelled: '已取消',
}

function groupBadge(group: string, label: string) {
  const cls =
    group === 'small_gem'
      ? 'badge badge--success'
      : group === 'popular_reference'
        ? 'badge badge--warning'
        : group === 'stable_track'
          ? 'badge badge--info'
          : 'badge badge--muted'
  return <span className={cls}>{label || group}</span>
}

function statusBadge(status: string) {
  if (status === 'succeeded') return <span className="badge badge--success">{STATUS_LABELS[status]}</span>
  if (status === 'partial') return <span className="badge badge--warning">{STATUS_LABELS[status]}</span>
  if (status === 'failed') return <span className="badge badge--error">{STATUS_LABELS[status]}</span>
  if (status === 'cancelled') return <span className="badge badge--muted">{STATUS_LABELS[status]}</span>
  return <span className="badge badge--info">{STATUS_LABELS[status] ?? status}</span>
}

function sourceSummary(candidate: DiscoveryCandidate): string {
  return candidate.sightings
    .map((sighting) => `${sighting.source_kind}#${sighting.source_rank}`)
    .join('、')
}

function formatSeconds(seconds: number | null): string | null {
  if (seconds === null || !Number.isFinite(seconds) || seconds <= 0) return null
  const whole = Math.ceil(seconds)
  return whole >= 60
    ? `${Math.floor(whole / 60)} 分 ${whole % 60} 秒`
    : `${whole} 秒`
}

function pauseMessage(
  gate: DiscoveryGateState,
  remainingSeconds: number | null,
): string {
  const remaining = formatSeconds(remainingSeconds)
  if (gate.paused_reason === null) {
    return `共享出站处于限流冷却：剩余 ${
      remaining ?? '未知'
    }。到期后不会自动重试；请在“连接状态”显式“使用已保存登录”或重新登录后再运行。`
  }
  if (gate.paused_reason === 'rate_limited') {
    return `共享出站已暂停：触发限流${
      remaining ? `，冷却剩余 ${remaining}` : ''
    }。到期后不会自动重试；请在“连接状态”显式“使用已保存登录”或重新登录后再运行。`
  }
  return '共享出站已暂停：需要重新授权。请在“连接状态”完成登录或“使用已保存登录”后再运行。'
}

function DiscoveryPanel({
  status,
  latestRun,
  busy,
  error,
  candidatesVersion,
  onStart,
  onCancel,
  onSelect,
  onRetryStatus,
}: Props) {
  const [candidates, setCandidates] = useState<DiscoveryCandidate[]>([])
  const [candidateTotal, setCandidateTotal] = useState(0)
  const [candidateTruncated, setCandidateTruncated] = useState(false)
  const [candidateQuery, setCandidateQuery] = useState('')
  const [groupFilter, setGroupFilter] = useState('')
  const [candidatePage, setCandidatePage] = useState(1)
  const [candidateError, setCandidateError] = useState<string | null>(null)
  const [candidatesLoading, setCandidatesLoading] = useState(false)
  const [tag, setTag] = useState('ウマ娘')
  const [tagError, setTagError] = useState<string | null>(null)
  const [reloadNonce, setReloadNonce] = useState(0)
  const [nowTick, setNowTick] = useState(() => Date.now())
  const candidateGeneration = useRef(0)
  const cooldownBase = useRef<{ seconds: number; at: number } | null>(null)

  const running = status?.state === 'running'
  const source = status?.source ?? 'mock'
  const schedule = status?.schedule ?? null
  const latestRunId = latestRun?.id ?? null
  const currentRun = status?.current_run ?? null
  const gate = status?.gate ?? null
  const cooldownProp = gate?.cooldown_remaining_seconds ?? null

  // 冷却倒计时只重渲染本地文本，不发请求；共享门状态仍来自 App 轮询。
  useEffect(() => {
    cooldownBase.current =
      cooldownProp === null ? null : { seconds: cooldownProp, at: Date.now() }
    setNowTick(Date.now())
    if (cooldownProp === null) return
    const timer = window.setInterval(() => setNowTick(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [cooldownProp])

  const cooldownLeft = cooldownBase.current
    ? Math.max(
        0,
        cooldownBase.current.seconds -
          (nowTick - cooldownBase.current.at) / 1000,
      )
    : null
  const showGateBanner =
    Boolean(gate?.paused_reason) || (cooldownLeft ?? 0) > 0

  // 有界读取：最多 2 页 / 200 候选；异常超限只提示截断，不无界抓取。
  // 世代保护：切换 run 或反馈刷新后的迟到响应不得覆盖当前候选。
  useEffect(() => {
    if (latestRunId === null) {
      candidateGeneration.current += 1
      setCandidates([])
      setCandidateTotal(0)
      setCandidateTruncated(false)
      setCandidatePage(1)
      setCandidateError(null)
      setCandidatesLoading(false)
      return
    }
    const generation = ++candidateGeneration.current
    setCandidatesLoading(true)
    setCandidateError(null)
    void (async () => {
      try {
        const first = await fetchDiscoveryCandidates(latestRunId, 100, 0)
        let items = first.items
        if (first.total > 100) {
          const second = await fetchDiscoveryCandidates(latestRunId, 100, 100)
          items = [...items, ...second.items]
        }
        if (generation !== candidateGeneration.current) return
        setCandidates(items.slice(0, 200))
        setCandidateTotal(first.total)
        setCandidateTruncated(first.total > 200)
        setCandidatePage(1)
      } catch {
        if (generation !== candidateGeneration.current) return
        setCandidateError('候选读取失败，请重试。')
      } finally {
        if (generation === candidateGeneration.current) setCandidatesLoading(false)
      }
    })()
  }, [latestRunId, candidatesVersion, reloadNonce])

  function start() {
    if (busy || running) return
    const normalized = tag.trim()
    if (!normalized) {
      setTagError('请输入发现标签')
      return
    }
    setTagError(null)
    onStart(normalized)
  }

  const filteredCandidates = candidates.filter(
    (candidate) =>
      (!candidateQuery ||
        candidate.title.includes(candidateQuery) ||
        candidate.author_name.includes(candidateQuery)) &&
      (!groupFilter || candidate.group === groupFilter),
  )
  const visibleCandidates = filteredCandidates.slice(
    (candidatePage - 1) * 50,
    candidatePage * 50,
  )

  return (
    <div className="card" data-testid="discovery-panel">
      <h2 className="card__title">
        {source === 'pixiv' ? '真实 Pixiv 发现' : '离线发现'}（来源：{source}）
      </h2>
      <div className="discovery-actions">
        <label>
          <span className="muted">标签</span>{' '}
          <input
            aria-label="发现标签"
            value={tag}
            maxLength={100}
            disabled={busy || running}
            onChange={(event) => setTag(event.target.value)}
          />
        </label>
        <button
          className="button button--primary"
          disabled={busy || running}
          onClick={start}
          aria-label="运行发现"
        >
          {running ? '运行中…' : '运行发现/扫描'}
        </button>
        {running && (
          <button
            className="button button--secondary"
            onClick={onCancel}
            aria-label="取消发现"
          >
            取消
          </button>
        )}
      </div>
      {schedule?.enabled && (
        <p className="muted">
          自动发现已启用：标签 {schedule.tag}，每 {schedule.interval_minutes} 分钟；
          下次 {schedule.next_run_at ? formatLocalTime(schedule.next_run_at) : '等待调度'}。
          正在运行时到点会安全跳过，不会并发扫描。
        </p>
      )}
      {tagError && <p className="error-text">{tagError}</p>}
      {error && (
        <p className="error-text">
          {error}
          {onRetryStatus && (
            <button
              className="button button--secondary"
              onClick={onRetryStatus}
            >
              重试
            </button>
          )}
        </p>
      )}
      {showGateBanner && gate && (
        <p className="pause-banner" role="status">
          {pauseMessage(gate, cooldownLeft)}
        </p>
      )}
      {latestRun && (
        <p className="muted">
          最近一次：{statusBadge(latestRun.status)} · 执行 {latestRun.executed_requests}/{latestRun.planned_requests}
          {' '}来源 · 去重候选 {latestRun.unique_candidates}
          {latestRun.error_code && ERROR_LABELS[latestRun.error_code] ? ` · ${ERROR_LABELS[latestRun.error_code]}` : ''}
          {' '}· {formatLocalTime(latestRun.started_at)}
        </p>
      )}
      {running && (
        <p className="muted" aria-live="polite">
          运行中：已完成 {currentRun?.executed_requests ?? 0}/
          {currentRun?.planned_requests ?? '—'} 个来源；可随时取消。
        </p>
      )}
      {running && candidates.length > 0 && (
        <p className="muted">运行中：以下为上一轮候选，结束后自动刷新。</p>
      )}
      {candidateError && (
        <p className="error-text">
          {candidateError}{' '}
          <button
            className="button button--secondary"
            onClick={() => setReloadNonce((value) => value + 1)}
          >
            重试
          </button>
        </p>
      )}
      {candidatesLoading && <p className="muted" aria-busy="true">正在读取候选…</p>}
      {candidates.length > 0 && (
        <div className="table-wrap">
          <div className="filters-row">
            <label className="filters-field">
              <span className="filters-label">候选标题/作者</span>
              <input
                aria-label="候选搜索"
                value={candidateQuery}
                onChange={(event) => {
                  setCandidateQuery(event.target.value)
                  setCandidatePage(1)
                }}
              />
            </label>
            <label className="filters-field">
              <span className="filters-label">分组</span>
              <select
                aria-label="候选分组筛选"
                value={groupFilter}
                onChange={(event) => {
                  setGroupFilter(event.target.value)
                  setCandidatePage(1)
                }}
              >
                <option value="">全部分组</option>
                <option value="small_gem">小众新发现</option>
                <option value="stable_track">稳定追踪</option>
                <option value="popular_reference">热门参考</option>
                <option value="observe">待观察</option>
              </select>
            </label>
            <span className="muted">
              本次共 {candidateTotal} 个候选，当前筛选后 {filteredCandidates.length} 个
              {candidateTruncated && '（超过 200，已按上限截断）'}
            </span>
          </div>
          <table role="table" aria-label="发现候选列表" className="candidate-table">
            <thead>
              <tr>
                <th className="col-id">ID</th>
                <th>标题</th>
                <th>作者</th>
                <th>分组</th>
                <th className="num col-metrics">收藏</th>
                <th className="num col-metrics">阅读</th>
                <th className="col-source">来源</th>
                <th className="col-time">发现时间</th>
              </tr>
            </thead>
            <tbody>
              {visibleCandidates.map((candidate) => (
                <tr
                  key={candidate.novel_id}
                  role="row"
                  tabIndex={0}
                  onClick={() => onSelect(candidate.novel_id)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' || event.key === ' ') {
                      event.preventDefault()
                      onSelect(candidate.novel_id)
                    }
                  }}
                >
                  <td className="col-id">{candidate.novel_id}</td>
                  <td className="title-cell">
                    <span className="title-text">{candidate.title}</span>
                    {candidate.explanations.length > 0 && (
                      <span className="candidate-why">
                        {candidate.explanations.join('；')}
                      </span>
                    )}
                  </td>
                  <td>
                    <span className="cell-clip" title={candidate.author_name || '—'}>
                      {candidate.author_name || '—'}
                    </span>
                  </td>
                  <td className="group-cell">
                    {groupBadge(candidate.group, candidate.group_label)}
                    <span className="candidate-why">{candidate.group_reason}</span>
                  </td>
                  <td className="num col-metrics">{candidate.total_bookmarks}</td>
                  <td className="num col-metrics">{candidate.total_view}</td>
                  <td className="col-source">
                    <span className="cell-clip" title={sourceSummary(candidate)}>
                      {sourceSummary(candidate)}
                    </span>
                  </td>
                  <td className="col-time">
                    {formatLocalTime(candidate.sightings[0]?.discovered_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {filteredCandidates.length > 50 && (
            <div className="pagination">
              <button
                className="button button--secondary"
                disabled={candidatePage <= 1}
                onClick={() => setCandidatePage((value) => Math.max(1, value - 1))}
                aria-label="候选上一页"
              >
                上一页
              </button>
              <span className="muted">
                第 {candidatePage} / {Math.max(1, Math.ceil(filteredCandidates.length / 50))} 页
              </span>
              <button
                className="button button--secondary"
                disabled={candidatePage >= Math.ceil(filteredCandidates.length / 50)}
                onClick={() => setCandidatePage((value) => value + 1)}
                aria-label="候选下一页"
              >
                下一页
              </button>
            </div>
          )}
          <details className="diagnostics">
            <summary>本轮诊断（来源明细与预算）</summary>
            <p className="muted">
              {source === 'pixiv'
                ? '真实模式：同一标签的精确/部分/关键词三次低频只读搜索，每轮最多 3 个来源 × 30 条；不请求正文。'
                : '离线演示：固定 8 个来源（标签/角色/关系词/新作等），硬上限 8 请求 × 30 条，去重后最多 200 候选。'}
              热度不是质量，来源明细只用于核对。
            </p>
            <ul className="plain-list">
              {visibleCandidates.map((candidate) => (
                <li key={candidate.novel_id}>
                  #{candidate.novel_id}：
                  {candidate.sightings
                    .map(
                      (sighting) =>
                        `${sighting.source_kind}#${sighting.source_rank}`,
                    )
                    .join('、') || '—'}
                </li>
              ))}
            </ul>
          </details>
          <p className="muted">
            分组：小众新发现 / 稳定追踪 / 热门参考 / 待观察；“为何出现”已在标题与分组下直接显示，
            选中后详情会给出同一 novel_id 的完整解释。
          </p>
        </div>
      )}
      {!candidatesLoading && candidates.length === 0 && latestRun && !running && (
        latestRun.status === 'succeeded' ? (
          <p className="muted">
            本轮完成：没有发现候选（空结果不是错误）。可更换标签或稍后再试。
          </p>
        ) : latestRun.status === 'cancelled' ? (
          <p className="muted">本轮已取消，没有候选数据。</p>
        ) : (
          <p className="error-text">
            本轮{latestRun.status === 'failed' ? '失败' : '部分失败'}
            {latestRun.error_code && ERROR_LABELS[latestRun.error_code]
              ? `：${ERROR_LABELS[latestRun.error_code]}`
              : ''}
            ，没有可用候选。按上面提示恢复后重试；已成功的历史候选仍保留在运行记录中。
          </p>
        )
      )}
      {!candidatesLoading && candidates.length === 0 && !latestRun && (
        <p className="muted">
          尚未运行发现；点击“运行发现/扫描”开始{source === 'pixiv' ? '低频只读搜索' : '离线演示'}。
        </p>
      )}
    </div>
  )
}

export default DiscoveryPanel

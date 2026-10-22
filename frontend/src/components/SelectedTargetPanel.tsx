import { useEffect, useRef } from 'react'
import { DiscoveryCandidate, HistoryItem, Target } from '../api'
import { RESULT_LABELS, STATE_LABELS, formatRatio } from '../radarMapping'
import ContentAnalysisPanel from './ContentAnalysisPanel'
import FeedbackPanel from './FeedbackPanel'
import TargetHistoryChart from './TargetHistoryChart'

export interface CandidateReason {
  status: 'loading' | 'loaded' | 'not_in_run' | 'error'
  item: DiscoveryCandidate | null
}

interface Props {
  selected: boolean
  target: Target | null
  loading: boolean
  error: 'not_found' | 'failed' | null
  history: HistoryItem[]
  historyLoading: boolean
  candidateReason: CandidateReason
  onRetry: () => void
  onClose: () => void
  onFeedbackConfirmed?: (novelId: number) => void
}

function SelectedTargetPanel({
  selected,
  target,
  loading,
  error,
  history,
  historyLoading,
  candidateReason,
  onRetry,
  onClose,
  onFeedbackConfirmed,
}: Props) {
  const headingRef = useRef<HTMLHeadingElement>(null)

  // 窄屏单列时，选中后把详情标题滚入视野并聚焦，提供“返回列表”操作。
  useEffect(() => {
    if (!selected || !target) return
    if (typeof window.matchMedia !== 'function') return
    if (!window.matchMedia('(max-width: 899px)').matches) return
    headingRef.current?.scrollIntoView({ block: 'start' })
    headingRef.current?.focus()
  }, [selected, target])

  if (!selected) {
    return (
      <div className="card selected-panel" data-testid="selected-target-panel">
        <h2 className="card__title">作品详情</h2>
        <p className="muted">
          从候选列表、目标表格、雷达图或热力图中选择一篇作品；筛选与翻页不会丢失选择。
        </p>
      </div>
    )
  }

  return (
    <div className="selected-panel" data-testid="selected-target-panel">
      {loading && (
        <div className="card">
          <p aria-busy="true">正在加载详情…</p>
        </div>
      )}
      {!loading && error === 'not_found' && (
        <div className="card">
          <p className="error-text">该作品已不在本地库中。</p>
          <div className="detail-actions">
            <button className="button button--secondary" onClick={onClose}>
              返回列表
            </button>
          </div>
        </div>
      )}
      {!loading && error === 'failed' && (
        <div className="card">
          <p className="error-text">详情读取失败。</p>
          <div className="detail-actions">
            <button className="button button--secondary" onClick={onRetry}>
              重试
            </button>
            <button className="button button--secondary" onClick={onClose}>
              返回列表
            </button>
          </div>
        </div>
      )}
      {!loading && !error && target && (
        <>
          <div className="card detail-summary">
            <div className="detail-heading">
              <h2 className="detail-title" ref={headingRef} tabIndex={-1}>
                {target.title}
              </h2>
              <button
                className="button button--secondary"
                onClick={onClose}
                aria-label="返回列表"
              >
                返回列表
              </button>
            </div>
            <p className="muted detail-line">
              {target.author_name || '未知作者'} ·{' '}
              {target.state ? STATE_LABELS[target.state] ?? target.state : '状态未知'} ·{' '}
              {target.latest_result
                ? RESULT_LABELS[target.latest_result] ?? target.latest_result
                : '评估未知'}{' '}
              · 来源 {target.data_source ?? '—'}
              {target.tags && target.tags.length > 0 &&
                ` · 标签：${target.tags
                  .map((tagItem) => tagItem.translated_name || tagItem.name)
                  .join('、')}`}
            </p>
            <p className="detail-line">
              增长信号/门限比：{formatRatio(target.signal_ratio)}
              {target.latest_ewma !== null &&
                target.latest_ewma !== undefined &&
                ` · EWMA ${target.latest_ewma.toFixed(2)} / 门限 ${
                  target.latest_threshold !== null &&
                  target.latest_threshold !== undefined
                    ? target.latest_threshold.toFixed(2)
                    : '—'
                }`}
              {target.latest_reason_codes && target.latest_reason_codes.length > 0 &&
                ` · 规则说明：${target.latest_reason_codes.join('、')}`}
            </p>
            <a
              href={`https://www.pixiv.net/novel/show.php?id=${target.novel_id}`}
              target="_blank"
              rel="noreferrer"
              className="detail-link"
            >
              在 Pixiv 打开同一作品（外链，不自动抓取正文）
            </a>
          </div>

          <div className="card candidate-reason" data-testid="candidate-reason">
            <h2 className="card__title">为何出现在候选</h2>
            {candidateReason.status === 'loading' && (
              <p className="muted" aria-busy="true">
                正在读取本轮候选解释…
              </p>
            )}
            {candidateReason.status === 'loaded' && candidateReason.item && (
              <>
                <p className="detail-line">
                  <span className="badge badge--muted">
                    {candidateReason.item.group_label || candidateReason.item.group}
                  </span>{' '}
                  {candidateReason.item.group_reason || '本轮候选'}
                </p>
                {candidateReason.item.explanations.length > 0 && (
                  <p className="muted detail-line">
                    {candidateReason.item.explanations.join('；')}
                  </p>
                )}
                <p className="muted detail-line">
                  来源：
                  {candidateReason.item.sightings
                    .map(
                      (sighting) =>
                        `${sighting.source_kind}#${sighting.source_rank}`,
                    )
                    .join('、')}
                </p>
              </>
            )}
            {candidateReason.status === 'not_in_run' && (
              <p className="muted detail-line">
                该作品不在本轮候选列表中，暂无本次解释；上方的状态与指标仍来自真实历史数据。
              </p>
            )}
            {candidateReason.status === 'error' && (
              <p className="muted detail-line">
                候选解释读取失败；不影响详情、反馈与外链。
              </p>
            )}
          </div>

          <FeedbackPanel
            novelId={target.novel_id}
            onConfirmed={onFeedbackConfirmed}
          />
          <TargetHistoryChart
            novelId={target.novel_id}
            title={target.title}
            history={history}
            loading={historyLoading}
          />
          <ContentAnalysisPanel novelId={target.novel_id} targetState={target.state} />
        </>
      )}
    </div>
  )
}

export default SelectedTargetPanel

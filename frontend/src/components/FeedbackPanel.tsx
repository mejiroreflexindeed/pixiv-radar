import { useEffect, useRef, useState } from 'react'
import {
  FeedbackEvent,
  FeedbackSummary,
  FeedbackTag,
  fetchPreferenceTags,
  fetchTargetFeedback,
  submitFeedback,
} from '../api'
import { formatLocalTime } from '../format'

interface Props {
  novelId: number
  onConfirmed?: (novelId: number) => void
}

const PAGE_SIZE = 5

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

function feedbackLabel(value: FeedbackSummary['effective_feedback']): string {
  if (value === 'good') return '好文'
  if (value === 'exclude') return '排除'
  return '暂无明确反馈'
}

function eventLabel(value: string): string {
  const labels: Record<string, string> = {
    good: '标记好文',
    exclude: '标记排除',
    undo: '撤销反馈',
    exposure: '曝光（不代表喜欢）',
    click: '点击弱信号',
    dwell: '停留弱信号',
  }
  return labels[value] ?? value
}

function tagTone(tag: FeedbackTag): 'positive' | 'negative' | 'neutral' {
  if (tag.positive_count > 0 && tag.negative_count === 0) return 'positive'
  if (tag.negative_count > 0 && tag.positive_count === 0) return 'negative'
  return 'neutral'
}

function tagDetails(tag: FeedbackTag): string {
  const parts: string[] = []
  if (tag.positive_count > 0) parts.push(`正向 ${tag.positive_count}`)
  if (tag.negative_count > 0) parts.push(`负向 ${tag.negative_count}`)
  if (tag.weak_signal_count > 0) parts.push(`弱信号 ${tag.weak_signal_count}`)
  return `${parts.join('；')}；权重 ${tag.score.toFixed(2)}`
}

function FeedbackPanel({ novelId, onConfirmed }: Props) {
  const [summary, setSummary] = useState<FeedbackSummary | null>(null)
  const [events, setEvents] = useState<FeedbackEvent[] | null>(null)
  const [tags, setTags] = useState<FeedbackTag[]>([])
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const generation = useRef(0)
  const readController = useRef<AbortController | null>(null)
  // 每次重读第一页都建立新数据集，旧分页完成（包括 finally）必须丢弃。
  const pageEpoch = useRef(0)
  const moreController = useRef<AbortController | null>(null)

  function isCurrent(requestGeneration: number): boolean {
    return generation.current === requestGeneration
  }

  function resetPages() {
    pageEpoch.current += 1
    moreController.current?.abort()
    moreController.current = null
    setLoadingMore(false)
    setEvents(null)
    setHasMore(false)
  }

  async function load(targetId: number, requestGeneration: number) {
    resetPages()
    readController.current?.abort()
    const controller = new AbortController()
    readController.current = controller
    setLoading(true)
    setError(null)
    try {
      const [feedback, preferenceTags] = await Promise.all([
        fetchTargetFeedback(targetId, PAGE_SIZE, 0, controller.signal),
        fetchPreferenceTags(8, controller.signal),
      ])
      if (controller.signal.aborted || !isCurrent(requestGeneration) || feedback.summary.novel_id !== targetId) return
      setSummary(feedback.summary)
      setEvents(feedback.events)
      setHasMore(feedback.events.length >= PAGE_SIZE)
      setTags(preferenceTags.items)
    } catch (caught) {
      if (controller.signal.aborted || !isCurrent(requestGeneration) || isAbortError(caught)) return
      setError('反馈暂时不可用。')
    } finally {
      if (!controller.signal.aborted && isCurrent(requestGeneration)) setLoading(false)
    }
  }

  useEffect(() => {
    const requestGeneration = ++generation.current
    setSummary(null)
    setTags([])
    setBusy(false)
    setLoading(true)
    setError(null)
    void load(novelId, requestGeneration)
    return () => {
      readController.current?.abort()
      moreController.current?.abort()
    }
  }, [novelId])

  async function refreshAfterSubmit(
    targetId: number,
    requestGeneration: number,
  ): Promise<void> {
    resetPages()
    readController.current?.abort()
    const controller = new AbortController()
    readController.current = controller
    const [refreshed, refreshedTags] = await Promise.all([
      fetchTargetFeedback(targetId, PAGE_SIZE, 0, controller.signal),
      fetchPreferenceTags(8, controller.signal),
    ])
    if (controller.signal.aborted || !isCurrent(requestGeneration) || refreshed.summary.novel_id !== targetId) return
    setEvents(refreshed.events)
    setHasMore(refreshed.events.length >= PAGE_SIZE)
    setTags(refreshedTags.items)
  }

  async function send(eventType: 'good' | 'exclude' | 'undo') {
    if (
      !summary ||
      busy ||
      summary.novel_id !== novelId ||
      (eventType === 'undo' && summary.active_feedback_event_id === null)
    ) return
    const requestGeneration = generation.current
    const targetId = novelId
    setBusy(true)
    setError(null)
    try {
      // 不用读取 controller 取消 POST：服务端可能已经提交，取消只会制造未知结果。
      const result = await submitFeedback(
        targetId,
        eventType,
        eventType === 'undo' ? summary.active_feedback_event_id : null,
      )
      if (!isCurrent(requestGeneration) || result.summary.novel_id !== targetId) return
      setSummary(result.summary)
      // POST 已确认：先通知外层重读候选排序/解释，再刷新本面板；后续读取失败
      // 不得回滚已确认的反馈，也不得再次 POST。
      onConfirmed?.(result.summary.novel_id)
      try {
        await refreshAfterSubmit(targetId, requestGeneration)
      } catch (caught) {
        if (!isCurrent(requestGeneration) || isAbortError(caught)) return
        setError('已保存，刷新失败，请重试读取')
      }
    } catch {
      if (isCurrent(requestGeneration)) {
        setError('保存结果未确认，请先刷新查看')
      }
    } finally {
      if (isCurrent(requestGeneration)) setBusy(false)
    }
  }

  async function loadMore() {
    if (!events || loading || busy || loadingMore || !hasMore) return
    const requestGeneration = generation.current
    const epoch = pageEpoch.current
    const targetId = novelId
    const offset = events.length
    const controller = new AbortController()
    moreController.current?.abort()
    moreController.current = controller
    setLoadingMore(true)
    try {
      const page = await fetchTargetFeedback(
        targetId,
        PAGE_SIZE,
        offset,
        controller.signal,
      )
      if (
        controller.signal.aborted ||
        !isCurrent(requestGeneration) ||
        pageEpoch.current !== epoch ||
        page.summary.novel_id !== targetId
      ) {
        return
      }
      setEvents((current) => {
        const existing = new Set((current ?? []).map((event) => event.id))
        return [...(current ?? []), ...page.events.filter((event) => !existing.has(event.id))]
      })
      setHasMore(page.events.length >= PAGE_SIZE)
    } catch (caught) {
      if (
        controller.signal.aborted ||
        !isCurrent(requestGeneration) ||
        pageEpoch.current !== epoch ||
        isAbortError(caught)
      ) {
        return
      }
      setError('审计记录加载失败，请重试读取')
    } finally {
      if (!controller.signal.aborted && isCurrent(requestGeneration) && pageEpoch.current === epoch) {
        setLoadingMore(false)
        if (moreController.current === controller) {
          moreController.current = null
        }
      }
    }
  }

  return (
    <div className="card" data-testid="feedback-panel" aria-live="polite">
      <h2 className="card__title">个人反馈</h2>
      {loading && <p className="muted" aria-busy="true">正在加载反馈…</p>}
      {!loading && error && !summary && (
        <>
          <p className="error-text">{error}</p>
          <button className="button button--secondary" onClick={() => void load(novelId, generation.current)}>
            重试读取
          </button>
        </>
      )}
      {!loading && summary && (
        <>
          <p className="feedback-summary">
            当前标记：<strong>{feedbackLabel(summary.effective_feedback)}</strong>；
            累计反馈权重（不是作品质量分） {summary.score.toFixed(2)}；
            显式 {summary.explicit_score.toFixed(2)}；弱信号 {summary.weak_signal_score.toFixed(2)}；
            评分版本 {summary.scoring_version}
          </p>
          <div className="feedback-actions">
            <button
              className="button button--primary"
              disabled={busy}
              onClick={() => void send('good')}
            >
              标记好文
            </button>
            <button
              className="button button--danger"
              disabled={busy}
              onClick={() => void send('exclude')}
            >
              标记排除
            </button>
            <button
              className="button button--secondary"
              disabled={busy || summary.active_feedback_event_id === null}
              onClick={() => void send('undo')}
            >
              撤销当前反馈
            </button>
          </div>
          {error && <p className="error-text">{error}</p>}
          {error && <button className="button button--secondary" onClick={() => void load(novelId, generation.current)}>重试读取</button>}
          {tags.length > 0 && (
            <div className="feedback-tags" aria-label="偏好标签">
              <p className="muted">偏好标签（基于反馈事件）</p>
              {(['positive', 'negative', 'neutral'] as const).map((tone) => {
                const group = tags.filter((tag) => tagTone(tag) === tone)
                if (group.length === 0) return null
                const label = tone === 'positive' ? '正向' : tone === 'negative' ? '负向' : '中性/弱信号'
                return (
                  <p className="muted" key={tone} data-testid={`feedback-tags-${tone}`}>
                    {label}：{group.map((tag) => `${tag.translated_name ?? tag.tag}（${tagDetails(tag)}）`).join('、')}
                  </p>
                )
              })}
            </div>
          )}
          {events && events.length > 0 && (
            <>
              <ul className="feedback-events" aria-label="反馈审计记录">
                {events.map((event) => (
                  <li key={String(event.id)}>
                    {eventLabel(event.event_type)} · {formatLocalTime(event.created_at)}
                    {event.is_reverted && ' · 已撤销'}
                  </li>
                ))}
              </ul>
              {hasMore && (
                <button className="button button--secondary" disabled={loadingMore || busy} onClick={() => void loadMore()}>
                  {loadingMore ? '正在加载…' : '加载更多审计记录'}
                </button>
              )}
            </>
          )}
        </>
      )}
    </div>
  )
}

export default FeedbackPanel

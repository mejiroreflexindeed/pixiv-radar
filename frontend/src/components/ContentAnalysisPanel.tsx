import { useEffect, useRef, useState } from 'react'
import {
  ContentAnalysis,
  SimilarContent,
  analyzeContent,
  fetchContentAnalysis,
  fetchContentStatus,
  fetchSimilarContent,
} from '../api'
import { formatLocalTime } from '../format'

interface Props {
  novelId: number
  targetState: string | null
}

const REASON_LABELS: Record<string, string> = {
  text_too_short: '正文过短',
  image_dominant: '图片标记占主导',
  dialogue_dominant: '对话占比过高',
  repeated_paragraphs: '重复段落较多',
  promotion_markers: '宣传词较多',
}

const ERROR_LABELS: Record<string, string> = {
  pixiv_login_required: '需要先在上方完成 Pixiv 登录。',
  pixiv_rate_limited: 'Pixiv 暂时限流，请稍后再试。',
  pixiv_content_not_found: 'Pixiv 上已无法读取该正文。',
  pixiv_content_unavailable: '正文暂时无法读取，请稍后重试。',
  content_analysis_unavailable: '当前运行模式未启用真实正文分析。',
  target_not_in_track: '只有追尾目标才会请求正文。',
  target_not_found: '该作品不在本地库中。',
  pixiv_operation_paused:
    '共享出站已暂停（认证失败或限流）：请在“连接状态”重新登录或“使用已保存登录”后再试。',
  pixiv_session_invalid:
    '登录会话已失效：请在“连接状态”重新登录或“使用已保存登录”后再试。',
  content_id_mismatch: '返回的正文与当前作品不一致，已丢弃。',
  content_invalid: '正文为空或超出大小限制，未保存分析。',
}

function readableError(error: unknown): string {
  const code = error instanceof Error ? error.message : String(error)
  return ERROR_LABELS[code] ?? '分析失败，请稍后重试。'
}

function isCacheMissing(error: unknown): boolean {
  return (
    error instanceof Error &&
    (error.message === 'analysis_not_found' || error.message === 'HTTP 404')
  )
}

type LoadState = 'loading' | 'ready' | 'error'
type CacheState = 'loading' | 'empty' | 'ready' | 'error'

function ContentAnalysisPanel({ novelId, targetState }: Props) {
  const [available, setAvailable] = useState<boolean | null>(null)
  const [statusState, setStatusState] = useState<LoadState>('loading')
  const [cacheState, setCacheState] = useState<CacheState>('loading')
  const [cacheError, setCacheError] = useState<string | null>(null)
  const [analysis, setAnalysis] = useState<ContentAnalysis | null>(null)
  const [busy, setBusy] = useState(false)
  const [postError, setPostError] = useState<string | null>(null)
  const [pendingConfirm, setPendingConfirm] = useState(false)
  const [similar, setSimilar] = useState<SimilarContent[]>([])
  const [reloadNonce, setReloadNonce] = useState(0)
  const generation = useRef(0)
  const lastLoadedId = useRef<number | null>(null)

  // R10/R15：能力读取与缓存读取分开；缓存暂错保留手动只读重试，绝不自动 POST。
  // 作品切换才重置全部状态；“同作品只读核对”（确认/重试）在结果可信前保留最后
  // 可信分析、待确认标记与 POST 锁。
  useEffect(() => {
    const current = ++generation.current
    const controller = new AbortController()
    const targetChanged = lastLoadedId.current !== novelId
    lastLoadedId.current = novelId
    if (targetChanged) {
      setAnalysis(null)
      setAvailable(null)
      setStatusState('loading')
      setCacheState('loading')
      setCacheError(null)
      setPostError(null)
      setPendingConfirm(false)
      setSimilar([])
      setBusy(false)
    } else {
      // 同一作品的核对：保留 analysis/pendingConfirm/postError，只标记读取中。
      setStatusState('loading')
      setCacheState('loading')
      setCacheError(null)
    }

    fetchContentStatus(controller.signal)
      .then((status) => {
        if (controller.signal.aborted || generation.current !== current) return
        setAvailable(status.available)
        setStatusState('ready')
      })
      .catch(() => {
        if (controller.signal.aborted || generation.current !== current) return
        setStatusState('error')
      })

    fetchContentAnalysis(novelId, controller.signal)
      .then((stored) => {
        if (controller.signal.aborted || generation.current !== current) return
        setAnalysis(stored)
        setCacheState('ready')
        // 可信核对完成：待确认解除，之前的抓取失败提示一并清除。
        setPendingConfirm(false)
        setPostError(null)
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted || generation.current !== current) return
        if (isCacheMissing(caught)) {
          // 404 是确定答案：服务端没有分析，待确认不再成立；缓存按空态处理。
          setAnalysis(null)
          setPendingConfirm(false)
          setCacheState('empty')
          return
        }
        // 503/网络暂错不能解除不确定结果：保留可信缓存与待确认、POST 锁。
        setCacheState('error')
        setCacheError(readableError(caught))
      })

    return () => controller.abort()
  }, [novelId, reloadNonce])

  function retryRead() {
    // 只重读能力与缓存（GET）；不会触发 analyze/force 或上游抓取。
    // R16：POST 进行中禁止重读——世代递增会丢弃在途分析结果并让按钮卡在“正在分析…”。
    if (busy) return
    setReloadNonce((value) => value + 1)
  }

  useEffect(() => {
    if (!analysis) {
      setSimilar([])
      return
    }
    const current = generation.current
    const controller = new AbortController()
    fetchSimilarContent(novelId, 5, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted && generation.current === current) {
          setSimilar(result.items)
        }
      })
      .catch(() => {
        // 相似列表是附加的本地能力，失败不覆盖主分析结果。
      })
    return () => controller.abort()
  }, [analysis, novelId])

  async function run() {
    // 待确认期间禁止再次 force：不只看按钮外观，事件入口同样拦截。
    if (busy || pendingConfirm || targetState !== 'track' || !available) return
    const current = generation.current
    const targetId = novelId
    setBusy(true)
    setPostError(null)
    try {
      // POST 可能已抓取并提交，切换目标时不主动中断，只丢弃旧响应。
      const result = await analyzeContent(targetId, analysis !== null)
      if (generation.current !== current || result.novel_id !== targetId) return
      setAnalysis(result)
      setCacheState('ready')
      setPendingConfirm(false)
    } catch (caught) {
      if (generation.current !== current) return
      setPostError(readableError(caught))
      // 结果未知：保留已确认缓存，先只读核对，确认前禁止再次抓取。
      setPendingConfirm(true)
    } finally {
      if (generation.current === current) setBusy(false)
    }
  }

  const eligible = targetState === 'track'
  const loading = statusState === 'loading' || cacheState === 'loading'
  return (
    <section className="card" data-testid="content-analysis-panel" aria-live="polite">
      <h2 className="card__title">正文轻量分析</h2>
      {loading && <p className="muted" aria-busy="true">正在读取本地分析缓存…</p>}
      {!loading && !eligible && (
        <p className="muted">该目标尚未进入追尾队列，因此不会请求正文。</p>
      )}
      {!loading && eligible && statusState === 'error' && (
        <p className="error-text">
          无法读取正文分析能力状态。{' '}
          <button
            className="button button--secondary"
            disabled={busy}
            onClick={retryRead}
          >
            重试读取
          </button>
        </p>
      )}
      {!loading && eligible && statusState === 'ready' && available === false && (
        <p className="muted">演示模式不请求正文；以 real 模式启动后，可由你手动触发。</p>
      )}
      {!loading && eligible && available === true && cacheState === 'error' && (
        <p className="error-text">
          {cacheError ?? '正文暂时无法读取，请稍后重试。'}{' '}
          <button
            className="button button--secondary"
            disabled={busy}
            onClick={retryRead}
          >
            重试本地读取
          </button>
        </p>
      )}
      {!loading && eligible && available === true && cacheState === 'empty' && (
        <p className="muted">尚未分析。点击后只请求这一篇正文，不会批量抓取。</p>
      )}
      {analysis && (
        <>
          <dl className="content-features">
            <div><dt>语言</dt><dd>{analysis.language}</dd></div>
            <div><dt>有效字符</dt><dd>{analysis.char_count.toLocaleString()}</dd></div>
            <div><dt>段落</dt><dd>{analysis.paragraph_count}</dd></div>
            <div><dt>对话占比</dt><dd>{(analysis.dialogue_ratio * 100).toFixed(1)}%</dd></div>
            <div><dt>图片标记</dt><dd>{analysis.image_marker_count}</dd></div>
            <div><dt>结构特征分</dt><dd>{analysis.structure_score.toFixed(1)} / 100</dd></div>
          </dl>
          <p className="muted">
            规则判断：{analysis.is_clutter ? '可能是内容杂波' : '未命中杂波规则'}；
            {analysis.is_suspect ? '命中可疑模式' : '未命中可疑模式'}。
            这不是文学质量评分。
          </p>
          {analysis.reason_codes.length > 0 && (
            <p className="muted">
              原因：{analysis.reason_codes.map((code) => REASON_LABELS[code] ?? code).join('、')}
            </p>
          )}
          {Object.keys(analysis.keyword_hits).length > 0 && (
            <p className="muted">
              关键词：{Object.entries(analysis.keyword_hits).map(([word, count]) => `${word}×${count}`).join('、')}
            </p>
          )}
          <p className="muted">分析时间：{formatLocalTime(analysis.analyzed_at)}；版本 {analysis.analyzer_version}</p>
          {similar.length > 0 && (
            <div className="content-similar">
              <p className="muted">本地 TF-IDF 文本相似（不是质量或语义判断）：</p>
              <ul>
                {similar.map((item) => (
                  <li key={item.novel_id}>
                    <a
                      href={`https://www.pixiv.net/novel/show.php?id=${item.novel_id}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {item.title}
                    </a>
                    {' '}· {(item.similarity * 100).toFixed(1)}%
                    {(item.is_clutter || item.is_suspect) && ' · 有规则警示'}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
      {postError && <p className="error-text">{postError}</p>}
      {pendingConfirm && (
        <>
          <p className="muted">
            抓取结果未确认（响应中断，本地可能已保存新结果）。确认前不会再次抓取，
            可先只读核对本地结果。
          </p>
          <button
            className="button button--secondary"
            disabled={busy}
            onClick={retryRead}
          >
            确认本地结果
          </button>
        </>
      )}
      {!loading && eligible && available && (
        <button
          className="button button--secondary"
          disabled={busy || pendingConfirm}
          onClick={() => void run()}
        >
          {busy ? '正在分析…' : analysis ? '重新抓取并分析' : '分析这篇正文'}
        </button>
      )}
    </section>
  )
}

export default ContentAnalysisPanel

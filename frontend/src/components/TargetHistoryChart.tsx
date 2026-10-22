import { useEffect, useRef } from 'react'
import { HistoryItem } from '../api'
import { ChartLike, initChart } from '../echartsSetup'
import { buildHistoryOption } from '../radarMapping'
import { formatLocalTime } from '../format'

interface Props {
  novelId: number
  title: string
  history: HistoryItem[]
  loading: boolean
}

function TargetHistoryChart({ novelId, title, history, loading }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ChartLike | null>(null)

  useEffect(() => {
    const element = ref.current
    if (!element) return
    const chart = initChart(element)
    chartRef.current = chart
    const onResize = () => chart.resize()
    window.addEventListener('resize', onResize)
    const observer =
      typeof ResizeObserver === 'function'
        ? new ResizeObserver(() => chart.resize())
        : null
    observer?.observe(element)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', onResize)
      chart.dispose()
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    if (chartRef.current) {
      chartRef.current.setOption(buildHistoryOption(history))
    }
  }, [history])

  return (
    <div className="card">
      <h2 className="card__title">
        详情曲线：{title}{' '}
        <a
          href={`https://www.pixiv.net/novel/show.php?id=${novelId}`}
          target="_blank"
          rel="noreferrer"
          className="muted"
          aria-label="在 Pixiv 打开（外部链接，不自动抓取正文）"
        >
          Pixiv 外链 ↗
        </a>
      </h2>
      {loading && <p aria-busy="true">正在加载历史…</p>}
      {!loading && history.length === 0 && <p className="muted">暂无历史数据。</p>}
      <div
        ref={ref}
        className="chart chart--history"
        role="img"
        aria-label="EWMA 与门限详情曲线；缺失点留空"
        data-testid="history-chart"
      />
      {!loading && history.length > 0 && (
        <p className="muted">
          最近 {history.length} 次扫描；缺失点留空（不伪造为 0）。最新一次：
          {formatLocalTime(history[history.length - 1].captured_at)}。
        </p>
      )}
    </div>
  )
}

export default TargetHistoryChart

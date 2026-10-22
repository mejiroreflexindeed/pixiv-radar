import { useEffect, useRef } from 'react'
import { HeatmapItem } from '../api'
import { ChartLike, initChart } from '../echartsSetup'
import { buildHeatmapOption, RESULT_LABELS } from '../radarMapping'

interface Props {
  items: HeatmapItem[]
  scaleMax: number
  onSelect: (id: number) => void
}

function ScoreHeatmap({ items, scaleMax, onSelect }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ChartLike | null>(null)
  const onSelectRef = useRef(onSelect)
  onSelectRef.current = onSelect

  useEffect(() => {
    const element = ref.current
    if (!element) return
    const chart = initChart(element)
    chartRef.current = chart
    chart.on('click', (params) => {
      const data = (params as { data?: { novel_id?: number } }).data
      if (data && typeof data.novel_id === 'number') {
        onSelectRef.current(data.novel_id)
      }
    })
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
    chartRef.current?.setOption(buildHeatmapOption(items, scaleMax))
  }, [items, scaleMax])

  return (
    <div className="card">
      <h2 className="card__title">七次扫描信号热力图</h2>
      <div
        ref={ref}
        className="chart chart--heatmap"
        role="img"
        aria-label="七次扫描信号热力图：每行一个目标，最近 7 次扫描"
        data-testid="score-heatmap"
      />
      <p className="muted">
        每行一个目标（最多 12 行），横向最近 7 次扫描；灰色 = 无指标。点击行可选择目标。
        颜色和半径按当前结果集稳健对数尺度展示（增长信号/门限比）；图例：
        {Object.entries(RESULT_LABELS)
          .map(([k, v]) => `${v}(${k})`)
          .join('、')}。
      </p>
    </div>
  )
}

export default ScoreHeatmap

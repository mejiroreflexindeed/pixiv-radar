import { useEffect, useRef } from 'react'
import { Target } from '../api'
import { ChartLike, initChart } from '../echartsSetup'
import { buildRadarOption, STATE_COLORS, STATE_LABELS } from '../radarMapping'

interface Props {
  targets: Target[]
  selectedId: number | null
  onSelect: (id: number) => void
}

function PpiRadar({ targets, selectedId, onSelect }: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const chartRef = useRef<ChartLike | null>(null)
  const onSelectRef = useRef(onSelect)
  onSelectRef.current = onSelect

  useEffect(() => {
    const element = ref.current
    if (!element) return
    const chart = initChart(element)
    chartRef.current = chart
    // A narrow DOM handle lets the offline UI check inspect the real ECharts
    // instance (pixels and rendered display list), without making it global.
    ;(element as HTMLDivElement & { __radarChart?: ChartLike }).__radarChart = chart
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
      delete (element as HTMLDivElement & { __radarChart?: ChartLike }).__radarChart
      chartRef.current = null
    }
  }, [])

  useEffect(() => {
    chartRef.current?.setOption(buildRadarOption(targets, selectedId))
  }, [targets, selectedId])

  return (
    <div className="card">
      <h2 className="card__title">PPI 雷达</h2>
      <div
        ref={ref}
        className="chart chart--radar"
        role="img"
        aria-label="PPI 雷达图：状态分区、信号越强越靠近圆心"
        data-testid="ppi-radar"
      />
      <div className="chart-legend" aria-hidden="true">
        {(Object.keys(STATE_COLORS) as (keyof typeof STATE_COLORS)[]).map((state) => (
          <span key={state} className="legend-item">
            <span
              className="legend-dot"
              style={{ background: STATE_COLORS[state] }}
            />
            {STATE_LABELS[state] ?? state}
          </span>
        ))}
        <span className="legend-item muted">
          外圈三角=信号比不可用
        </span>
      </div>
      <p className="muted">
        颜色和半径按当前结果集稳健对数尺度展示（增长信号/门限比），越强越靠近圆心。
      </p>
    </div>
  )
}

export default PpiRadar

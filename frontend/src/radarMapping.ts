/** 阶段 E 显示层纯函数：signal ratio、PPI 雷达映射、热力图与曲线 option 构建。

契约见 docs/PHASE_E_R1_SEARCH_1_CONTRACT.md §1/§2（R1 修订优先于
PHASE_E_ALPHA_CONTRACT 的 [0,2] 截断规则）。全部确定性、无副作用。
*/

import { HeatmapItem, HistoryItem, Target } from './api'
import { formatLocalTime } from './format'

export const STATE_LABELS: Record<string, string> = {
  cold_start: '冷启动',
  broad: '广域',
  track: '追尾',
}

export const STATE_COLORS: Record<string, string> = {
  cold_start: '#94a3b8',
  broad: '#2563eb',
  track: '#d97706',
}

export const RESULT_LABELS: Record<string, string> = {
  qualified: '合格',
  not_qualified: '不合格',
  unknown: '未知',
}

export const RATIO_EPSILON = 1e-9
export const RADAR_RADIUS_INNER = 12
export const RADAR_RADIUS_OUTER = 92
export const RADAR_RADIUS_UNAVAILABLE = 96

/** 显示层信号比：max(ewma,0)/max(threshold,ε)；任一缺失/非法 → null。 */
export function displayRatio(
  ewma: number | null | undefined,
  threshold: number | null | undefined,
): number | null {
  if (ewma === null || ewma === undefined || threshold === null || threshold === undefined) {
    return null
  }
  if (!Number.isFinite(ewma) || !Number.isFinite(threshold) || threshold <= 0) {
    return null
  }
  return Math.max(ewma, 0) / Math.max(threshold, RATIO_EPSILON)
}

/** 确定性 percentile95（线性插值）；空数组返回 null。 */
export function percentile95(values: number[]): number | null {
  if (values.length === 0) return null
  const sorted = [...values].sort((a, b) => a - b)
  const rank = (sorted.length - 1) * 0.95
  const lo = Math.floor(rank)
  const hi = Math.ceil(rank)
  if (lo === hi) return sorted[lo]
  return sorted[lo] + (rank - lo) * (sorted[hi] - sorted[lo])
}

/** 当前结果集的稳健尺度上限：max(2, p95)。 */
export function buildScaleMax(ratios: (number | null)[]): number {
  const finite = ratios.filter(
    (r): r is number => r !== null && Number.isFinite(r) && r >= 0,
  )
  const p95 = percentile95(finite)
  return Math.max(2, p95 ?? 2)
}

/** 稳健对数强度：clamp(log1p(ratio)/log1p(scale_max), 0, 1)。 */
export function visualStrength(ratio: number, scaleMax: number): number {
  if (!Number.isFinite(ratio) || ratio < 0) return 0
  const denominator = Math.log1p(Math.max(scaleMax, 2))
  return Math.min(Math.max(Math.log1p(ratio) / denominator, 0), 1)
}

/** 雷达半径：12 + 80*(1-strength)；unavailable 固定 96。 */
export function radarRadius(strength: number | null): number {
  if (strength === null) return RADAR_RADIUS_UNAVAILABLE
  return RADAR_RADIUS_INNER + (RADAR_RADIUS_OUTER - RADAR_RADIUS_INNER) * (1 - strength)
}

export function formatRatio(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return '—'
  return ratio.toFixed(2)
}

/** 统一 HTML 转义：处理 & < > " '（tooltip formatter 返回 ECharts HTML）。 */
export function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

const SECTORS: Record<string, { start: number; span: number }> = {
  cold_start: { start: 0, span: 120 },
  broad: { start: 120, span: 120 },
  track: { start: 240, span: 120 },
}

export interface RadarPoint {
  novel_id: number
  angle: number
  radius: number
  symbolSize: number
  color: string
  selected: boolean
  unavailable: boolean
  strength: number | null
  title: string
  author_name: string
  state: string | null
  total_view: number
  total_bookmarks: number
  ewma: number | null
  threshold: number | null
  ratio: number | null
  last_seen_at: string
}

/** 确定性雷达点映射：状态扇区 + novel_id 升序；半径按稳健对数强度。 */
export function buildRadarPoints(targets: Target[]): RadarPoint[] {
  const ratios = targets.map((t) => displayRatio(t.latest_ewma, t.latest_threshold))
  const scaleMax = buildScaleMax(ratios)

  const byState: Record<string, Target[]> = {}
  for (const target of targets) {
    const state = target.state ?? 'cold_start'
    ;(byState[state] ??= []).push(target)
  }
  const maxViews = Math.max(...targets.map((t) => t.total_view), 1)
  const points: RadarPoint[] = []
  for (const [state, group] of Object.entries(byState)) {
    const sector = SECTORS[state] ?? SECTORS.cold_start
    const sorted = [...group].sort((a, b) => a.novel_id - b.novel_id)
    const count = sorted.length || 1
    sorted.forEach((target, index) => {
      const ratio = displayRatio(target.latest_ewma, target.latest_threshold)
      const strength = ratio === null ? null : visualStrength(ratio, scaleMax)
      const angle = sector.start + ((index + 0.5) / count) * sector.span
      const size =
        6 +
        12 *
          Math.min(
            Math.log10(1 + target.total_view) / Math.log10(1 + maxViews),
            1,
          )
      points.push({
        novel_id: target.novel_id,
        angle,
        radius: radarRadius(strength),
        symbolSize: size,
        color: STATE_COLORS[state] ?? STATE_COLORS.cold_start,
        selected: false,
        unavailable: strength === null,
        strength,
        title: target.title,
        author_name: target.author_name,
        state,
        total_view: target.total_view,
        total_bookmarks: target.total_bookmarks,
        ewma: target.latest_ewma,
        threshold: target.latest_threshold,
        ratio,
        last_seen_at: target.last_seen_at,
      })
    })
  }
  return points
}

/** 当前结果集的雷达尺度（供热力图共用同一 scale_max）。 */
export function radarScaleMax(targets: Target[]): number {
  return buildScaleMax(
    targets.map((t) => displayRatio(t.latest_ewma, t.latest_threshold)),
  )
}

export function buildRadarOption(targets: Target[], selectedId: number | null) {
  const points = buildRadarPoints(targets).map((p) => ({
    ...p,
    selected: p.novel_id === selectedId,
  }))
  const data = points.map((p) => ({
    // ECharts polar scatter consumes [radius, angle] (the third dimension is
    // metadata only).  Keep novel_id on the object for click selection.
    value: [p.radius, p.angle, p.novel_id],
    symbol: p.unavailable ? 'triangle' : p.selected ? 'diamond' : 'circle',
    symbolSize: p.symbolSize,
    itemStyle: {
      color: p.color,
      borderColor: p.selected ? '#ffffff' : p.color,
      borderWidth: p.selected ? 2 : 0,
      opacity: p.unavailable ? 0.6 : 1,
    },
    novel_id: p.novel_id,
    _meta: p,
  }))
  return {
    tooltip: {
      formatter: (params: { data?: { _meta?: RadarPoint } }) => {
        const meta = params?.data?._meta
        if (!meta) return ''
        const lines = [
          `${escapeHtml(meta.title)}`,
          `作者：${escapeHtml(meta.author_name || '—')}`,
          `状态：${STATE_LABELS[meta.state ?? ''] ?? meta.state ?? '—'}`,
          `阅读 ${meta.total_view} / 收藏 ${meta.total_bookmarks}`,
          `EWMA：${meta.ewma?.toFixed(2) ?? '—'}`,
          `门限：${meta.threshold?.toFixed(2) ?? '—'}`,
          `增长信号/门限比：${formatRatio(meta.ratio)}${
            meta.unavailable ? '（不可用）' : ''
          }`,
          `最近发现：${formatLocalTime(meta.last_seen_at)}`,
        ]
        return lines.join('<br/>')
      },
    },
    angleAxis: {
      type: 'value',
      min: 0,
      max: 360,
      startAngle: 90,
      axisLabel: { formatter: '{value}°' },
      splitLine: { show: false },
    },
    radiusAxis: {
      type: 'value',
      min: 0,
      max: 100,
      axisLabel: { show: false },
    },
    polar: {},
    series: [
      {
        type: 'scatter',
        coordinateSystem: 'polar',
        data,
        emphasis: { scale: 1.2 },
      },
    ],
  }
}

/** 热力图 option（12 行 × 最多 7 列；无指标灰色；颜色按稳健对数强度 0..1）。 */
export function buildHeatmapOption(
  items: HeatmapItem[],
  scaleMax: number,
) {
  const rows = items.slice(0, 12)
  const data: unknown[] = []
  rows.forEach((item, rowIndex) => {
    item.points.slice(0, 7).forEach((point, colIndex) => {
      const ratio = displayRatio(point.ewma, point.threshold)
      const strength = ratio === null ? null : visualStrength(ratio, scaleMax)
      const cell: Record<string, unknown> = {
        value: [colIndex, rowIndex, strength === null ? -1 : strength],
        novel_id: item.novel_id,
        row_index: rowIndex,
        _meta: { point, ratio, strength, title: item.title, state: item.state },
      }
      if (strength === null) {
        cell.itemStyle = { color: '#e5e7eb' }
      }
      data.push(cell)
    })
  })
  return {
    tooltip: {
      formatter: (params: {
        data?: {
          _meta?: {
            point: {
              captured_at: string
              result: string | null
              reason_codes: string[]
              ewma: number | null
              threshold: number | null
            }
            ratio: number | null
            strength: number | null
            title: string
            state: string | null
          }
        }
      }) => {
        const meta = params?.data?._meta
        if (!meta) return ''
        const lines = [
          `${escapeHtml(meta.title)}`,
          `时间：${formatLocalTime(meta.point.captured_at)}`,
          `EWMA：${meta.point.ewma?.toFixed(2) ?? '—'}`,
          `门限：${meta.point.threshold?.toFixed(2) ?? '—'}`,
          `增长信号/门限比：${formatRatio(meta.ratio)}`,
          `结果：${
            RESULT_LABELS[meta.point.result ?? ''] ?? meta.point.result ?? '—'
          }`,
        ]
        const reasons = meta.point.reason_codes.filter(
          (r) => typeof r === 'string',
        )
        if (reasons.length > 0) {
          lines.push(`原因：${escapeHtml(reasons.join(', '))}`)
        }
        return lines.join('<br/>')
      },
    },
    xAxis: {
      type: 'category',
      name: '最近 7 次扫描',
      data: ['1', '2', '3', '4', '5', '6', '7'],
      splitArea: { show: true },
    },
    yAxis: {
      type: 'category',
      data: rows.map((r) => `${r.novel_id}`),
      inverse: true,
      axisLabel: { fontSize: 10 },
      splitArea: { show: true },
    },
    visualMap: {
      min: 0,
      max: 1,
      calculable: false,
      orient: 'vertical',
      right: 0,
      top: 'center',
      text: ['强', '弱'],
      inRange: { color: ['#dbeafe', '#2563eb', '#7c3aed'] },
    },
    series: [
      {
        type: 'heatmap',
        data,
        label: { show: false },
        emphasis: { itemStyle: { borderColor: '#111827', borderWidth: 1 } },
      },
    ],
  }
}

/** 详情曲线 option：EWMA 与 threshold 双线；缺失点留空不伪造 0。 */
export function buildHistoryOption(history: HistoryItem[]) {
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: ['EWMA', '门限'] },
    grid: { left: 48, right: 16, top: 32, bottom: 48 },
    xAxis: {
      type: 'category',
      data: history.map((h) => formatLocalTime(h.captured_at)),
      axisLabel: { rotate: 30, fontSize: 10 },
    },
    yAxis: { type: 'value' },
    series: [
      {
        name: 'EWMA',
        type: 'line',
        data: history.map((h) => h.ewma_view_velocity),
        connectNulls: false,
        symbol: 'circle',
      },
      {
        name: '门限',
        type: 'line',
        data: history.map((h) => h.threshold),
        connectNulls: false,
        symbol: 'none',
        lineStyle: { type: 'dashed' },
      },
    ],
  }
}

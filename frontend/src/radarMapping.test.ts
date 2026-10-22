import { describe, expect, it } from 'vitest'
import {
  RADAR_RADIUS_INNER,
  RADAR_RADIUS_OUTER,
  RADAR_RADIUS_UNAVAILABLE,
  buildHeatmapOption,
  buildHistoryOption,
  buildRadarOption,
  buildRadarPoints,
  buildScaleMax,
  displayRatio,
  escapeHtml,
  formatRatio,
  percentile95,
  radarRadius,
  visualStrength,
} from './radarMapping'
import { HeatmapItem, HistoryItem, Target } from './api'

function makeTarget(overrides: Partial<Target>): Target {
  return {
    novel_id: 1,
    title: 'novel-01',
    author_name: 'author-1',
    text_length: 100,
    total_bookmarks: 10,
    total_view: 300,
    total_comments: 0,
    last_seen_at: '2026-08-30T00:00:00+00:00',
    snapshot_at: null,
    state: 'cold_start',
    observation_count: 0,
    promotion_streak: 0,
    demotion_streak: 0,
    latest_result: null,
    latest_ewma: null,
    latest_threshold: null,
    latest_threshold_source: null,
    signal_ratio: null,
    data_source: 'mock',
    ...overrides,
  }
}

describe('displayRatio', () => {
  it('computes max(ewma,0)/max(threshold,eps)', () => {
    expect(displayRatio(20, 5)).toBeCloseTo(4)
    expect(displayRatio(-20, 5)).toBeCloseTo(0)
    expect(displayRatio(20, 0)).toBeNull()
    expect(displayRatio(20, -3)).toBeNull()
    expect(displayRatio(null, 5)).toBeNull()
    expect(displayRatio(Infinity, 5)).toBeNull()
    expect(displayRatio(5, NaN)).toBeNull()
  })
})

describe('percentile95 / buildScaleMax', () => {
  it('uses deterministic linear interpolation', () => {
    expect(percentile95([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])).toBeCloseTo(9.55)
    expect(percentile95([])).toBeNull()
    expect(percentile95([7])).toBe(7)
  })

  it('scale_max is at least 2', () => {
    expect(buildScaleMax([])).toBe(2)
    expect(buildScaleMax([0.1, 0.5])).toBe(2)
    expect(buildScaleMax([1, 2, 5, 6.4])).toBeGreaterThan(2)
  })

  it('ignores unavailable (null) in scale', () => {
    expect(buildScaleMax([null, null, 0.5])).toBe(2)
  })
})

describe('visualStrength / radarRadius', () => {
  it('maps [0,1,2,5,6.4] finite and monotonic; 5 and 6.4 distinct', () => {
    const ratios = [0, 1, 2, 5, 6.4]
    const scale = buildScaleMax(ratios)
    const strengths = ratios.map((r) => visualStrength(r, scale))
    strengths.forEach((s) => expect(Number.isFinite(s)).toBe(true))
    for (let i = 1; i < strengths.length; i++) {
      expect(strengths[i]).toBeGreaterThan(strengths[i - 1])
    }
    expect(strengths[4]).toBeGreaterThan(strengths[3]) // 6.4 > 5
    const radii = strengths.map((s) => radarRadius(s))
    expect(radii[4]).toBeLessThan(radii[3]) // 更强更靠圆心
    expect(new Set(radii.map((r) => r.toFixed(6))).size).toBe(5)
  })

  it('extreme outlier does not crush low values into one point', () => {
    const scale = buildScaleMax([0, 1, 2, 3, 4, 100])
    const radii = [0, 1, 2, 3, 4].map((r) =>
      radarRadius(visualStrength(r, scale)),
    )
    expect(new Set(radii.map((r) => r.toFixed(4))).size).toBe(5)
  })

  it('radius bounds and unavailable ring', () => {
    const scale = buildScaleMax([1])
    const radius = radarRadius(visualStrength(1, scale))
    expect(radius).toBeGreaterThanOrEqual(RADAR_RADIUS_INNER)
    expect(radius).toBeLessThanOrEqual(RADAR_RADIUS_OUTER)
    expect(radarRadius(null)).toBe(RADAR_RADIUS_UNAVAILABLE)
  })
})

describe('buildRadarPoints', () => {
  it('is deterministic and places unavailable on outer ring, not zero', () => {
    const targets = [
      makeTarget({ novel_id: 1, state: 'broad', latest_ewma: 20, latest_threshold: 5 }),
      makeTarget({ novel_id: 2, state: 'broad', latest_ewma: null, latest_threshold: 5 }),
    ]
    const a = buildRadarPoints(targets)
    const b = buildRadarPoints(targets)
    expect(a).toEqual(b)
    const byId = new Map(a.map((p) => [p.novel_id, p]))
    expect(byId.get(2)!.unavailable).toBe(true)
    expect(byId.get(2)!.radius).toBe(RADAR_RADIUS_UNAVAILABLE)
    expect(byId.get(1)!.radius).toBeLessThan(RADAR_RADIUS_OUTER)
  })

  it('keeps angles per state sector ordered by novel_id', () => {
    const points = buildRadarPoints([
      makeTarget({ novel_id: 5, state: 'cold_start' }),
      makeTarget({ novel_id: 3, state: 'cold_start' }),
      makeTarget({ novel_id: 9, state: 'broad' }),
    ])
    const byId = new Map(points.map((p) => [p.novel_id, p]))
    expect(byId.get(3)!.angle).toBeLessThan(byId.get(5)!.angle)
    expect(byId.get(9)!.angle).toBeGreaterThanOrEqual(120)
  })
})

describe('buildRadarOption', () => {
  it('uses ECharts polar scatter dimension order [radius, angle, novel_id]', () => {
    const target = makeTarget({
      novel_id: 11,
      state: 'track',
      latest_ewma: 10,
      latest_threshold: 5,
    })
    const option = buildRadarOption([target], null) as {
      series: { data: { value: number[]; _meta: { radius: number; angle: number; novel_id: number } }[] }[]
    }
    const point = option.series[0].data[0]
    expect(point.value).toEqual([point._meta.radius, point._meta.angle, 11])
    expect(point.value[0]).not.toBe(point.value[1])
  })

  it('marks selected point with distinct symbol and white border', () => {
    const targets = [
      makeTarget({ novel_id: 1, state: 'track', latest_ewma: 10, latest_threshold: 5 }),
      makeTarget({ novel_id: 2, state: 'track', latest_ewma: 12, latest_threshold: 5 }),
    ]
    const option = buildRadarOption(targets, 2) as {
      series: { data: { novel_id: number; symbol: string; itemStyle: { borderColor: string; borderWidth: number } }[] }[]
    }
    const selected = option.series[0].data.find((d) => d.novel_id === 2)!
    const unselected = option.series[0].data.find((d) => d.novel_id === 1)!
    expect(selected.symbol).not.toBe(unselected.symbol)
    expect(selected.itemStyle.borderColor).toBe('#ffffff')
  })

  it('escapes malicious title/author in tooltip formatter output', () => {
    const targets = [
      makeTarget({
        novel_id: 1,
        title: '<img src=x onerror=alert(1)>',
        author_name: 'a"b&c<script>',
        latest_ewma: 10,
        latest_threshold: 5,
      }),
    ]
    const option = buildRadarOption(targets, null) as {
      tooltip: { formatter: (p: { data: { _meta: unknown } }) => string }
      series: { data: { _meta: unknown }[] }[]
    }
    const rendered = option.tooltip.formatter({
      data: option.series[0].data[0],
    })
    expect(rendered).not.toContain('<img')
    expect(rendered).not.toContain('<script>')
    // 原始尖括号全部转义，属性文本只会作为惰性文本出现，不会形成可执行 HTML
    expect(rendered).not.toContain('<img src=')
    expect(rendered).not.toContain('<b onclick')
    expect(rendered).toContain('&lt;img')
    expect(rendered).toContain('&quot;')
    expect(rendered).toContain('&amp;')
  })
})

describe('escapeHtml', () => {
  it('escapes & < > " and single quote', () => {
    expect(escapeHtml(`&<>"'`)).toBe('&amp;&lt;&gt;&quot;&#39;')
  })
})

describe('buildHeatmapOption', () => {
  const item: HeatmapItem = {
    novel_id: 1,
    title: 'novel-01',
    author_name: 'author-1',
    state: 'track',
    points: [
      { scan_run_id: 1, captured_at: '2026-08-30T00:00:00+00:00', ewma: 25, threshold: 5, result: 'qualified', reason_codes: [] },
      { scan_run_id: 2, captured_at: '2026-08-30T01:00:00+00:00', ewma: 32, threshold: 5, result: 'qualified', reason_codes: [] },
      { scan_run_id: 3, captured_at: '2026-08-30T02:00:00+00:00', ewma: null, threshold: null, result: 'unknown', reason_codes: ['insufficient_exposure'] },
    ],
  }

  it('maps different high ratios to different strengths/colors', () => {
    const option = buildHeatmapOption([item], buildScaleMax([5, 6.4])) as {
      series: { data: { value: number[]; itemStyle?: { color: string } }[] }[]
    }
    const cells = option.series[0].data
    expect(cells[0].value[2]).not.toBe(cells[1].value[2])
    expect(cells[0].value[2]).toBeGreaterThan(0)
    expect(cells[1].value[2]).toBeGreaterThan(cells[0].value[2])
  })

  it('colors missing metrics gray and escapes tooltip reason text', () => {
    const option = buildHeatmapOption([item], 2) as {
      series: { data: { value: number[]; itemStyle?: { color: string }; _meta: unknown }[] }[]
      tooltip: { formatter: (p: { data: { _meta: unknown } }) => string }
    }
    const missing = option.series[0].data[2]
    expect(missing.value[2]).toBe(-1)
    expect(missing.itemStyle?.color).toBe('#e5e7eb')
    const malicious: HeatmapItem = {
      novel_id: 2,
      title: '<b onclick=alert(1)>',
      author_name: 'a',
      state: 'broad',
      points: [
        { scan_run_id: 1, captured_at: '2026-08-30T00:00:00+00:00', ewma: 5, threshold: 5, result: 'unknown', reason_codes: ['<img src=x onerror=alert(1)>'] },
      ],
    }
    const opt2 = buildHeatmapOption([malicious], 2) as {
      series: { data: { _meta: unknown }[] }[]
      tooltip: { formatter: (p: { data: { _meta: unknown } }) => string }
    }
    const rendered = opt2.tooltip.formatter({ data: opt2.series[0].data[0] })
    expect(rendered).not.toContain('<img')
    expect(rendered).not.toContain('<b ')
    expect(rendered).toContain('&lt;b')
  })

  it('caps rows at 12 and points at 7', () => {
    const items: HeatmapItem[] = Array.from({ length: 15 }, (_, i) => ({
      novel_id: i + 1,
      title: `n${i}`,
      author_name: 'a',
      state: 'track',
      points: Array.from({ length: 9 }, (_, j) => ({
        scan_run_id: j + 1,
        captured_at: `2026-08-30T0${j}:00:00+00:00`,
        ewma: 1,
        threshold: 1,
        result: 'qualified',
        reason_codes: [],
      })),
    }))
    const option = buildHeatmapOption(items, 2) as {
      yAxis: { data: string[] }
      series: { data: unknown[] }[]
    }
    expect(option.yAxis.data.length).toBe(12)
    const byRow = new Map<number, number>()
    for (const cell of option.series[0].data as { value: number[] }[]) {
      byRow.set(cell.value[1], (byRow.get(cell.value[1]) ?? 0) + 1)
    }
    for (const count of byRow.values()) {
      expect(count).toBeLessThanOrEqual(7)
    }
  })
})

describe('buildHistoryOption', () => {
  it('keeps missing points as null (not 0)', () => {
    const history: HistoryItem[] = [
      { novel_id: 1, scan_run_id: 1, captured_at: '2026-08-30T00:00:00+00:00', total_view: 300, total_bookmarks: 10, result: 'unknown', ewma_view_velocity: null, threshold: 5 },
      { novel_id: 1, scan_run_id: 2, captured_at: '2026-08-30T01:00:00+00:00', total_view: 500, total_bookmarks: 20, result: 'qualified', ewma_view_velocity: 200, threshold: 5 },
    ]
    const option = buildHistoryOption(history) as { series: { name: string; data: (number | null)[]; connectNulls: boolean }[] }
    expect(option.series[0].data).toEqual([null, 200])
    expect(option.series[1].data).toEqual([5, 5])
    expect(option.series[0].connectNulls).toBe(false)
  })
})

describe('formatRatio', () => {
  it('formats ratio or dash', () => {
    expect(formatRatio(1.5)).toBe('1.50')
    expect(formatRatio(null)).toBe('—')
  })
})

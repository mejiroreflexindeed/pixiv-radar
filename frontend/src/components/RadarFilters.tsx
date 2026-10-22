import { useEffect, useRef, useState } from 'react'
import { TargetFilters, TargetResult, TargetState } from '../api'

interface Props {
  filters: TargetFilters
  onChange: (filters: TargetFilters) => void
}

const STATE_OPTIONS: { value: TargetState; label: string }[] = [
  { value: 'cold_start', label: '冷启动' },
  { value: 'broad', label: '广域' },
  { value: 'track', label: '追尾' },
]

const RESULT_OPTIONS: { value: TargetResult; label: string }[] = [
  { value: 'qualified', label: '合格' },
  { value: 'not_qualified', label: '不合格' },
  { value: 'unknown', label: '未知' },
]

function RadarFilters({ filters, onChange }: Props) {
  const [q, setQ] = useState(filters.q)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  // q 输入防抖 300ms；其余立即生效
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => {
      if (q !== filters.q) onChange({ ...filters, q })
    }, 300)
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q])

  return (
    <div className="card">
      <h2 className="card__title">筛选</h2>
      <div className="filters-row">
        <label className="filters-field">
          <span className="filters-label">标题/作者</span>
          <input
            type="text"
            value={q}
            maxLength={100}
            placeholder="搜索标题或作者"
            aria-label="标题或作者查询"
            onChange={(e) => setQ(e.target.value)}
          />
        </label>
        <label className="filters-field">
          <span className="filters-label">状态</span>
          <select
            value={filters.state ?? ''}
            aria-label="状态筛选"
            onChange={(e) =>
              onChange({
                ...filters,
                state: (e.target.value || null) as TargetState | null,
              })
            }
          >
            <option value="">全部</option>
            {STATE_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label className="filters-field">
          <span className="filters-label">最新评估</span>
          <select
            value={filters.result ?? ''}
            aria-label="最新评估筛选"
            onChange={(e) =>
              onChange({
                ...filters,
                result: (e.target.value || null) as TargetResult | null,
              })
            }
          >
            <option value="">全部</option>
            {RESULT_OPTIONS.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <label className="filters-field">
          <span className="filters-label">最小信号比</span>
          <input
            type="number"
            min={0}
            max={100}
            step="0.1"
            value={filters.minSignalRatio ?? ''}
            aria-label="最小增长信号门限比"
            onChange={(e) => {
              const raw = e.target.value
              const value = raw === '' ? null : Number(raw)
              onChange({
                ...filters,
                minSignalRatio:
                  value === null || Number.isNaN(value) ? null : value,
              })
            }}
          />
        </label>
      </div>
    </div>
  )
}

export default RadarFilters

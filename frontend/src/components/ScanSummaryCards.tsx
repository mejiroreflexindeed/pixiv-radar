import { ScanRun } from '../api'
import { formatLocalTime } from '../format'

interface Props {
  total: number
  trackCount: number
  latestScan: ScanRun | null
  timeZone: string
}

function statusBadge(status: string) {
  if (status === 'succeeded') return <span className="badge badge--success">成功</span>
  if (status === 'partial') return <span className="badge badge--warning">部分成功</span>
  if (status === 'failed') return <span className="badge badge--error">失败</span>
  return <span className="badge badge--muted">{status}</span>
}

function ScanSummaryCards({ total, trackCount, latestScan, timeZone }: Props) {
  return (
    <div className="summary-cards">
      <div className="summary-card">
        <div className="summary-card__label">目标总数</div>
        <div className="summary-card__value">{total}</div>
      </div>
      <div className="summary-card">
        <div className="summary-card__label">追尾中</div>
        <div className="summary-card__value">{trackCount}</div>
      </div>
      <div className="summary-card">
        <div className="summary-card__label">最近扫描</div>
        <div className="summary-card__value">
          {latestScan ? statusBadge(latestScan.status) : '—'}
        </div>
        <div className="muted">
          {latestScan
            ? formatLocalTime(latestScan.finished_at ?? latestScan.started_at)
            : '尚无扫描'}
        </div>
      </div>
      <div className="summary-card">
        <div className="summary-card__label">数据来源</div>
        <div className="summary-card__value">{latestScan?.source ?? 'unknown'}</div>
      </div>
      <div className="summary-card">
        <div className="summary-card__label">时区</div>
        <div className="summary-card__value muted">{timeZone}</div>
      </div>
    </div>
  )
}

export default ScanSummaryCards

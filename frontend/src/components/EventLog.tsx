import { EventItem } from '../api'
import { formatLocalTime } from '../format'

interface Props {
  events: EventItem[]
}

const EVENT_LABELS: Record<string, string> = {
  target_entered_track: '进入追尾',
  target_left_track: '退出追尾',
  cold_start_completed: '冷启动完成',
  data_quality_unknown: '数据质量未知',
  scan_rate_limited: '扫描限流',
  scan_partial: '扫描部分成功',
}

function severityBadge(severity: string) {
  if (severity === 'error') return <span className="badge badge--error">错误</span>
  if (severity === 'warning') return <span className="badge badge--warning">警告</span>
  return <span className="badge badge--info">信息</span>
}

function EventLog({ events }: Props) {
  return (
    <div className="card">
      <h2 className="card__title">事件日志</h2>
      {events.length === 0 ? (
        <p className="muted">暂无事件。</p>
      ) : (
        <ul className="run-list" aria-label="事件日志列表">
          {events.map((event) => (
            <li key={event.id}>
              {severityBadge(event.severity)}{' '}
              <span>{EVENT_LABELS[event.type] ?? event.type}</span>{' '}
              <span className="muted">
                #{event.novel_id ?? '—'} · {formatLocalTime(event.created_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default EventLog

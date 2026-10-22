import { ScanRun } from '../api'
import { formatLocalTime } from '../format'

interface Props {
  runs: ScanRun[]
}

function badge(status: string) {
  if (status === 'succeeded') return <span className="badge badge--success">成功</span>
  if (status === 'partial') return <span className="badge badge--warning">部分成功</span>
  if (status === 'failed') return <span className="badge badge--error">失败</span>
  return <span className="badge badge--muted">{status}</span>
}

function RunHistory({ runs }: Props) {
  return (
    <div className="card">
      <h2 className="card__title">扫描历史</h2>
      {runs.length === 0 ? (
        <p className="muted">暂无扫描记录</p>
      ) : (
        <ul className="run-list">
          {runs.map((run) => (
            <li key={run.id}>
              #{run.id} {badge(run.status)}{' '}
              <span className="muted">
                发现 {run.results_seen} / 写入 {run.results_written}
                {' · '}
                {run.source}
                {' · '}
                {formatLocalTime(run.started_at)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default RunHistory

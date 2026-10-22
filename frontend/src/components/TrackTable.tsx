import {
  ColumnDef,
  SortingState,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
} from '@tanstack/react-table'
import { Target, SortDir, TargetSort } from '../api'
import { formatLocalTime } from '../format'
import {
  RESULT_LABELS,
  STATE_LABELS,
  STATE_COLORS,
  formatRatio,
} from '../radarMapping'

const SORT_KEY: Record<string, TargetSort> = {
  title: 'title',
  signal: 'signal',
  views: 'views',
  bookmarks: 'bookmarks',
  time: 'time',
}

interface Props {
  items: Target[]
  selectedId: number | null
  onSelect: (id: number) => void
  sort: TargetSort
  sortDir: SortDir
  onSortChange: (sort: TargetSort, dir: SortDir) => void
  total?: number
  page?: number
  pageSize?: number
  onPageChange?: (page: number) => void
}

function stateBadge(state: string | null) {
  const label = state ? STATE_LABELS[state] ?? state : '—'
  const color = state ? STATE_COLORS[state] ?? '#6b7280' : '#6b7280'
  return (
    <span className="badge" style={{ background: `${color}22`, color }}>
      {label}
    </span>
  )
}

function resultBadge(result: string | null) {
  if (!result) return <span className="badge badge--muted">—</span>
  if (result === 'qualified') return <span className="badge badge--success">合格</span>
  if (result === 'not_qualified') return <span className="badge badge--muted">不合格</span>
  return <span className="badge badge--warning">{RESULT_LABELS[result] ?? result}</span>
}

function TrackTable({
  items,
  selectedId,
  onSelect,
  sort,
  sortDir,
  onSortChange,
  total,
  page = 1,
  pageSize = 50,
  onPageChange,
}: Props) {
  const columns: ColumnDef<Target>[] = [
    {
      id: 'title',
      header: '标题',
      accessorKey: 'title',
      cell: (info) => (
        <span className="title-cell" title={info.getValue<string>()}>
          {info.getValue<string>()}
        </span>
      ),
    },
    {
      id: 'signal',
      header: '增长信号/门限比',
      accessorKey: 'signal_ratio',
      cell: (info) => (
        <span className="num">{formatRatio(info.getValue<number | null>())}</span>
      ),
    },
    {
      id: 'views',
      header: '阅读',
      accessorKey: 'total_view',
      cell: (info) => (
        <span className="num">{info.getValue<number>() ?? '—'}</span>
      ),
    },
    {
      id: 'bookmarks',
      header: '收藏',
      accessorKey: 'total_bookmarks',
      cell: (info) => (
        <span className="num">{info.getValue<number>() ?? '—'}</span>
      ),
    },
    {
      id: 'time',
      header: '最近发现',
      accessorKey: 'last_seen_at',
      cell: (info) => formatLocalTime(info.getValue<string>()),
    },
    {
      id: 'state',
      header: '状态',
      accessorKey: 'state',
      cell: (info) => stateBadge(info.getValue<string | null>()),
    },
    {
      id: 'result',
      header: '最新评估',
      accessorKey: 'latest_result',
      cell: (info) => resultBadge(info.getValue<string | null>()),
    },
    {
      id: 'source',
      header: '来源',
      accessorKey: 'data_source',
      cell: (info) => info.getValue<string | null>() ?? '—',
    },
  ]

  // 受控排序：sorting state 完全由 props 派生，点击表头只调用一次 onSortChange
  // （在事件处理器中，而非 setSorting updater 内），消除跨组件更新警告。
  const sorting: SortingState = (() => {
    const columnId = Object.keys(SORT_KEY).find((k) => SORT_KEY[k] === sort)
    return columnId ? [{ id: columnId, desc: sortDir === 'desc' }] : []
  })()

  const handleSortToggle = (columnId: string) => {
    const key = SORT_KEY[columnId]
    if (!key) return
    const nextDir: SortDir = sort === key && sortDir === 'asc' ? 'desc' : 'asc'
    onSortChange(key, nextDir)
  }

  const table = useReactTable({
    data: items,
    columns,
    state: { sorting },
    manualSorting: true,
    onSortingChange: () => {
      // 排序状态由 props 受控；实际切换在 handleSortToggle 中发出。
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })

  return (
    <div className="card">
      <h2 className="card__title">目标表格</h2>
      {items.length === 0 ? (
        <p className="muted">暂无目标（空状态，不代表扫描失败）。可先点击上方“运行发现/扫描”开始离线演示。</p>
      ) : (
        <div className="table-wrap">
        <table role="table" aria-label="目标列表（可排序、可选择）">
          <thead>
            {table.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => {
                  const canSort = header.id in SORT_KEY
                  const ariaSort =
                    header.column.getIsSorted() === 'asc'
                      ? 'ascending'
                      : header.column.getIsSorted() === 'desc'
                        ? 'descending'
                        : 'none'
                  return (
                    <th
                      key={header.id}
                      className={header.id in { views: 1, bookmarks: 1, signal: 1 } ? 'num' : undefined}
                      aria-sort={canSort ? ariaSort : undefined}
                    >
                      {canSort ? (
                        <button
                          type="button"
                          className="th-sort"
                          onClick={() => handleSortToggle(header.id)}
                        >
                          {flexRender(header.column.columnDef.header, header.getContext())}
                          <span aria-hidden="true">
                            {ariaSort === 'ascending' ? ' ↑' : ariaSort === 'descending' ? ' ↓' : ' ↕'}
                          </span>
                        </button>
                      ) : (
                        flexRender(header.column.columnDef.header, header.getContext())
                      )}
                    </th>
                  )
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr
                key={row.id}
                role="row"
                aria-selected={selectedId === row.original.novel_id}
                tabIndex={0}
                onClick={() => onSelect(row.original.novel_id)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    onSelect(row.original.novel_id)
                  }
                }}
              >
                {row.getVisibleCells().map((cell) => (
                  <td key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      )}
      <p className="muted">
        {total !== undefined
          ? `第 ${total === 0 ? 0 : (page - 1) * pageSize + 1}–${Math.min(
              page * pageSize,
              total,
            )} 条 / 共 ${total} 条；点击或按 Enter/空格选择。`
          : `共 ${items.length} 个目标；点击或按 Enter/空格选择。`}
      </p>
      {onPageChange && total !== undefined && total > pageSize && (
        <div className="pagination">
          <button
            className="button button--secondary"
            disabled={page <= 1}
            onClick={() => onPageChange(page - 1)}
            aria-label="上一页"
          >
            上一页
          </button>
          <span className="muted">
            第 {page} / {Math.max(1, Math.ceil(total / pageSize))} 页
          </span>
          <button
            className="button button--secondary"
            disabled={page >= Math.ceil(total / pageSize)}
            onClick={() => onPageChange(page + 1)}
            aria-label="下一页"
          >
            下一页
          </button>
        </div>
      )}
    </div>
  )
}

export default TrackTable

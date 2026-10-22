/** 本地时区显示：UTC 入库，浏览器本地展示（见 AUTH_AND_UI_DESIGN.md §1）。 */

export function formatLocalTime(
  iso: string | null | undefined,
  timeZone?: string,
): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const options: Intl.DateTimeFormatOptions = {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }
  if (timeZone) options.timeZone = timeZone
  return new Intl.DateTimeFormat('zh-CN', options).format(date)
}

export function timeZoneLabel(): string {
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone
  const offsetMinutes = new Date().getTimezoneOffset()
  const hours = -offsetMinutes / 60
  const sign = hours >= 0 ? '+' : ''
  return `UTC${sign}${hours} (${tz})`
}

import { describe, expect, it } from 'vitest'
import { formatLocalTime, timeZoneLabel } from './format'

describe('formatLocalTime', () => {
  it('converts UTC to fixed timezone deterministically', () => {
    expect(
      formatLocalTime('2026-08-30T00:00:00+00:00', 'UTC'),
    ).toContain('2026/08/30')
    expect(
      formatLocalTime('2026-08-30T00:00:00+00:00', 'Asia/Tokyo'),
    ).toContain('2026/08/30 09:00:00')
  })

  it('handles null/undefined as dash', () => {
    expect(formatLocalTime(null)).toBe('—')
    expect(formatLocalTime(undefined)).toBe('—')
  })

  it('returns raw string for unparseable input', () => {
    expect(formatLocalTime('not-a-date')).toBe('not-a-date')
  })

  it('shows negative offset conversion', () => {
    // UTC-5 → 前一天 19:00
    expect(
      formatLocalTime('2026-08-30T00:00:00+00:00', 'America/New_York'),
    ).toContain('2026/08/29')
  })
})

describe('timeZoneLabel', () => {
  it('returns a label containing UTC offset and zone', () => {
    const label = timeZoneLabel()
    expect(label).toMatch(/^UTC[+-]\d+ \(.+\)$/)
  })
})

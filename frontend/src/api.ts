export interface RuntimeInfo {
  mode: 'demo' | 'real' | string
  db_path: string
  db_scope: 'formal' | 'preview' | 'custom' | string
  schedule_minutes: number
  schedule_tag: string
}

export interface Health {
  status: string
  app: string
  version: string
  time: string
  runtime?: RuntimeInfo
}

export interface ScanRun {
  id: number
  layer: string
  status: string
  started_at: string
  finished_at: string | null
  error: string | null
  results_seen: number
  results_written: number
  source: string
  run_key: string | null
}

export interface Target {
  novel_id: number
  title: string
  author_name: string
  text_length: number
  total_bookmarks: number
  total_view: number
  total_comments: number
  last_seen_at: string
  snapshot_at: string | null
  state: string | null
  observation_count: number | null
  promotion_streak: number | null
  demotion_streak: number | null
  latest_result: string | null
  latest_ewma: number | null
  latest_threshold: number | null
  latest_threshold_source: string | null
  signal_ratio: number | null
  data_source: string | null
  latest_reason_codes?: string[]
  tags?: { name: string; translated_name: string | null }[]
}

export interface TargetsResponse {
  items: Target[]
  total: number
  track_total: number
  limit: number
  offset: number
  latest_scan: ScanRun | null
}

export type TargetState = 'cold_start' | 'broad' | 'track'
export type TargetResult = 'qualified' | 'not_qualified' | 'unknown'
export type TargetSort = 'time' | 'title' | 'signal' | 'views' | 'bookmarks'
export type SortDir = 'asc' | 'desc'

export interface TargetFilters {
  q: string
  state: TargetState | null
  result: TargetResult | null
  minSignalRatio: number | null
}

export interface TargetQuery extends TargetFilters {
  sort: TargetSort
  sortDir: SortDir
  limit: number
  offset: number
}

export interface HeatmapPoint {
  scan_run_id: number
  captured_at: string
  ewma: number | null
  threshold: number | null
  result: string | null
  reason_codes: string[]
}

export interface HeatmapItem {
  novel_id: number
  title: string
  author_name: string
  state: string | null
  points: HeatmapPoint[]
}

export interface HeatmapResponse {
  items: HeatmapItem[]
}

export interface DiscoverySighting {
  source_kind: string
  query_key: string
  query_text: string | null
  source_rank: number
  discovered_at: string
}

export interface DiscoveryRun {
  id: number
  run_key: string
  status: string
  started_at: string
  finished_at: string | null
  planned_requests: number
  executed_requests: number
  results_seen: number
  unique_candidates: number
  error_code: string | null
}

export interface DiscoveryCandidate {
  novel_id: number
  title: string
  author_name: string
  total_bookmarks: number
  total_view: number
  total_comments: number
  sightings: DiscoverySighting[]
  group: string
  group_label: string
  group_reason: string
  explanations: string[]
  score_fields: {
    rate: number | null
    velocity: number | null
    snapshots: number
    sources: number
    feedback: string | null
  }
  filtered_reason: string | null
}

export interface DiscoveryGateState {
  busy: boolean
  paused_reason: string | null
  cooldown_until: number | null
  cooldown_remaining_seconds: number | null
  min_interval_seconds: number
}

export interface DiscoveryStatus {
  state: string
  source?: 'mock' | 'pixiv' | string
  current_run: {
    run_key: string
    status: string
    started_at: string
    error_code: string | null
    unique_candidates?: number
    planned_requests?: number
    executed_requests?: number
    results_seen?: number
  } | null
  gate?: DiscoveryGateState | null
  schedule?: {
    enabled: boolean
    tag: string | null
    interval_minutes: number | null
    next_run_at: string | null
    last_attempt_at: string | null
    last_error_code: string | null
  }
}

export interface ScanRunsResponse {
  items: ScanRun[]
}

export interface HistoryItem {
  novel_id: number
  scan_run_id: number
  captured_at: string
  total_view: number
  total_bookmarks: number
  result: string | null
  ewma_view_velocity: number | null
  threshold: number | null
}

export interface EventItem {
  id: number
  event_key: string
  type: string
  severity: string
  payload: Record<string, unknown>
  novel_id: number | null
  scan_run_id: number | null
  created_at: string
}

export type FeedbackEventType =
  | 'good'
  | 'exclude'
  | 'undo'
  | 'exposure'
  | 'click'
  | 'dwell'

export interface FeedbackEvent {
  id: number
  novel_id: number
  event_type: FeedbackEventType
  reverts_event_id: number | null
  scoring_version: string
  weight: number
  created_at: string
  is_reverted?: boolean
}

export interface FeedbackSummary {
  novel_id: number
  effective_feedback: 'good' | 'exclude' | null
  active_feedback_event_id: number | null
  explicit_score: number
  weak_signal_score: number
  score: number
  good_count: number
  exclude_count: number
  undo_count: number
  exposure_count: number
  click_count: number
  dwell_count: number
  scoring_version: string
  active_event_count: number
  reverted_event_count: number
}

export interface FeedbackTag {
  tag: string
  translated_name: string | null
  positive_count: number
  negative_count: number
  weak_signal_count: number
  score: number
}

export interface TargetFeedbackResponse {
  summary: FeedbackSummary
  events: FeedbackEvent[]
  limit: number
  offset: number
}

export type AuthState =
  | 'not_configured'
  | 'authorizing'
  | 'connected'
  | 'expired_or_revoked'
  | 'rate_limited'
  | 'temporarily_unavailable'
  | 'user_action_required'

export interface AuthStatus {
  state: AuthState
  login_available: boolean
  provider_mode: string
  can_disconnect: boolean
  last_success_at: string | null
  error_code: string | null
  cooldown_until: string | null
  session_expires_at: string | null
  validate_available: boolean
  unavailable_reason: string | null
}

export interface AuthStartResponse {
  status: AuthStatus
  launch: {
    mode: string
    browser_opened: boolean
  }
}

export interface ContentAnalysisStatus {
  available: boolean
}

export interface ContentAnalysis {
  novel_id: number
  analyzer_version: string
  language: string
  char_count: number
  paragraph_count: number
  dialogue_ratio: number
  image_marker_count: number
  structure_score: number
  is_clutter: boolean
  is_suspect: boolean
  keyword_hits: Record<string, number>
  reason_codes: string[]
  analyzed_at: string
  cached?: boolean
}

export interface SimilarContent {
  novel_id: number
  title: string
  similarity: number
  structure_score: number
  is_clutter: boolean
  is_suspect: boolean
  analyzed_at: string
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(url, init)
  if (!resp.ok) {
    let code = `HTTP ${resp.status}`
    try {
      const body = await resp.json()
      const safeCode = body?.detail?.code
      if (typeof safeCode === 'string') code = safeCode
    } catch {
      // 非 JSON 错误只显示 HTTP 状态，不暴露响应正文。
    }
    throw new Error(code)
  }
  return resp.json()
}

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  return requestJson<T>(url, { signal })
}

async function postJson<T>(
  url: string,
  body?: object,
  signal?: AbortSignal,
): Promise<T> {
  return requestJson<T>(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body ?? {}),
    signal,
  })
}

export function fetchHealth(): Promise<Health> {
  return getJson<Health>('/api/health')
}

export function fetchTargets(
  query?: Partial<TargetQuery>,
  signal?: AbortSignal,
): Promise<TargetsResponse> {
  const params = new URLSearchParams()
  const q = query ?? {}
  params.set('limit', String(q.limit ?? 100))
  params.set('offset', String(q.offset ?? 0))
  if (q.sort) params.set('sort', q.sort)
  if (q.sortDir) params.set('sort_dir', q.sortDir)
  if (q.q) params.set('q', q.q)
  if (q.state) params.set('state', q.state)
  if (q.result) params.set('result', q.result)
  if (q.minSignalRatio !== null && q.minSignalRatio !== undefined) {
    params.set('min_signal_ratio', String(q.minSignalRatio))
  }
  return getJson<TargetsResponse>(`/api/targets?${params.toString()}`, signal)
}

export function fetchHeatmap(
  filters?: Partial<TargetFilters>,
  signal?: AbortSignal,
): Promise<HeatmapResponse> {
  const params = new URLSearchParams()
  if (filters?.q) params.set('q', filters.q)
  if (filters?.state) params.set('state', filters.state)
  if (filters?.result) params.set('result', filters.result)
  if (filters?.minSignalRatio !== null && filters?.minSignalRatio !== undefined) {
    params.set('min_signal_ratio', String(filters.minSignalRatio))
  }
  const suffix = params.toString() ? `?${params.toString()}` : ''
  return getJson<HeatmapResponse>(`/api/dashboard/heatmap${suffix}`, signal)
}

export function fetchScanRuns(limit = 10): Promise<ScanRunsResponse> {
  return getJson<ScanRunsResponse>(`/api/scan-runs?limit=${limit}`)
}

export function fetchEvents(limit = 20): Promise<{ items: EventItem[] }> {
  return getJson<{ items: EventItem[] }>(`/api/events?limit=${limit}`)
}

export function fetchTargetHistory(
  novelId: number,
  limit = 50,
  signal?: AbortSignal,
): Promise<{ items: HistoryItem[] }> {
  return getJson<{ items: HistoryItem[] }>(
    `/api/targets/${novelId}/history?limit=${limit}`,
    signal,
  )
}

export function startDiscovery(tag = 'ウマ娘'): Promise<{
  run_key: string
  started_at: string
  source: string
}> {
  return postJson(`/api/discovery/start?tag=${encodeURIComponent(tag)}`)
}

export function cancelDiscovery(): Promise<{ cancelled: boolean }> {
  return postJson('/api/discovery/cancel')
}

export function fetchDiscoveryStatus(signal?: AbortSignal): Promise<DiscoveryStatus> {
  return getJson<DiscoveryStatus>('/api/discovery/status', signal)
}

export function fetchDiscoveryRuns(
  limit = 20,
): Promise<{ items: DiscoveryRun[] }> {
  return getJson<{ items: DiscoveryRun[] }>(`/api/discovery/runs?limit=${limit}`)
}

export function fetchDiscoveryCandidates(
  runId: number,
  limit = 100,
  offset = 0,
  novelId?: number,
  signal?: AbortSignal,
): Promise<{ items: DiscoveryCandidate[]; total: number; limit: number; offset: number }> {
  const novelQuery = novelId === undefined ? '' : `&novel_id=${novelId}`
  return getJson<{
    items: DiscoveryCandidate[]
    total: number
    limit: number
    offset: number
  }>(
    `/api/discovery/candidates?run_id=${runId}&limit=${limit}&offset=${offset}${novelQuery}`,
    signal,
  )
}

export function fetchTargetDetail(
  novelId: number,
  signal?: AbortSignal,
): Promise<Target> {
  return getJson<Target>(`/api/targets/${novelId}`, signal)
}

export function fetchTargetFeedback(
  novelId: number,
  limit = 20,
  offset = 0,
  signal?: AbortSignal,
): Promise<TargetFeedbackResponse> {
  return getJson<TargetFeedbackResponse>(
    `/api/feedback/targets/${novelId}?limit=${limit}&offset=${offset}`,
    signal,
  )
}

export function submitFeedback(
  novelId: number,
  eventType: FeedbackEventType,
  revertsEventId?: number | null,
  signal?: AbortSignal,
): Promise<{ event: FeedbackEvent; summary: FeedbackSummary }> {
  return postJson<{ event: FeedbackEvent; summary: FeedbackSummary }>(
    '/api/feedback',
    {
      novel_id: novelId,
      event_type: eventType,
      ...(revertsEventId == null ? {} : { reverts_event_id: revertsEventId }),
    },
    signal,
  )
}

export function fetchPreferenceTags(
  limit = 8,
  signal?: AbortSignal,
): Promise<{ items: FeedbackTag[] }> {
  return getJson<{ items: FeedbackTag[] }>(
    `/api/feedback/tags?limit=${limit}`,
    signal,
  )
}

export function fetchAuthStatus(): Promise<AuthStatus> {
  return getJson<AuthStatus>('/api/auth/status')
}

export function startAuth(): Promise<AuthStartResponse> {
  return postJson<AuthStartResponse>('/api/auth/start', {
    user_initiated: true,
  })
}

export function validateAuth(): Promise<AuthStatus> {
  return postJson<AuthStatus>('/api/auth/validate', {
    user_initiated: true,
  })
}

export function cancelAuth(): Promise<AuthStatus> {
  return postJson<AuthStatus>('/api/auth/cancel')
}

export function disconnectAuth(): Promise<AuthStatus> {
  return postJson<AuthStatus>('/api/auth/disconnect', { confirm: true })
}

export function fetchContentStatus(signal?: AbortSignal): Promise<ContentAnalysisStatus> {
  return getJson<ContentAnalysisStatus>('/api/content/status', signal)
}

export function fetchContentAnalysis(
  novelId: number,
  signal?: AbortSignal,
): Promise<ContentAnalysis> {
  return getJson<ContentAnalysis>(`/api/content/${novelId}`, signal)
}

export function analyzeContent(
  novelId: number,
  force = false,
): Promise<ContentAnalysis> {
  return postJson<ContentAnalysis>(
    `/api/content/${novelId}/analyze?force=${force ? 'true' : 'false'}`,
  )
}

export function fetchSimilarContent(
  novelId: number,
  limit = 5,
  signal?: AbortSignal,
): Promise<{ items: SimilarContent[]; method: string; note: string }> {
  return getJson(
    `/api/content/${novelId}/similar?limit=${limit}`,
    signal,
  )
}

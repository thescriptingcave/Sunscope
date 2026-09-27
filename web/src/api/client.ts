/**
 * REST client for the solar API.
 *
 * Same-origin in production (the API serves this bundle at `/`), so the base
 * URL is a relative path. In development Vite proxies /api to :8000.
 *
 * The token is held in memory only, not localStorage. A token in localStorage is
 * readable by any script that gets injected, and survives logout in a way that is
 * easy to forget. The cost is that a page reload requires logging in again,
 * which for a single-operator tool is the right trade.
 */

const BASE = '/api'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

let token: string | null = null

export function setToken(value: string | null): void {
  token = value
}

export function hasToken(): boolean {
  return token !== null
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)

  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, { ...init, headers })
  } catch (cause) {
    // A network failure is not an API error; distinguishing them matters because
    // one means "the server said no" and the other means "never reached it".
    throw new ApiError(0, `Cannot reach the API: ${(cause as Error).message}`)
  }

  if (!response.ok) {
    let detail = `HTTP ${response.status}`
    try {
      const body = (await response.json()) as { detail?: string }
      if (body?.detail) detail = body.detail
    } catch {
      /* non-JSON error body; keep the status */
    }
    throw new ApiError(response.status, detail)
  }
  return (await response.json()) as T
}

// --- types ------------------------------------------------------------------

export interface Device {
  site: string
  block: string
  inverter_id: string
  model: string
  ac_power_w: number
  dc_power_w: number
  ac_voltage_v: number
  ac_current_a: number
  efficiency: number
  heatsink_temp_c: number
  internal_temp_c: number
  uptime_s: number
  status_code: number
  clipping: boolean
  time?: string
}

export interface NowResponse {
  source: string
  site: string
  count: number
  devices: Device[]
}

export interface SeriesPoint {
  time: string
  value: number
  inverter_id?: string
  block?: string
  string_id?: string
  station_id?: string
}

export interface SeriesResponse {
  metric: string
  interval: string
  group_by: string | null
  count: number
  points: SeriesPoint[]
}

export interface SummaryResponse {
  /** Timestamp of the rollup sample, RFC 3339. Null when no data in window. */
  rollup_time: string | null
  /** Numeric only -- a non-number here would render as a raw string in a tile. */
  rollup: Record<string, number> | null
  devices: Array<{
    inverter_id: string
    avg_ac_power_w: number
    peak_ac_power_w: number
    max_heatsink_temp_c: number
    avg_efficiency: number
    clipped_samples: number
  }>
}

export interface StringRow {
  inverter_id: string
  string_count: number
  min_dc_power_w: number
  max_dc_power_w: number
  imbalance_ratio: number
}

export interface EventRow {
  time: string
  severity: string
  source: string
  code: string
  message: string
  value: number | null
  threshold: number | null
}

export interface Meta {
  site: string
  tables: string[]
  metrics: Record<string, string[]>
  dimensions: Record<string, string[]>
  intervals: string[]
  severities: string[]
}

// --- alerting ---------------------------------------------------------------

export interface Alert {
  rule_id: string
  subject: string
  scope: 'inverter' | 'site'
  severity: 'info' | 'warning' | 'critical'
  message: string
  value: number | null
  threshold: number | null
  fired_at: number
  since: number
  resolved_at: number | null
  active: boolean
}

export interface AlertsResponse {
  site: string
  count: number
  counts: Record<'critical' | 'warning' | 'info', number>
  /** False when the engine cannot reach MQTT: alerting is silently dead. */
  engine_connected: boolean
  alerts: Alert[]
}

export interface AlertStats {
  site: string
  engine_connected: boolean
  active: number
  received: number
  alerts_fired: number
  resolutions: number
  errors: number
  /** Writes shed because InfluxDB could not keep up. */
  dropped: number
}

export interface AlertRule {
  id: string
  description: string
  scope: string
  severity: string
  kind: 'threshold' | 'staleness'
  debounce_s: number
  stale_after_s: number
  conditions: Array<{ metric: string; operator: string; threshold: number }>
}

// --- endpoints --------------------------------------------------------------

export const api = {
  // `role` comes back on the token response itself, so the UI knows what the session
  // can do without a second round trip on every page load. The API is the only thing
  // that enforces it; this is for hiding controls the user cannot use.
  login: (username: string, password: string) =>
    request<{ token: string; expires_in: number; role: string }>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),

  now: () => request<NowResponse>('/now'),

  summary: (start?: string, end?: string) => {
    const q = new URLSearchParams()
    if (start) q.set('start', start)
    if (end) q.set('end', end)
    return request<SummaryResponse>(`/summary${q.toString() ? `?${q}` : ''}`)
  },

  series: (params: {
    table: string
    metric: string
    interval: string
    group_by?: string
    start?: string
    end?: string
  }) => {
    const q = new URLSearchParams({
      table: params.table,
      metric: params.metric,
      interval: params.interval,
    })
    if (params.group_by) q.set('group_by', params.group_by)
    if (params.start) q.set('start', params.start)
    if (params.end) q.set('end', params.end)
    return request<SeriesResponse>(`/series?${q}`)
  },

  strings: (start?: string, end?: string) => {
    const q = new URLSearchParams()
    if (start) q.set('start', start)
    if (end) q.set('end', end)
    return request<{ inverters: StringRow[] }>(`/strings${q.toString() ? `?${q}` : ''}`)
  },

  events: (severity?: string[]) => {
    const q = new URLSearchParams()
    for (const s of severity ?? []) q.append('severity', s)
    return request<{ events: EventRow[] }>(`/events${q.toString() ? `?${q}` : ''}`)
  },

  meta: () => request<Meta>('/meta'),

  /**
   * Single-use ticket for the live WebSocket.
   *
   * The JWT cannot go in the query string: a WebSocket handshake carries no
   * Authorization header, and a token in a URL ends up in access logs, proxy
   * logs and browser history. This hands back a 30-second, one-shot token
   * instead, so the long-lived credential never leaves a header.
   */
  liveTicket: () =>
    request<{ ticket: string; expires_in: number; path: string; stream: boolean }>(
      '/live-ticket',
      { method: 'POST' },
    ),

  /** Live alerts held in the engine's memory. */
  alerts: (severity?: string[]) => {
    const q = new URLSearchParams()
    for (const s of severity ?? []) q.append('severity', s)
    return request<AlertsResponse>(`/alerts${q.toString() ? `?${q}` : ''}`)
  },

  alertStats: () => request<AlertStats>('/alert-stats'),

  alertRules: () => request<{ site: string; count: number; rules: AlertRule[] }>('/alert-rules'),
}

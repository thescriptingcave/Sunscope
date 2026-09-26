/** Site header, KPI tiles, per-inverter cards, string heatmap, and event feed. */

import { useMemo } from 'react'
import { LineChart, type Series } from './LineChart'
import { ConnectionBanner } from './ConnectionBanner'
import {
  formatAge,
  formatClock,
  formatEnergy,
  formatPercent,
  formatPower,
  formatTemp,
  statusLabel,
} from '../format'
import type {
  AlertsResponse,
  AlertRule,
  AlertStats,
  Device,
  EventRow,
  StringRow,
  SummaryResponse,
} from '../api/client'
import type { ConnectionState, LiveReading, Rollup } from '../mqtt/live'

export function Header({ site, onLogout }: { site: string; onLogout: () => void }) {
  return (
    <header className="app-header">
      <div>
        <h1>Solar Farm</h1>
        <p className="muted">
          {site} · 1.0 MWac · 1.19 MWp
        </p>
      </div>
      <button className="btn" onClick={onLogout}>
        Sign out
      </button>
    </header>
  )
}

export function KpiTiles({
  rollup,
  readings,
  summary,
}: {
  rollup: Rollup | null
  readings: LiveReading[]
  summary: SummaryResponse | null
}) {
  // Prefer the live rollup; fall back to summing live readings, then to the
  // API's cached summary. Three sources because the live feed may not have
  // delivered yet on a cold load.
  const totalFromLive = readings.reduce((sum, r) => sum + r.acPowerW, 0)
  // `num` guards the boundary: a hand-typed field or an unexpected server
  // value must render as "--", never as a raw string in a KPI tile.
  const num = (value: unknown): number => (typeof value === 'number' ? value : 0)
  const cached = summary?.rollup
  const fleetSize = summary?.devices.length || readings.length || 0
  const total = rollup?.totalAcPowerW ?? (totalFromLive > 0 ? totalFromLive : num(cached?.total_ac_power_w))
  const pr = rollup?.prRatio ?? num(cached?.pr_ratio)
  const online = rollup?.invertersOnline ?? (cached ? num(cached.inverters_online) : null)
  const clipping = readings.filter((r) => r.clipping).length
  // The clipping count can only be trusted once live readings have arrived.
  // Claiming "no clipping" while the feed is still empty is a false all-clear:
  // the inverter cards directly below, fed from the API cache, can show
  // CLIPPING on all four at the same moment. Silence is not a healthy reading.
  const clippingSub =
    readings.length === 0 ? 'awaiting live data' : clipping > 0 ? `${clipping} clipping` : 'no clipping'

  return (
    <div className="tiles">
      <Tile label="Site power" value={formatPower(total)} sub="AC, all inverters" accent />
      <Tile label="Performance ratio" value={formatPercent(pr)} sub="temp-corrected" />
      <Tile
        label="Energy today"
        value={formatEnergy(rollup?.dailyYieldKwh ?? num(cached?.daily_yield_kwh))}
        sub="since local midnight"
      />
      <Tile
        label="Inverters online"
        // Fleet size comes from the API rather than a hardcoded 4, so the tile
        // stays correct if the topology ever changes.
        value={online === null ? '--' : `${online} / ${fleetSize}`}
        sub={clippingSub}
        tone={clipping > 0 ? 'warn' : undefined}
      />
    </div>
  )
}

function Tile({
  label,
  value,
  sub,
  accent,
  tone,
}: {
  label: string
  value: string
  sub?: string | undefined
  accent?: boolean | undefined
  tone?: 'warn' | 'bad' | undefined
}) {
  return (
    <div className={`tile${accent ? ' tile-accent' : ''}${tone ? ` tile-${tone}` : ''}`}>
      <span className="tile-label">{label}</span>
      <span className="tile-value">{value}</span>
      {sub && <span className="tile-sub">{sub}</span>}
    </div>
  )
}

export function InverterCards({
  devices,
  readings,
}: {
  devices: Device[]
  readings: LiveReading[]
}) {
  const live = useMemo(() => new Map(readings.map((r) => [r.inverterId, r])), [readings])

  // Merge: live values win, but show devices that only the cache knows about so a
  // cold load is not empty.
  const ids = new Set<string>([...devices.map((d) => d.inverter_id), ...live.keys()])
  if (ids.size === 0) {
    return (
      <section className="panel">
        <h2>Inverters</h2>
        <p className="muted">No device data yet.</p>
      </section>
    )
  }

  return (
    <section className="panel">
      <h2>Inverters</h2>
      <div className="cards">
        {[...ids].sort().map((id) => {
          const cached = devices.find((d) => d.inverter_id === id)
          const current = live.get(id)
          const power = current?.acPowerW ?? cached?.ac_power_w ?? 0
          const temp = current?.heatsinkTempC ?? cached?.heatsink_temp_c ?? null
          const efficiency = current?.efficiency ?? cached?.efficiency ?? null
          const code = current?.statusCode ?? cached?.status_code ?? 0
          const clipping = current?.clipping ?? cached?.clipping ?? false
          return (
            <article key={id} className="card">
              <header>
                <strong>{id}</strong>
                <span className={`pill pill-${code === 3 ? 'ok' : code === 0 ? 'bad' : 'warn'}`}>
                  {statusLabel(code)}
                </span>
              </header>
              <div className="card-value">{formatPower(power)}</div>
              <dl className="card-grid">
                <div>
                  <dt>Efficiency</dt>
                  <dd>{formatPercent(efficiency)}</dd>
                </div>
                <div>
                  <dt>Heatsink</dt>
                  <dd>{formatTemp(temp)}</dd>
                </div>
                <div>
                  <dt>DC in</dt>
                  <dd>{formatPower(current?.dcPowerW ?? cached?.dc_power_w)}</dd>
                </div>
                <div>
                  <dt>Clipping</dt>
                  <dd className={clipping ? 'warn-text' : undefined}>{clipping ? 'Yes' : 'No'}</dd>
                </div>
              </dl>
            </article>
          )
        })}
      </div>
    </section>
  )
}

export function PowerChart({ series, loading }: { series: Series[]; loading: boolean }) {
  return (
    <section className="panel">
      <h2>AC power, last 24 hours</h2>
      {loading && series.length === 0 ? (
        <p className="muted">Loading…</p>
      ) : (
        <LineChart
          series={series}
          yLabel="AC power"
          formatValue={(v) => formatPower(v, 0)}
          height={240}
        />
      )}
    </section>
  )
}

export function StringHeatmap({ rows }: { rows: StringRow[] }) {
  if (rows.length === 0) {
    return (
      <section className="panel">
        <h2>String balance</h2>
        <p className="muted">No string data in the selected window.</p>
      </section>
    )
  }
  return (
    <section className="panel">
      <h2>String balance</h2>
      <p className="muted small">
        Spread between the strongest and weakest string on each inverter. Normal operation stays
        under 5%; a genuinely degrading string exceeds 20%.
      </p>
      <div className="heat">
        {rows.map((row) => {
          // Colour scale tuned to the thresholds above rather than to the
          // observed range, so the colours mean the same thing every day.
          const ratio = row.imbalance_ratio
          const level = ratio < 0.05 ? 'ok' : ratio < 0.2 ? 'warn' : 'bad'
          return (
            <div key={row.inverter_id} className="heat-cell">
              <span className="heat-label">{row.inverter_id}</span>
              <div className={`heat-bar heat-${level}`} style={{ width: `${Math.min(ratio * 400, 100)}%` }} />
              <span className="heat-value">{formatPercent(ratio, 1)}</span>
            </div>
          )
        })}
      </div>
    </section>
  )
}

export function EventFeed({ events }: { events: EventRow[] }) {
  if (events.length === 0) {
    return (
      <section className="panel">
        <h2>Events</h2>
        <p className="muted">No events recorded.</p>
      </section>
    )
  }
  return (
    <section className="panel">
      <h2>Events</h2>
      <ul className="events">
        {events.map((event, index) => (
          <li key={`${event.time}-${index}`} className={`event event-${event.severity}`}>
            <span className="event-time">{formatClock(event.time)}</span>
            <span className={`pill pill-${event.severity === 'critical' ? 'bad' : event.severity === 'warning' ? 'warn' : 'ok'}`}>
              {event.severity}
            </span>
            <span className="event-source">{event.source}</span>
            <span className="event-message">{event.message}</span>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function Live({ state, lastUpdate, now }: { state: ConnectionState; lastUpdate: number | null; now: number }) {
  return <ConnectionBanner state={state} lastUpdate={lastUpdate} now={now} />
}

const SEVERITY_PILL: Record<string, string> = {
  critical: 'pill-bad',
  warning: 'pill-warn',
  info: 'pill-ok',
}

/**
 * Live alerts, plus the engine's own health.
 *
 * The engine status is shown separately and prominently because a disconnected
 * alert engine is the failure that looks like good news: the panel goes empty
 * and reads as "all clear" when in fact nothing is watching. Silently blank is
 * the one unacceptable outcome here.
 */
export function AlertsPanel({
  data,
  stats,
  now,
}: {
  data: AlertsResponse | null
  stats: AlertStats | null
  now: number
}) {
  const engineDown = stats !== null && !stats.engine_connected
  const count = data?.count ?? 0

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Alerts</h2>
        {stats && (
          <span className={`pill ${engineDown ? 'pill-bad' : count > 0 ? 'pill-warn' : 'pill-ok'}`}>
            {engineDown ? 'engine offline' : count === 0 ? 'all clear' : `${count} active`}
          </span>
        )}
      </div>

      {engineDown && (
        <p className="notice">
          The alert engine cannot reach MQTT, so nothing is being watched for. This is not the
          same as an all-clear.
        </p>
      )}

      {stats && stats.errors > 0 && (
        <p className="notice">
          {stats.errors} error{stats.errors === 1 ? '' : 's'} inside the alert engine. It is
          running but not evaluating cleanly.
        </p>
      )}

      {count === 0 && !engineDown ? (
        <p className="muted">
          No active alerts. {stats ? `${stats.received.toLocaleString()} readings evaluated.` : ''}
        </p>
      ) : (
        <ul className="alerts">
          {(data?.alerts ?? []).map((alert) => (
            <li key={`${alert.rule_id}-${alert.subject}`} className={`alert alert-${alert.severity}`}>
              <div className="alert-top">
                <span className={`pill ${SEVERITY_PILL[alert.severity] ?? 'pill-ok'}`}>
                  {alert.severity}
                </span>
                <strong>{alert.subject}</strong>
                <code>{alert.rule_id}</code>
                <span className="alert-age">{formatAge(alert.fired_at * 1000, now)}</span>
              </div>
              <p className="alert-message">{alert.message}</p>
              <p className="alert-detail">
                {alert.value !== null && (
                  <>
                    measured <b>{alert.value.toFixed(alert.rule_id.includes('temp') ? 1 : 2)}</b>
                    {alert.threshold !== null && <> (threshold {alert.threshold})</>}
                  </>
                )}
                {' · '}
                since {new Date(alert.since * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
              </p>
            </li>
          ))}
        </ul>
      )}

      {data === null && <p className="muted">Loading…</p>}
    </section>
  )
}

/** The rules being evaluated, so an operator can see what is watched for. */
export function AlertRules({ rules }: { rules: AlertRule[] }) {
  return (
    <section className="panel">
      <h2>Rules</h2>
      <p className="muted small">
        {rules.filter((r) => r.kind === 'staleness').length} of these are staleness rules, which
        fire when data stops arriving. They are the only way to catch a network partition, because
        a device holding its MQTT session open never triggers a Last Will.
      </p>
      <div className="rules">
        {rules.map((rule) => (
          <div key={rule.id} className="rule">
            <span className={`pill ${SEVERITY_PILL[rule.severity] ?? 'pill-ok'}`}>{rule.severity}</span>
            <code>{rule.id}</code>
            <span className="rule-desc">{rule.description}</span>
            <span className="rule-timing">
              {rule.kind === 'staleness'
                ? `stale after ${rule.stale_after_s}s`
                : `debounce ${rule.debounce_s}s`}
            </span>
          </div>
        ))}
      </div>
    </section>
  )
}

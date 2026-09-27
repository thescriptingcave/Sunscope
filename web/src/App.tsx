import { useEffect, useMemo, useState } from 'react'
import { api, type AlertsResponse, type AlertRule, type AlertStats, type EventRow, type StringRow, type SummaryResponse } from './api/client'
import { useAuth, useLiveFeed, useNow, usePolling } from './hooks'
import type { Series } from './components/LineChart'
import {
  AlertRules,
  AlertsPanel,
  EventFeed,
  Header,
  InverterCards,
  KpiTiles,
  Live,
  PowerChart,
  StringHeatmap,
} from './components/panels'
import './styles.css'

export default function App() {
  const { token, role, error: authError, busy, login, logout } = useAuth()

  if (!token) {
    return (
      <div className="login-shell">
        <LoginForm onSubmit={login} busy={busy} error={authError} />
      </div>
    )
  }
  return <Dashboard onLogout={logout} role={role} />
}

function LoginForm({
  onSubmit,
  busy,
  error,
}: {
  onSubmit: (u: string, p: string) => Promise<boolean>
  busy: boolean
  error: string | null
}) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')

  return (
    <form
      className="login"
      onSubmit={(e) => {
        e.preventDefault()
        void onSubmit(username, password)
      }}
    >
      <h1>Solar Farm</h1>
      <p className="muted">Sign in to view live telemetry</p>
      <label>
        Username
        <input
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          autoComplete="username"
          required
        />
      </label>
      <label>
        Password
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
        />
      </label>
      {error && <p className="error">{error}</p>}
      <button className="btn btn-primary" type="submit" disabled={busy}>
        {busy ? 'Signing in…' : 'Sign in'}
      </button>
    </form>
  )
}

function Dashboard({ onLogout, role }: { onLogout: () => void; role: string | null }) {
  const { readings, rollup, state, lastUpdate } = useLiveFeed()
  const { devices, stale } = useNow(true)

  // Re-render on a timer so relative timestamps ("12s ago") and the staleness
  // check stay honest even when no MQTT traffic arrives.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 5000)
    return () => window.clearInterval(timer)
  }, [])

  const summary = usePolling<SummaryResponse>(() => api.summary(), 60_000, true)
  const strings = usePolling<{ inverters: StringRow[] }>(() => api.strings(), 120_000, true)
  const events = usePolling<{ events: EventRow[] }>(() => api.events(), 60_000, true)
  // Alerts poll faster than history. A fault that resolves should stop drawing
  // attention quickly, and a fresh alert should appear without waiting a minute.
  const alerts = usePolling<AlertsResponse>(() => api.alerts(), 15_000, true)
  const alertStats = usePolling<AlertStats>(() => api.alertStats(), 15_000, true)
  const alertRules = usePolling<{ count: number; rules: AlertRule[] }>(
    () => api.alertRules(),
    300_000,
    true,
  )

  const powerSeries = usePolling(() => {
    const end = new Date()
    const start = new Date(end.getTime() - 24 * 3600 * 1000)
    return api.series({
      table: 'inverter_telemetry',
      metric: 'ac_power_w',
      interval: '1h',
      group_by: 'inverter_id',
      start: start.toISOString().replace(/\.\d{3}Z$/, 'Z'),
      end: end.toISOString().replace(/\.\d{3}Z$/, 'Z'),
    })
  }, 300_000, true)

  const series: Series[] = useMemo(() => {
    const points = powerSeries.data?.points ?? []
    const byInverter = new Map<string, Series>()
    for (const point of points) {
      const key = point.inverter_id ?? 'site'
      const at = new Date(point.time).getTime()
      if (Number.isNaN(at)) continue
      let entry = byInverter.get(key)
      if (!entry) {
        entry = { key, label: key, colour: '', points: [] }
        byInverter.set(key, entry)
      }
      entry.points.push({ x: at, y: Number(point.value) || 0 })
    }
    return [...byInverter.values()].sort((a, b) => a.label.localeCompare(b.label))
  }, [powerSeries.data])

  const site = 'mojave'

  return (
    <div className="app">
      <Header site={site} onLogout={onLogout} role={role} />

      <main className="app-main">
        <Live state={state} lastUpdate={lastUpdate} now={now} />

        {stale && (
          <p className="notice">
            The device cache is empty — nothing has been published recently. Live values will
            appear below as soon as the simulator publishes.
          </p>
        )}

        <KpiTiles rollup={rollup} readings={readings} summary={summary.data} />

        <AlertsPanel data={alerts.data} stats={alertStats.data} now={now} />

        <PowerChart series={series} loading={powerSeries.loading} />

        <InverterCards devices={devices} readings={readings} />

        <StringHeatmap rows={strings.data?.inverters ?? []} />

        <EventFeed events={events.data?.events ?? []} />

        {alertRules.data && <AlertRules rules={alertRules.data.rules} />}

        {summary.error && <p className="error">API: {summary.error}</p>}
        {alerts.error && <p className="error">API: {alerts.error}</p>}
        {strings.error && <p className="error">API: {strings.error}</p>}
        {events.error && <p className="error">API: {events.error}</p>}
      </main>
    </div>
  )
}

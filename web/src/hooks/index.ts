import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, setToken, type NowResponse } from '../api/client'
import { liveFeed, type ConnectionState, type LiveReading, type Rollup } from '../mqtt/live'

// --- auth -------------------------------------------------------------------

export function useAuth() {
  const [token, setTokenState] = useState<string | null>(null)
  const [role, setRole] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const login = useCallback(async (username: string, password: string) => {
    setBusy(true)
    setError(null)
    try {
      const result = await api.login(username, password)
      setToken(result.token)
      setTokenState(result.token)
      setRole(result.role)
      return true
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Login failed')
      return false
    } finally {
      setBusy(false)
    }
  }, [])

  const logout = useCallback(() => {
    setToken(null)
    setTokenState(null)
    // Cleared with the token, not left behind: a stale role surviving a sign-out is the
    // kind of thing that later gets mistaken for a permission that outlives the session.
    setRole(null)
  }, [])

  return { token, role, error, busy, login, logout }
}

// --- live feed -------------------------------------------------------------

/**
 * Subscribe to the live feed.
 *
 * The feed is a module-level singleton rather than something created per
 * component, so all views share one MQTT connection instead of one each.
 */
export function useLiveFeed(): {
  readings: LiveReading[]
  rollup: Rollup | null
  state: ConnectionState
  lastUpdate: number | null
} {
  // `tick` exists purely to force a re-render when the feed emits; the feed
  // itself is a mutable singleton, so React has no other way to notice.
  const [, setTick] = useState(0)
  const [lastUpdate, setLastUpdate] = useState<number | null>(null)

  useEffect(() => {
    const unsubscribe = liveFeed.subscribe(() => {
      setTick((v) => v + 1)
      const snap = liveFeed.snapshot
      setLastUpdate(snap.readings.length || snap.rollup ? snap.at : null)
    })
    void liveFeed.connect()
    return () => {
      unsubscribe()
      // The connection is intentionally left open: navigating between views
      // should not churn the broker session.
    }
  }, [])

  const snap = liveFeed.snapshot
  return { readings: snap.readings, rollup: snap.rollup, state: liveFeed.connectionState, lastUpdate }
}

// --- cold-load state -------------------------------------------------------

/**
 * Current device state from the API.
 *
 * This is the path that makes a cold load useful: the Last Value Cache is an
 * in-memory structure on the server, so it answers in milliseconds instead of
 * aggregating hours of Parquet.
 *
 * It can legitimately return zero devices -- the cache has a 30 minute TTL, so
 * if nothing has been published recently it is simply empty. That is not an
 * error and must not be rendered as one; the live feed fills in as soon as the
 * simulator publishes again.
 */
export function useNow(enabled: boolean): {
  devices: NowResponse['devices']
  loading: boolean
  error: string | null
  stale: boolean
  reload: () => void
} {
  const [devices, setDevices] = useState<NowResponse['devices']>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    setLoading(true)
    api
      .now()
      .then((response) => {
        if (cancelled) return
        setDevices(response.devices)
        setError(null)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof ApiError ? err.message : 'Failed to load state')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [enabled, tick])

  return { devices, loading, error, stale: !loading && devices.length === 0, reload: () => setTick((t) => t + 1) }
}

// --- polling hook ----------------------------------------------------------

export function usePolling<T>(
  loader: () => Promise<T>,
  intervalMs: number,
  enabled: boolean,
): { data: T | null; error: string | null; loading: boolean } {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const loaderRef = useRef(loader)
  loaderRef.current = loader

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    const load = () => {
      setLoading(true)
      loaderRef
        .current()
        .then((value) => {
          if (cancelled) return
          setData(value)
          setError(null)
        })
        .catch((err: unknown) => {
          if (cancelled) return
          setError(err instanceof ApiError ? err.message : 'Request failed')
        })
        .finally(() => {
          if (!cancelled) setLoading(false)
        })
    }
    load()
    const timer = window.setInterval(load, intervalMs)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [intervalMs, enabled])

  return { data, error, loading }
}

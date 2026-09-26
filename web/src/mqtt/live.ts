/**
 * Live telemetry over MQTT.
 *
 * Connects straight to EMQX's WebSocket listener rather than going through the
 * API, so live updates do not depend on the backend being up and the path stays
 * sub-second.
 *
 * That works while the page is served over http://localhost, because localhost
 * is a secure context. It will NOT work once the app is served over HTTPS: the
 * browser blocks a `ws://` connection from an `https://` page as mixed content,
 * and the fix is not `wss://` on this endpoint, because that would mean
 * exposing EMQX. The answer is to move this fan-out server-side behind the API
 * (see docs/04-security.md). This module is the seam where that swap happens.
 */

/**
 * Live telemetry, relayed from the API's own MQTT subscription.
 *
 * WHY THIS IS NOT A BROWSER -> BROKER CONNECTION
 *
 * The dashboard used to open its own WebSocket straight to EMQX. That works on
 * http://localhost, because a browser treats localhost as a secure context. The
 * moment the page is served over https the browser blocks the ws:// connection
 * as mixed content, and there is no way around it from the client: not wss://,
 * because that would mean putting a TLS terminator in front of the broker, and
 * not a rewrite, because the block happens before anything is sent.
 *
 * So the browser no longer talks to the broker at all. It opens a same-origin
 * socket to the API, which already holds an MQTT subscription for the alert
 * engine, and relays. That removes the mixed-content problem outright, keeps the
 * broker unexposed, and costs one extra hop on the path that must feel instant.
 *
 * The socket is authenticated with a single-use ticket rather than the JWT: a
 * browser cannot set an Authorization header on a WebSocket handshake, and
 * putting the long-lived token in a query string would leak it into access logs,
 * proxy logs and history.
 *
 * Frames are `{type, payload}` with type one of hello | reading | status |
 * rollup | weather. The payload is the raw telemetry object, unchanged from the
 * MQTT wire format, so this path stays a thin contract.
 */

import { api } from '../api/client'

export interface LiveReading {
  inverterId: string
  acPowerW: number
  dcPowerW: number
  efficiency: number
  heatsinkTempC: number
  clipping: boolean
  statusCode: number
  at: number
}

export interface Rollup {
  totalAcPowerW: number
  prRatio: number
  dailyYieldKwh: number
  invertersOnline: number
  stringsOnline: number
  at: number
}

export type ConnectionState = 'connecting' | 'connected' | 'reconnecting' | 'offline'

interface Payload {
  ts?: string
  site?: string
  block?: string
  inverter_id?: string
  model?: string
  ac_power_w?: number
  dc_power_w?: number
  efficiency?: number
  heatsink_temp_c?: number
  clipping?: boolean
  status_code?: number
  total_ac_power_w?: number
  pr_ratio?: number
  daily_yield_kwh?: number
  inverters_online?: number
  strings_online?: number
}

interface Frame {
  type: 'hello' | 'reading' | 'status' | 'rollup' | 'weather'
  payload?: Payload
  subject?: string
  engine_connected?: boolean
}

export class LiveFeed {
  private socket: WebSocket | null = null
  private state: ConnectionState = 'offline'
  private readonly readings = new Map<string, LiveReading>()
  private rollup: Rollup | null = null
  private readonly listeners = new Set<() => void>()
  private retryDelay = 1000
  private stopped = false

  subscribe(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private emit(): void {
    for (const listener of this.listeners) listener()
  }

  get connectionState(): ConnectionState {
    return this.state
  }

  get snapshot(): { readings: LiveReading[]; rollup: Rollup | null; at: number } {
    return { readings: [...this.readings.values()], rollup: this.rollup, at: Date.now() }
  }

  /**
   * Open the relay.
   *
   * Each attempt mints a fresh ticket, because the previous one was consumed the
   * instant its socket opened. Retrying with a spent ticket would fail
   * identically every time, which reads as "the server is down" rather than
   * "the credential expired".
   */
  async connect(): Promise<void> {
    if (this.socket || this.stopped) return
    this.setState('connecting')

    let ticket: string
    try {
      ticket = (await api.liveTicket()).ticket
    } catch (err) {
      // Degraded but not fatal: cached state and history still render. It must
      // say so rather than looking like a quiet, healthy dashboard.
      console.error('could not obtain a live-feed ticket:', err)
      this.setState('offline')
      this.scheduleReconnect()
      return
    }

    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
    const url = `${scheme}://${window.location.host}/api/live?ticket=${encodeURIComponent(ticket)}`

    let socket: WebSocket
    try {
      socket = new WebSocket(url)
    } catch (err) {
      console.error('could not open the live socket:', err)
      this.setState('offline')
      this.scheduleReconnect()
      return
    }
    this.socket = socket

    socket.onopen = () => {
      this.retryDelay = 1000
      this.setState('connected')
    }
    socket.onmessage = (event) => {
      let frame: Frame
      try {
        frame = JSON.parse(String(event.data)) as Frame
      } catch {
        return // a malformed frame is not worth tearing the feed down for
      }
      if (frame.type === 'reading' || frame.type === 'rollup' || frame.type === 'status') {
        if (frame.payload) this.apply(frame.payload)
      }
    }
    socket.onerror = () => this.setState('reconnecting')
    socket.onclose = () => {
      if (this.socket === socket) this.socket = null
      if (this.stopped) {
        this.setState('offline')
        return
      }
      this.setState('reconnecting')
      this.scheduleReconnect()
    }
  }

  /** Exponential backoff, capped, so a restarting API is not hammered. */
  private scheduleReconnect(): void {
    if (this.stopped) return
    const delay = this.retryDelay
    this.retryDelay = Math.min(this.retryDelay * 2, 15000)
    window.setTimeout(() => {
      if (!this.stopped) void this.connect()
    }, delay)
  }

  private apply(payload: Payload): void {
    if (typeof payload.total_ac_power_w === 'number') {
      this.rollup = {
        totalAcPowerW: payload.total_ac_power_w,
        prRatio: payload.pr_ratio ?? 0,
        dailyYieldKwh: payload.daily_yield_kwh ?? 0,
        invertersOnline: payload.inverters_online ?? 0,
        stringsOnline: payload.strings_online ?? 0,
        at: Date.now(),
      }
      this.emit()
      return
    }

    if (payload.inverter_id && typeof payload.ac_power_w === 'number') {
      this.readings.set(payload.inverter_id, {
        inverterId: payload.inverter_id,
        acPowerW: payload.ac_power_w,
        dcPowerW: payload.dc_power_w ?? 0,
        efficiency: payload.efficiency ?? 0,
        heatsinkTempC: payload.heatsink_temp_c ?? 0,
        clipping: payload.clipping ?? false,
        statusCode: payload.status_code ?? 0,
        at: Date.now(),
      })
      this.emit()
    }
  }

  private setState(next: ConnectionState): void {
    if (this.state === next) return
    this.state = next
    this.emit()
  }

  /**
   * Close the socket for good.
   *
   * `stopped` is what distinguishes a deliberate shutdown from a drop: without
   * it `onclose` would treat this as a network failure and immediately schedule
   * a reconnect.
   */
  disconnect(): void {
    this.stopped = true
    const socket = this.socket
    this.socket = null
    socket?.close()
    this.setState('offline')
  }
}

export const liveFeed = new LiveFeed()

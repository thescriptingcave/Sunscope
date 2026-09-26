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

import type { MqttClient } from 'mqtt'

/**
 * mqtt.js is loaded dynamically.
 *
 * It is the bulk of the bundle (~150 kB gzipped) and it is not needed to paint
 * the first screen: the cold-load state comes from the API's Last Value Cache,
 * and the live feed connects a moment later. Splitting it keeps the critical
 * path small on a phone, which is the whole point of shipping this as a PWA.
 */
type MqttModule = typeof import('mqtt')

/** EMQX WebSocket listener, per docker-compose.yml (MQTT_WS_PORT). */
const WS_URL = (() => {
  if (import.meta.env.VITE_MQTT_WS) return import.meta.env.VITE_MQTT_WS as string
  const { protocol, hostname } = window.location
  // The dev server runs on :5173 while EMQX is on :8083, so the host is right
  // but the port is not. In production the app is same-origin with the API, and
  // 8083 is still the broker's port, so default to it either way.
  const scheme = protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${hostname}:8083/mqtt`
})()

const SITE = 'mojave'

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

export class LiveFeed {
  private client: MqttClient | null = null
  private state: ConnectionState = 'offline'
  private readonly readings = new Map<string, LiveReading>()
  private rollup: Rollup | null = null
  private readonly listeners = new Set<() => void>()

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

  private mqttModule: MqttModule | null = null

  async connect(): Promise<void> {
    if (this.client) return
    this.setState('connecting')

    if (!this.mqttModule) {
      try {
        this.mqttModule = await import('mqtt')
      } catch {
        // Without the broker client the app still works, showing cached state
        // and history. It is a degraded mode, not a fatal error.
        this.setState('offline')
        return
      }
    }

    this.client = this.mqttModule.connect(WS_URL, {
      // The broker has no authentication in the local dev stack; see
      // docs/04-security.md. It is bound to loopback for exactly this reason.
      reconnectPeriod: 2000,
      connectTimeout: 8000,
      clean: true,
    })

    this.client.on('connect', () => {
      this.setState('connected')
      this.client?.subscribe(
        [
          `solar/${SITE}/block/+/inverter/+/telemetry`,
          `solar/${SITE}/rollup`,
          `solar/${SITE}/block/+/inverter/+/status`,
        ],
        { qos: 0 },
      )
    })

    this.client.on('reconnect', () => this.setState('reconnecting'))
    this.client.on('offline', () => this.setState('reconnecting'))
    this.client.on('close', () => {
      // A closed socket is only a problem if we never reconnect; flag it so the
      // UI can distinguish "quiet" from "broken".
      if (this.state !== 'offline') this.setState('reconnecting')
    })
    this.client.on('error', () => this.setState('reconnecting'))

    this.client.on('message', (_topic, raw) => {
      let payload: Payload
      try {
        payload = JSON.parse(raw.toString()) as Payload
      } catch {
        return // a malformed message is not worth tearing the feed down for
      }
      this.apply(payload)
    })
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

  disconnect(): void {
    this.client?.end(true)
    this.client = null
    this.setState('offline')
  }
}

export const liveFeed = new LiveFeed()

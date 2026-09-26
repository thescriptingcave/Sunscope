/** Connection banner.
 *
 * Worth its own component because the distinction it draws matters: a *stale*
 * feed (broker unreachable, data frozen) looks identical to a *quiet* one unless
 * the UI says so. Silently showing frozen numbers as if they were live is the
 * worst failure mode a monitoring dashboard has.
 */

import { formatAge } from '../format'
import type { ConnectionState } from '../mqtt/live'

interface Props {
  state: ConnectionState
  lastUpdate: number | null
  now: number
}

const COPY: Record<ConnectionState, { label: string; tone: string; detail: string }> = {
  connecting: { label: 'Connecting', tone: 'warn', detail: 'Opening the live feed' },
  connected: { label: 'Live', tone: 'ok', detail: 'Streaming from MQTT' },
  reconnecting: { label: 'Reconnecting', tone: 'warn', detail: 'Lost the broker, retrying' },
  offline: { label: 'Offline', tone: 'bad', detail: 'No live connection' },
}

export function ConnectionBanner({ state, lastUpdate, now }: Props) {
  const copy = COPY[state]
  const age = formatAge(lastUpdate)
  // Data older than a few intervals means the feed is stale even if the socket
  // says connected: a broker can stay up while a publisher has stopped.
  const stale = state === 'connected' && lastUpdate !== null && now - lastUpdate > 90_000

  return (
    <div className={`banner banner-${stale ? 'warn' : copy.tone}`} role="status">
      <span className="banner-dot" aria-hidden="true" />
      <span className="banner-label">{stale ? 'Stale' : copy.label}</span>
      <span className="banner-detail">
        {stale ? 'No fresh data — the publisher may have stopped' : copy.detail}
      </span>
      {lastUpdate !== null && <span className="banner-age">updated {age}</span>}
    </div>
  )
}

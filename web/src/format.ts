/** Formatting helpers, kept in one place so units are consistent. */

export function formatPower(watts: number | null | undefined, digits = 1): string {
  if (watts === null || watts === undefined || Number.isNaN(watts)) return '--'
  if (Math.abs(watts) >= 1000) return `${(watts / 1000).toFixed(digits)} kW`
  return `${watts.toFixed(0)} W`
}

export function formatEnergy(kwh: number | null | undefined): string {
  if (kwh === null || kwh === undefined || Number.isNaN(kwh)) return '--'
  if (kwh >= 1000) return `${(kwh / 1000).toFixed(2)} MWh`
  return `${kwh.toFixed(1)} kWh`
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '--'
  return `${(value * 100).toFixed(digits)}%`
}

export function formatTemp(celsius: number | null | undefined): string {
  if (celsius === null || celsius === undefined || Number.isNaN(celsius)) return '--'
  return `${celsius.toFixed(1)}°C`
}

export function formatAge(timestamp: number | null | undefined, reference?: number): string {
  if (!timestamp) return '--'
  // `reference` lets a caller pass the same render-tick timestamp it uses
  // elsewhere, so every relative time on screen agrees with every other one.
  const now = reference ?? Date.now()
  const seconds = Math.max(0, Math.round((now - timestamp) / 1000))
  if (seconds < 2) return 'now'
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  return `${hours}h ago`
}

/** ISO timestamp -> local HH:MM, or '--' when unparseable. */
export function formatClock(iso: string | null | undefined): string {
  if (!iso) return '--'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '--'
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

/**
 * Status code -> label, mirroring the simulator's STATUS_CODES.
 * 0 offline, 1 standby, 3 producing, 4 derating, 5 fault.
 */
export function statusLabel(code: number | null | undefined): string {
  switch (code) {
    case 0:
      return 'Offline'
    case 1:
      return 'Standby'
    case 3:
      return 'Producing'
    case 4:
      return 'Derating'
    case 5:
      return 'Fault'
    default:
      return 'Unknown'
  }
}

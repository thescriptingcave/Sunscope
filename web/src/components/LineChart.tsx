/**
 * A small multi-series line chart, hand-rolled in SVG.
 *
 * Deliberately no charting dependency. The charts needed here are one thing:
 * a time series per inverter. A library would add weight, a config dialect, and
 * a version to keep current, in exchange for features this dashboard does not
 * use. SVG also means the whole thing scales cleanly from a 375px phone to a
 * desktop without a resize observer.
 */

import { useId, useMemo } from 'react'

export interface Series {
  key: string
  label: string
  colour: string
  points: Array<{ x: number; y: number }>
}

interface Props {
  series: Series[]
  height?: number
  yLabel?: string
  formatValue?: (value: number) => string
  /** Draw a horizontal marker, e.g. the plant's clipping threshold. */
  threshold?: { value: number; label: string }
}

const PALETTE = [
  '#38bdf8', // sky
  '#a78bfa', // violet
  '#34d399', // emerald
  '#fbbf24', // amber
  '#f87171', // red
  '#22d3ee', // cyan
]

export function LineChart({
  series,
  height = 220,
  yLabel,
  formatValue = (v) => v.toFixed(0),
  threshold,
}: Props) {
  const gradientId = useId()

  const bounds = useMemo(() => {
    const all = series.flatMap((s) => s.points)
    if (all.length === 0) return null
    const xs = all.map((p) => p.x)
    const ys = all.map((p) => p.y)
    const minX = Math.min(...xs)
    const maxX = Math.max(...xs)
    let minY = Math.min(...ys)
    let maxY = Math.max(...ys)
    if (threshold) {
      minY = Math.min(minY, threshold.value)
      maxY = Math.max(maxY, threshold.value)
    }
    // A flat series would otherwise divide by zero; give it a band so the line
    // renders through the middle instead of vanishing.
    if (maxY - minY < 1e-9) {
      minY -= 1
      maxY += 1
    }
    const pad = (maxY - minY) * 0.08
    return { minX, maxX, minY: minY - pad, maxY: maxY + pad }
  }, [series, threshold])

  if (!bounds) {
    return (
      <div className="chart-empty" style={{ height }}>
        No data yet
      </div>
    )
  }

  const { minX, maxX, minY, maxY } = bounds
  const spanX = maxX - minX || 1
  const spanY = maxY - minY || 1

  // viewBox is wider than the rendered element so the SVG scales down cleanly.
  const VB_W = 800
  const VB_H = height
  const PAD_L = 52
  const PAD_R = 8
  const PAD_T = 8
  const PAD_B = 22

  const px = (x: number) => PAD_L + ((x - minX) / spanX) * (VB_W - PAD_L - PAD_R)
  const py = (y: number) => PAD_T + (1 - (y - minY) / spanY) * (VB_H - PAD_T - PAD_B)

  const ticks = 4
  const yTicks = Array.from({ length: ticks + 1 }, (_, i) => minY + (spanY * i) / ticks)
  const timeTicks = 4
  const xTickValues = Array.from({ length: timeTicks + 1 }, (_, i) => minX + (spanX * i) / timeTicks)

  return (
    <div className="chart">
      <svg
        viewBox={`0 0 ${VB_W} ${VB_H}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={yLabel ?? 'time series'}
        style={{ height, width: '100%' }}
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#38bdf8" stopOpacity="0.28" />
            <stop offset="100%" stopColor="#38bdf8" stopOpacity="0" />
          </linearGradient>
        </defs>

        {yTicks.map((value, i) => (
          <g key={`y${i}`}>
            <line
              x1={PAD_L}
              x2={VB_W - PAD_R}
              y1={py(value)}
              y2={py(value)}
              stroke="rgba(148,163,184,0.14)"
              strokeWidth="1"
            />
            <text x={PAD_L - 8} y={py(value) + 4} className="chart-tick" textAnchor="end">
              {formatValue(value)}
            </text>
          </g>
        ))}

        {xTickValues.map((value, i) => (
          <text
            key={`x${i}`}
            x={px(value)}
            y={VB_H - 6}
            className="chart-tick"
            textAnchor={i === 0 ? 'start' : i === timeTicks ? 'end' : 'middle'}
          >
            {new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </text>
        ))}

        {threshold && (
          <g>
            <line
              x1={PAD_L}
              x2={VB_W - PAD_R}
              y1={py(threshold.value)}
              y2={py(threshold.value)}
              stroke="#fbbf24"
              strokeWidth="1.5"
              strokeDasharray="5 4"
              opacity="0.75"
            />
            <text x={VB_W - PAD_R} y={py(threshold.value) - 5} className="chart-threshold" textAnchor="end">
              {threshold.label}
            </text>
          </g>
        )}

        {series.map((s, index) => {
          if (s.points.length === 0) return null
          const colour = s.colour || PALETTE[index % PALETTE.length]!
          const path = s.points
            .map((p, i) => `${i === 0 ? 'M' : 'L'}${px(p.x).toFixed(2)},${py(p.y).toFixed(2)}`)
            .join(' ')
          const area =
            s.points.length > 1
              ? `${path} L${px(s.points[s.points.length - 1]!.x).toFixed(2)},${py(minY).toFixed(2)} L${px(s.points[0]!.x).toFixed(2)},${py(minY).toFixed(2)} Z`
              : ''
          return (
            <g key={s.key}>
              {index === 0 && area ? <path d={area} fill={`url(#${gradientId})`} /> : null}
              <path d={path} fill="none" stroke={colour} strokeWidth="2" strokeLinejoin="round" />
            </g>
          )
        })}
      </svg>

      {series.length > 1 && (
        <div className="legend">
          {series.map((s, index) => (
            <span key={s.key} className="legend-item">
              <i style={{ background: s.colour || PALETTE[index % PALETTE.length] }} />
              {s.label}
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

import L from 'leaflet'
import 'leaflet/dist/leaflet.css'
import { type ReactNode, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { now } from './format'

// -- routing: a handful of pages, so the History API is enough ---------------

const onNav = (fn: () => void) => {
  window.addEventListener('popstate', fn)
  return () => window.removeEventListener('popstate', fn)
}

export function usePath(): string {
  return useSyncExternalStore(onNav, () => window.location.pathname)
}

export function go(path: string) {
  window.history.pushState(null, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

export function Link({ to, children, className }: { to: string; children: ReactNode; className?: string }) {
  return (
    <a
      href={to}
      className={className}
      onClick={(e) => {
        if (e.metaKey || e.ctrlKey || e.shiftKey) return
        e.preventDefault()
        go(to)
      }}
    >
      {children}
    </a>
  )
}

// -- small pieces --------------------------------------------------------------

export function Tile({ label, value, unit, sub }: { label: string; value: ReactNode; unit?: string; sub?: ReactNode }) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className="value">
        {value}
        {unit && <small>{unit}</small>}
      </div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  )
}

/** Splits "21.3 km" into value and unit so tiles can set them differently. */
export function split(text: string): { value: string; unit?: string } {
  const i = text.indexOf(' ')
  return i < 0 ? { value: text } : { value: text.slice(0, i), unit: text.slice(i + 1) }
}

const PERIODS = [
  { key: '7d', label: '7 days', days: 7 },
  { key: '30d', label: '30 days', days: 30 },
  { key: '90d', label: '90 days', days: 90 },
  { key: '1y', label: 'Year', days: 365 },
  { key: 'all', label: 'All', days: 0 },
] as const

export type PeriodKey = (typeof PERIODS)[number]['key']

/** Period picker state, remembered across pages. Returns a query string for the API. */
export function usePeriod(initial: PeriodKey = '30d') {
  const [key, setKey] = useState<PeriodKey>(() => (sessionStorage.getItem('period') as PeriodKey) || initial)
  const query = useMemo(() => {
    const days = PERIODS.find((p) => p.key === key)?.days ?? 30
    // Rounded to the hour so the query string is stable between renders.
    return days ? `?from=${Math.floor((now() - days * 86400) / 3600) * 3600}` : ''
  }, [key])
  const picker = (
    <div className="seg" role="group" aria-label="Period">
      {PERIODS.map((p) => (
        <button
          key={p.key}
          className={p.key === key ? 'on' : ''}
          aria-pressed={p.key === key}
          onClick={() => {
            sessionStorage.setItem('period', p.key)
            setKey(p.key)
          }}
        >
          {p.label}
        </button>
      ))}
    </div>
  )
  return { query, picker }
}

export function Battery({ soc, target }: { soc: number; target?: number | null }) {
  return (
    <div className="meter" role="img" aria-label={`Battery ${Math.round(soc)} percent`}>
      <i className={soc < 15 ? 'low' : ''} style={{ width: `${Math.min(Math.max(soc, 0), 100)}%` }} />
      {target != null && target < 100 && <b style={{ left: `${target}%` }} title={`Charge limit ${target}%`} />}
    </div>
  )
}

// -- map -----------------------------------------------------------------------

type LatLon = [number, number]

export function MapView({ path, markers, dashed }: { path?: LatLon[]; markers?: { at: LatLon; label: string; end?: boolean }[]; dashed?: boolean }) {
  const el = useRef<HTMLDivElement>(null)
  const key = JSON.stringify([path, markers, dashed])

  useEffect(() => {
    if (!el.current) return
    const css = getComputedStyle(document.documentElement)
    const color = css.getPropertyValue('--series-1').trim()
    const map = L.map(el.current, { scrollWheelZoom: false, attributionControl: true })
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(map)
    const all: LatLon[] = [...(path ?? []), ...(markers ?? []).map((m) => m.at)]
    if (path && path.length > 1) {
      L.polyline(path, { color, weight: 4, opacity: 0.9, dashArray: dashed ? '6 8' : undefined }).addTo(map)
    }
    for (const m of markers ?? []) {
      L.circleMarker(m.at, {
        radius: 7, color: '#fff', weight: 2, fillColor: m.end ? color : '#52514e', fillOpacity: 1,
      }).addTo(map).bindTooltip(m.label)
    }
    if (all.length === 1) map.setView(all[0], 15)
    else if (all.length) map.fitBounds(L.latLngBounds(all), { padding: [28, 28], maxZoom: 16 })
    return () => {
      map.remove()
    }
  }, [key]) // eslint-disable-line react-hooks/exhaustive-deps

  return <div ref={el} className="map" />
}

/** A map to click a position on. Keeps its zoom while you move the pin. */
export function PickMap({ center, zoom = 15, value, onPick }: { center: LatLon; zoom?: number; value: LatLon | null; onPick: (at: LatLon) => void }) {
  const el = useRef<HTMLDivElement>(null)
  const pin = useRef<L.CircleMarker | null>(null)
  const map = useRef<L.Map | null>(null)
  const pick = useRef(onPick)
  pick.current = onPick

  useEffect(() => {
    if (!el.current) return
    const m = L.map(el.current, { scrollWheelZoom: true, attributionControl: true }).setView(center, zoom)
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    }).addTo(m)
    m.on('click', (e: L.LeafletMouseEvent) => pick.current([e.latlng.lat, e.latlng.lng]))
    map.current = m
    return () => {
      m.remove()
      map.current = null
      pin.current = null
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!map.current) return
    pin.current?.remove()
    pin.current = null
    if (value) {
      const color = getComputedStyle(document.documentElement).getPropertyValue('--series-1').trim()
      pin.current = L.circleMarker(value, { radius: 8, color: '#fff', weight: 2, fillColor: color, fillOpacity: 1 }).addTo(map.current)
    }
  }, [value?.[0], value?.[1]]) // eslint-disable-line react-hooks/exhaustive-deps

  return <div ref={el} className="map" style={{ cursor: 'crosshair' }} />
}

// -- chart tooltip ---------------------------------------------------------------

export function Tip({ active, payload, title, rows }: {
  active?: boolean
  payload?: { payload: any }[]
  title: (d: any) => string
  rows: (d: any) => { label: string; value: string; color?: string }[]
}) {
  if (!active || !payload?.length) return null
  const d = payload[0].payload
  return (
    <div className="tip">
      <div className="t">{title(d)}</div>
      {rows(d).map((r) => (
        <div key={r.label}>
          {r.color && <i style={{ background: r.color }} />}
          <span className="soft">{r.label}</span> <strong className="nums">{r.value}</strong>
        </div>
      ))}
    </div>
  )
}

export const axis = {
  tick: { fill: 'var(--muted)', fontSize: 12 },
  axisLine: { stroke: 'var(--axis)' },
  tickLine: false as const,
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>
}

export function useAction() {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<{ text: string; bad?: boolean } | null>(null)
  async function run(fn: () => Promise<string | void>) {
    setBusy(true)
    setMessage(null)
    try {
      const text = await fn()
      if (text) setMessage({ text })
    } catch (e) {
      setMessage({ text: (e as Error).message, bad: true })
    } finally {
      setBusy(false)
    }
  }
  const note = message && <span className={message.bad ? 'error' : 'hint'} role="status">{message.text}</span>
  return { busy, run, note }
}

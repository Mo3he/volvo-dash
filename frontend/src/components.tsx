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

// Line icons, drawn on a 24-unit grid.
const ICONS = {
  gauge: ['M12 14l4-4', 'M3.34 19a10 10 0 1 1 17.32 0'],
  route: ['M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15', 'c6 19 3', 'c18 5 3'],
  bolt: ['M13 2 3 14h9l-1 8 10-12h-9l1-8z'],
  chart: ['M3 3v18h18', 'M18 17V9', 'M13 17V5', 'M8 17v-3'],
  sliders: ['M4 21v-7', 'M4 10V3', 'M12 21v-9', 'M12 8V3', 'M20 21v-5', 'M20 12V3', 'M1 14h6', 'M9 8h6', 'M17 16h6'],
  lock: ['M5 11h14v11H5z', 'M7 11V7a5 5 0 0 1 10 0v4'],
  unlock: ['M5 11h14v11H5z', 'M7 11V7a5 5 0 0 1 9.9-1'],
  plug: ['M12 22v-5', 'M9 8V2', 'M15 8V2', 'M18 8v5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z'],
  pin: ['M20 10c0 6-8 12-8 12s-8-6-8-12a8 8 0 0 1 16 0Z', 'c12 10 3'],
  leaf: ['M11 20A7 7 0 0 1 9.8 6.1C15.5 5 17 4.48 19 2c1 2 2 4.18 2 8 0 5.5-4.78 10-10 10Z', 'M2 21c0-3 1.85-5.36 5.08-6'],
  battery: ['M2 7h16v10H2z', 'M22 11v2', 'M6 11v2'],
  refresh: ['M21 12a9 9 0 1 1-3-6.7L21 8', 'M21 3v5h-5'],
  clock: ['c12 12 10', 'M12 6v6l4 2'],
  car: ['M5 17h14', 'M3 17v-4l2-5a2 2 0 0 1 2-1h10a2 2 0 0 1 2 1l2 5v4', 'c7 17 2', 'c17 17 2', 'M3 13h18'],
  fan: ['c12 12 2', 'M12 10c0-4 1-7 4-7s3 4-2 7', 'M14 12c4 0 7 1 7 4s-4 3-7-2', 'M12 14c0 4-1 7-4 7s-3-4 2-7', 'M10 12c-4 0-7-1-7-4s4-3 7 2'],
  shield: ['M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z', 'M9 12l2 2 4-4'],
  activity: ['M22 12h-4l-3 9L9 3l-3 9H2'],
  sun: ['c12 12 4', 'M12 2v2', 'M12 20v2', 'M4.93 4.93l1.41 1.41', 'M17.66 17.66l1.41 1.41', 'M2 12h2', 'M20 12h2', 'M6.34 17.66l-1.41 1.41', 'M19.07 4.93l-1.41 1.41'],
  moon: ['M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z'],
  auto: ['c12 12 9', 'fM12 3a9 9 0 0 1 0 18z'],
} as const

export type IconName = keyof typeof ICONS

export function Icon({ name, className = 'icon' }: { name: IconName; className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.9}
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {ICONS[name].map((d) => {
        if (d.startsWith('f')) return <path key={d} d={d.slice(1)} fill="currentColor" stroke="none" />
        if (d.startsWith('c')) {
          const [cx, cy, r] = d.slice(1).split(' ').map(Number)
          return <circle key={d} cx={cx} cy={cy} r={r} />
        }
        return <path key={d} d={d} />
      })}
    </svg>
  )
}

export function Tile({ label, value, unit, sub, icon }: { label: string; value: ReactNode; unit?: string; sub?: ReactNode; icon?: IconName }) {
  return (
    <div className="tile">
      <div className="label">
        {icon && <span className="badge"><Icon name={icon} /></span>}
        {label}
      </div>
      <div className="value nums">
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

/** Battery level as a ring, with a tick at the charge limit. */
export function BatteryRing({ soc, target, children }: { soc: number; target?: number | null; children?: ReactNode }) {
  const r = 80
  const c = 2 * Math.PI * r
  const level = Math.min(Math.max(soc, 0), 100)
  // The svg is turned a quarter so 0% sits at the top; angles here are in that turned frame.
  const a = target != null && target < 100 ? (target / 100) * 2 * Math.PI : null
  const point = (dist: number) => [100 + Math.cos(a!) * dist, 100 + Math.sin(a!) * dist]
  const tick = a != null ? [point(r - 12), point(r + 12)] : null
  return (
    <div className="ring" role="img" aria-label={`Battery ${Math.round(soc)} percent${target != null ? `, charge limit ${target} percent` : ''}`}>
      <svg viewBox="0 0 200 200">
        <circle className="track" cx="100" cy="100" r={r} fill="none" strokeWidth="14" />
        <circle
          className={`fill${soc < 15 ? ' low' : ''}`} cx="100" cy="100" r={r} fill="none" strokeWidth="14"
          strokeLinecap="round" strokeDasharray={c} strokeDashoffset={c * (1 - Math.max(level, 1.5) / 100)}
        />
        {tick && <line className="limit" x1={tick[0][0]} y1={tick[0][1]} x2={tick[1][0]} y2={tick[1][1]} strokeWidth="3" strokeLinecap="round" />}
      </svg>
      <div className="ring-text">{children}</div>
    </div>
  )
}

type Theme = 'auto' | 'light' | 'dark'

/** Auto follows the device; the choice is remembered in this browser only. */
export function ThemeSwitch() {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      const t = localStorage.getItem('theme')
      return t === 'light' || t === 'dark' ? t : 'auto'
    } catch {
      return 'auto'
    }
  })
  const pick = (t: Theme) => {
    setTheme(t)
    if (t === 'auto') delete document.documentElement.dataset.theme
    else document.documentElement.dataset.theme = t
    try {
      if (t === 'auto') localStorage.removeItem('theme')
      else localStorage.setItem('theme', t)
    } catch {
      /* storage blocked: the choice lasts until reload */
    }
  }
  const options: [Theme, IconName, string][] = [['auto', 'auto', 'Auto, follow the device'], ['light', 'sun', 'Light'], ['dark', 'moon', 'Dark']]
  return (
    <div className="seg icons theme" role="group" aria-label="Theme">
      {options.map(([t, icon, label]) => (
        <button key={t} className={t === theme ? 'on' : ''} aria-pressed={t === theme} aria-label={label} title={label} onClick={() => pick(t)}>
          <Icon name={icon} />
        </button>
      ))}
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
export function PickMap({ center, zoom = 15, value, onPick, radius, others }: {
  center: LatLon
  zoom?: number
  value: LatLon | null
  onPick: (at: LatLon) => void
  /** Metres: drawn around the pin. */
  radius?: number
  /** Other places, shown faintly for reference. */
  others?: { at: LatLon; label: string; radius?: number }[]
}) {
  const el = useRef<HTMLDivElement>(null)
  const pin = useRef<L.LayerGroup | null>(null)
  const rest = useRef<L.LayerGroup | null>(null)
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
      rest.current = null
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const m = map.current
    if (!m) return
    pin.current?.remove()
    pin.current = null
    if (!value) return
    const color = getComputedStyle(document.documentElement).getPropertyValue('--series-1').trim()
    const g = L.layerGroup().addTo(m)
    if (radius) L.circle(value, { radius, color, weight: 1.5, fillOpacity: 0.12 }).addTo(g)
    L.circleMarker(value, { radius: 8, color: '#fff', weight: 2, fillColor: color, fillOpacity: 1 }).addTo(g)
    pin.current = g
    // Typed-in coordinates may be off screen: bring them into view.
    if (!m.getBounds().contains(value)) m.panTo(value)
  }, [value?.[0], value?.[1], radius]) // eslint-disable-line react-hooks/exhaustive-deps

  const othersKey = JSON.stringify(others ?? [])
  useEffect(() => {
    const m = map.current
    if (!m) return
    rest.current?.remove()
    const g = L.layerGroup().addTo(m)
    for (const o of others ?? []) {
      if (o.radius) L.circle(o.at, { radius: o.radius, color: '#8a8984', weight: 1, fillOpacity: 0.06, interactive: false }).addTo(g)
      L.circleMarker(o.at, { radius: 5, color: '#fff', weight: 1.5, fillColor: '#8a8984', fillOpacity: 1 }).addTo(g).bindTooltip(o.label)
    }
    rest.current = g
  }, [othersKey]) // eslint-disable-line react-hooks/exhaustive-deps

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

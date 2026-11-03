// Everything is stored in metric; conversion happens here, at display time.

const MILE = 1.609344

let units: 'km' | 'mi' = 'km'
let currency = '€'
let timeZone: string | undefined
let clockOffset = 0 // server time minus browser time, seconds

export function configure(s: { units: 'km' | 'mi'; currency: string; timezone: string }, serverNow: number) {
  units = s.units
  currency = s.currency
  timeZone = s.timezone && s.timezone !== 'UTC' ? s.timezone : undefined
  clockOffset = serverNow - Date.now() / 1000
}

/** Current time according to the server (the simulator can run fast). */
export const now = () => Date.now() / 1000 + clockOffset
export const distUnit = () => units

const num = (v: number, digits: number) =>
  v.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits })

export const dash = '–'

export function dist(km: number | null | undefined, digits = 1): string {
  if (km == null) return dash
  return `${num(units === 'mi' ? km / MILE : km, digits)} ${units}`
}

export function distValue(km: number): number {
  return units === 'mi' ? km / MILE : km
}

export function speed(kmh: number | null | undefined): string {
  if (kmh == null) return dash
  return units === 'mi' ? `${num(kmh / MILE, 0)} mph` : `${num(kmh, 0)} km/h`
}

/** Stored as kWh/100 km. Miles users get mi/kWh, where higher is better. */
export function efficiency(v: number | null | undefined): string {
  if (v == null || v <= 0) return dash
  return units === 'mi' ? `${num(100 / v / MILE, 1)} mi/kWh` : `${num(v, 1)} kWh/100\u00a0km`
}

export function efficiencyValue(v: number): number {
  return units === 'mi' ? 100 / v / MILE : v
}

export const efficiencyUnit = () => (units === 'mi' ? 'mi/kWh' : 'kWh/100\u00a0km')

export function kwh(v: number | null | undefined, digits = 1): string {
  return v == null ? dash : `${num(v, digits)} kWh`
}

export function kw(v: number | null | undefined): string {
  return v == null ? dash : `${num(v, v >= 20 ? 0 : 1)} kW`
}

export function pct(v: number | null | undefined): string {
  return v == null ? dash : `${Math.round(v)}%`
}

export function money(v: number | null | undefined): string {
  if (v == null) return dash
  const text = num(v, 2)
  return currency.length > 1 ? `${text} ${currency}` : `${currency}${text}`
}

export const currencySymbol = () => currency

export function temp(c: number | null | undefined): string {
  return c == null ? dash : `${num(c, 0)} °C`
}

export function duration(seconds: number | null | undefined): string {
  if (seconds == null) return dash
  const m = Math.max(Math.round(seconds / 60), 0)
  if (m < 60) return `${m} min`
  return `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, '0')} min`
}

const fmt = (opts: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat(undefined, { timeZone, ...opts })

export function time(ts: number | null | undefined): string {
  return ts == null ? dash : fmt({ hour: '2-digit', minute: '2-digit' }).format(ts * 1000)
}

export function day(ts: number): string {
  return fmt({ weekday: 'short', day: 'numeric', month: 'short' }).format(ts * 1000)
}

export function dayKey(ts: number): string {
  return fmt({ year: 'numeric', month: '2-digit', day: '2-digit' }).format(ts * 1000)
}

export function dateTime(ts: number | null | undefined): string {
  if (ts == null) return dash
  return fmt({ day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }).format(ts * 1000)
}

export function ago(ts: number | null | undefined): string {
  if (ts == null) return 'never'
  const s = Math.max(now() - ts, 0)
  if (s < 90) return 'just now'
  if (s < 3600) return `${Math.round(s / 60)} min ago`
  if (s < 86400) return `${Math.round(s / 3600)} h ago`
  return `${Math.round(s / 86400)} d ago`
}

/** Label for a stats bucket key like 2026-10-05. */
export function bucketLabel(key: string, unit: 'day' | 'week' | 'month'): string {
  const d = new Date(`${key}T12:00:00Z`)
  const f = (o: Intl.DateTimeFormatOptions) => new Intl.DateTimeFormat(undefined, { timeZone: 'UTC', ...o }).format(d)
  return unit === 'month' ? f({ month: 'short', year: '2-digit' }) : f({ day: 'numeric', month: 'short' })
}

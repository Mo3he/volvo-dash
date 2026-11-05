import { useCallback, useEffect, useRef, useState } from 'react'

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message)
  }
}

export async function api<T = any>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const r = await fetch(`/api${path}`, {
    method,
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (r.status === 401 && path !== '/login') window.dispatchEvent(new Event('signed-out'))
  if (!r.ok) {
    let message = `Request failed (${r.status})`
    try {
      message = (await r.json()).detail ?? message
    } catch {
      /* not JSON */
    }
    throw new ApiError(message, r.status)
  }
  return r.json()
}

// The server pushes a line whenever it has polled the car; views refetch on it.
const listeners = new Set<() => void>()
let source: EventSource | null = null

function subscribe(fn: () => void) {
  listeners.add(fn)
  if (!source) {
    source = new EventSource('/api/stream')
    source.onmessage = () => listeners.forEach((l) => l())
  }
  return () => {
    listeners.delete(fn)
    if (!listeners.size) {
      source?.close()
      source = null
    }
  }
}

/** Fetch `path`, and again whenever the server says something changed. */
export function useLive<T = any>(path: string | null): { data: T | undefined; error: string | null; reload: () => void } {
  const [data, setData] = useState<T>()
  const [error, setError] = useState<string | null>(null)
  const latest = useRef(path)
  latest.current = path

  const load = useCallback(() => {
    if (!path) return
    api<T>(path)
      .then((d) => {
        if (latest.current === path) {
          setData(d)
          setError(null)
        }
      })
      .catch((e) => setError(e.message))
  }, [path])

  useEffect(() => {
    load()
    return subscribe(load)
  }, [load])

  return { data, error, reload: load }
}

export interface Trip {
  id: number
  started_at: number
  ended_at: number
  start_estimated: number
  /** Null while the car hasn't reported how far it went. */
  distance_km: number | null
  soc_start: number | null
  soc_end: number | null
  energy_kwh: number | null
  energy_source: 'car' | 'soc' | null
  efficiency: number | null
  avg_speed_kmh: number | null
  start_lat: number | null
  start_lon: number | null
  end_lat: number | null
  end_lon: number | null
  start_place: string | null
  end_place: string | null
  start_manual: number
  end_manual: number
  /** Set when the user corrected the end time or distance by hand. */
  ended_override: number | null
  distance_override: number | null
  /** The end the car reported. */
  ended_car: number | null
  temp_c: number | null
  note: string | null
  points?: { ts: number; lat: number; lon: number }[]
}

export interface Charge {
  id: number
  started_at: number
  ended_at: number | null
  soc_start: number | null
  soc_end: number | null
  target_soc: number | null
  charge_type: 'AC' | 'DC' | null
  energy_soc_kwh: number | null
  energy_metered_kwh: number | null
  metered_coverage: number | null
  energy_kwh: number | null
  energy_source: 'metered' | 'soc' | null
  energy_billed_kwh: number | null
  max_power_kw: number | null
  avg_power_kw: number | null
  lat: number | null
  lon: number | null
  place_name: string | null
  price_per_kwh: number | null
  cost_override: number | null
  energy_override: number | null
  cost: number | null
  note: string | null
  samples?: { ts: number; power_w: number | null; soc: number | null }[]
  /** Driving from the end of this charge until the next one (or now). */
  after?: AfterCharge | null
}

export interface AfterCharge {
  ongoing: boolean
  until: number | null
  trips: number
  distance_km: number
  energy_kwh: number | null
  efficiency: number | null
  soc_from: number | null
  soc_to: number | null
  soc_used: number | null
  full_range_km: number | null
}

export interface Status {
  now: number
  settings: { vehicle_name: string; units: 'km' | 'mi'; currency: string; timezone: string }
  vehicle: { model: string | null; year: number | null; colour: string | null; vin_tail: string | null; capacity_kwh: number | null; image: string | null }
  connection: {
    configured: boolean
    connected: boolean
    auth_error: { at: number; message: string } | null
    consent_at: number | null
    /** When Volvo stops accepting the sign-in; refreshing normally pushes it on. */
    sign_in_until: number | null
    calls_today: number
    daily_limit: number
    unavailable: string[]
    commands: string[]
    sim: string | null
    mqtt: { state: 'off' | 'waiting' | 'connected' | 'error'; error: string | null; since: string | null }
    last_poll: number | null
    next_poll: number | null
    last_error: string | null
    paused: boolean
    mode: string
  }
  obs: Record<string, any> | null
  state: Record<string, { value: any; unit: string | null; ts: number | null }>
  trip: Trip | null
  charge: Charge | null
  last_trip: Trip | null
  last_charge: Charge | null
}

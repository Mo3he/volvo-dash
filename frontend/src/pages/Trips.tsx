import { useMemo, useState } from 'react'
import { api, useLive, type Trip } from '../api'
import { Empty, Icon, MapView, PickMap, Tile, split, useAction, usePeriod } from '../components'
import { day, dayKey, dist, duration, efficiency, kwh, pct, speed, temp, time } from '../format'

const place = (name: string | null) => name || 'Unknown place'

// No position at all means the car had no GPS there (an underground car park, say).
const spot = (t: Trip, at: 'start' | 'end') =>
  t[`${at}_lat`] == null ? 'no GPS' : place(t[`${at}_place`])

function route(t: Trip): string {
  if (t.start_place && t.start_place === t.end_place) return `${t.start_place}, round trip`
  return `${spot(t, 'start')} → ${spot(t, 'end')}`
}

export function Trips() {
  const { query, picker } = usePeriod()
  const { data: trips, reload: reloadTrips } = useLive<Trip[]>(`/trips${query}`)
  const { data: stats } = useLive<any>(`/stats${query}`)
  const [open, setOpen] = useState<number | null>(null)

  const days = useMemo(() => {
    const groups: { key: string; trips: Trip[] }[] = []
    for (const t of trips ?? []) {
      const key = dayKey(t.started_at)
      if (groups.at(-1)?.key === key) groups.at(-1)!.trips.push(t)
      else groups.push({ key, trips: [t] })
    }
    return groups
  }, [trips])

  const totals = stats?.totals
  const d = split(dist(totals?.distance_km, 0))
  const e = split(kwh(totals?.used_kwh, 0))
  const eff = split(efficiency(totals?.efficiency))

  return (
    <>
      <div className="page-head">
        <h1>Trips</h1>
        <div className="row">
          {picker}
          <a className="btn" href={`/api/export/trips.csv${query}`}>Export CSV</a>
        </div>
      </div>

      <div className="grid tiles">
        <Tile icon="route" label="Distance" value={d.value} unit={d.unit} sub={`${totals?.trips ?? 0} trips`} />
        <Tile icon="bolt" label="Energy used" value={e.value} unit={e.unit} sub={duration(totals?.drive_seconds) + ' driving'} />
        <Tile icon="leaf" label="Consumption" value={eff.value} unit={eff.unit} sub="Average over the period" />
      </div>

      <div className="card">
        {!trips ? null : !trips.length ? (
          <Empty>No trips in this period.</Empty>
        ) : (
          <div className="list">
            {days.map((g) => (
              <div key={g.key}>
                <div className="day">
                  <span>{day(g.trips[0].started_at)}</span>
                  <span className="nums">{dist(g.trips.reduce((sum, t) => sum + t.distance_km, 0))}</span>
                </div>
                {g.trips.map((t) => (
                  <div className="item" key={t.id}>
                    <button className="item-row" aria-expanded={open === t.id} onClick={() => setOpen(open === t.id ? null : t.id)}>
                      <span className="badge"><Icon name="route" /></span>
                      <span className="when nums soft">
                        {t.start_estimated ? '~' : ''}{time(t.started_at)} – {time(t.ended_at)}
                      </span>
                      <span className="title" title={route(t)}>{route(t)}</span>
                      <span className="right nums"><strong>{dist(t.distance_km)}</strong></span>
                      <span className="right nums soft hide-s">{duration(t.ended_at - t.started_at)}</span>
                      <span className="right nums soft hide-s">{kwh(t.energy_kwh)}</span>
                    </button>
                    {open === t.id && <TripDetail id={t.id} onChanged={reloadTrips} />}
                  </div>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  )
}

function TripDetail({ id, onChanged }: { id: number; onChanged: () => void }) {
  const { data: t, reload } = useLive<Trip>(`/trips/${id}`)
  const [note, setNote] = useState<string | null>(null)
  if (!t) return null

  const points = t.points ?? []
  const path = points.map((p) => [p.lat, p.lon] as [number, number])
  const markers = []
  if (t.start_lat != null && t.start_lon != null) markers.push({ at: [t.start_lat, t.start_lon] as [number, number], label: `Start: ${place(t.start_place)}` })
  if (t.end_lat != null && t.end_lon != null) markers.push({ at: [t.end_lat, t.end_lon] as [number, number], label: `End: ${place(t.end_place)}`, end: true })
  const sparse = points.length <= 2
  const text = note ?? t.note ?? ''

  return (
    <div className="item-body">
      <div className="facts">
        <div><dt>Distance</dt><dd>{dist(t.distance_km)}</dd></div>
        <div><dt>Duration</dt><dd>{duration(t.ended_at - t.started_at)}</dd></div>
        <div><dt>Average speed</dt><dd>{speed(t.avg_speed_kmh)}</dd></div>
        <div><dt>Battery</dt><dd>{pct(t.soc_start)} → {pct(t.soc_end)}</dd></div>
        <div><dt>Energy</dt><dd>{kwh(t.energy_kwh)}{t.energy_source === 'soc' ? ' (from battery %)' : ''}</dd></div>
        <div><dt>Consumption</dt><dd>{t.efficiency ? efficiency(t.efficiency) : 'Too short to measure'}</dd></div>
        <div><dt>Outside</dt><dd>{temp(t.temp_c)}</dd></div>
      </div>
      {t.start_estimated ? (
        <p className="hint">
          The car only reported this trip after it ended, so the start time is estimated from distance and average speed.
        </p>
      ) : null}
      {markers.length > 0 && <MapView path={path} markers={markers} dashed={sparse} />}
      {(['start', 'end'] as const).map((at) => (t[`${at}_lat`] == null || !!t[`${at}_manual`]) && (
        <PositionFix key={at} t={t} at={at} onSaved={() => { reload(); onChanged() }} />
      ))}
      {markers.length > 0 && sparse && (
        <p className="hint">The car did not report its position along the way, so only the start and end are shown.</p>
      )}
      <div className="row">
        <input
          type="text" className="grow" placeholder="Add a note" value={text} aria-label="Note"
          onChange={(e) => setNote(e.target.value)}
        />
        <button
          className="btn" disabled={note === null || note === (t.note ?? '')}
          onClick={async () => {
            await api(`/trips/${id}`, 'PATCH', { note })
            setNote(null)
            reload()
          }}
        >
          Save note
        </button>
      </div>
    </div>
  )
}

interface SavedPlace { id: number; name: string; lat: number; lon: number }
type LatLon = [number, number]

/** Enter a start or end the car had no GPS for: a saved place, or a spot clicked on the map. */
function PositionFix({ t, at, onSaved }: { t: Trip; at: 'start' | 'end'; onSaved: () => void }) {
  const [open, setOpen] = useState(false)
  const [pin, setPin] = useState<LatLon | null>(null)
  const [name, setName] = useState('')
  const { data: places } = useLive<SavedPlace[]>('/places')
  const { busy, run, note } = useAction()
  const manual = !!t[`${at}_manual`]

  const save = (body: { lat: number; lon: number; name?: string } | null) =>
    run(async () => {
      await api(`/trips/${t.id}`, 'PATCH', { [at]: body })
      setOpen(false)
      setPin(null)
      setName('')
      onSaved()
    })

  const other = at === 'start' ? [t.end_lat, t.end_lon] : [t.start_lat, t.start_lon]
  const center: LatLon | null =
    other[0] != null && other[1] != null ? [other[0], other[1]]
      : places?.length ? [places[0].lat, places[0].lon]
        : places ? [50, 10] : null

  return (
    <div className="stack">
      <div className="row">
        <p className="hint grow" style={{ margin: 0 }}>
          {manual
            ? `The ${at} position was entered by hand.`
            : `The car had no GPS at the ${at} of this trip (an underground car park, for example), so that place is unknown.`}
        </p>
        {!open && <button className="btn" onClick={() => setOpen(true)}>{manual ? 'Change' : `Set ${at} location`}</button>}
        {!open && manual && <button className="btn plain" disabled={busy} onClick={() => save(null)}>Remove</button>}
      </div>
      {open && (
        <div className="stack">
          {places && places.length > 0 && (
            <label className="field" style={{ maxWidth: 320 }}>
              A saved place
              <select value="" onChange={(e) => {
                const p = places.find((x) => String(x.id) === e.target.value)
                if (p) save({ lat: p.lat, lon: p.lon, name: p.name })
              }}>
                <option value="">Choose…</option>
                {places.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </label>
          )}
          <p className="hint" style={{ margin: 0 }}>{places?.length ? 'Or click' : 'Click'} the spot on the map.</p>
          {center && <PickMap center={center} zoom={other[0] != null || places?.length ? 15 : 4} value={pin} onPick={setPin} />}
          <div className="row">
            <input type="text" className="grow" placeholder="Name (optional, otherwise the address)" value={name} onChange={(e) => setName(e.target.value)} aria-label="Place name" />
            <button className="btn primary" disabled={busy || !pin} onClick={() => pin && save({ lat: pin[0], lon: pin[1], name })}>Save</button>
            <button className="btn" onClick={() => { setOpen(false); setPin(null) }}>Cancel</button>
            {note}
          </div>
          <p className="hint" style={{ margin: 0 }}>
            The car stayed put until its next trip, so that trip’s {at === 'end' ? 'start' : 'end'} gets this position too.
          </p>
        </div>
      )}
      {!open && note}
    </div>
  )
}

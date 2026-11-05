import { useEffect, useState } from 'react'
import { api, useLive, type Status } from '../api'
import { PickMap, useAction } from '../components'
import { ago, currencySymbol, dateTime } from '../format'

export function Settings({ status }: { status: Status }) {
  return (
    <>
      <div className="page-head"><h1>Settings</h1></div>
      <Volvo status={status} />
      <General capacity={status.vehicle.capacity_kwh} mqtt={status.connection.mqtt} />
      <Places status={status} />
      <Data />
      <Password sim={!!status.connection.sim} />
    </>
  )
}

// -- Volvo connection --------------------------------------------------------

interface VolvoView {
  configured: boolean
  connected: boolean
  signed_in: boolean
  client_id: string
  redirect_uri: string
  has_api_key: boolean
  has_client_secret: boolean
  suggested_redirect_uri: string
  vin: string
  vehicles: string[]
  scopes: string[]
  consent_at: number | null
  auth_error: { at: number; message: string } | null
  sim: string | null
}

function Volvo({ status }: { status: Status }) {
  const { data: v, reload } = useLive<VolvoView>('/volvo')
  const [form, setForm] = useState({ api_key: '', client_id: '', client_secret: '', redirect_uri: '' })
  const [pasted, setPasted] = useState('')
  const [editing, setEditing] = useState(false)
  const [awaiting, setAwaiting] = useState(false)
  const { busy, run, note } = useAction()
  const params = new URLSearchParams(window.location.search)
  const callbackError = params.get('volvo_error')

  useEffect(() => {
    if (v) setForm((f) => ({ ...f, client_id: v.client_id, redirect_uri: v.redirect_uri || v.suggested_redirect_uri }))
  }, [v?.client_id, v?.redirect_uri, v?.suggested_redirect_uri]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!v) return null
  const c = status.connection
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value })

  if (v.sim) {
    return (
      <div className="card stack">
        <h2>Volvo connection</h2>
        <p className="soft">
          Running against the built-in simulator ({v.sim} reporting). Start the server without <code>VOLVODASH_SIM</code> to connect a real car.
        </p>
      </div>
    )
  }

  // Volvo's sign-in opens in a new tab so this page stays here to paste the
  // address back into. The tab is opened before any await, or the browser
  // treats it as an unwanted pop-up.
  const signIn = () => {
    const tab = window.open('', '_blank')
    run(async () => {
      try {
        await api('/volvo/credentials', 'PUT', form)
        const { url } = await api<{ url: string }>('/volvo/authorize', 'POST')
        if (tab) tab.location.href = url
        else window.location.href = url
        setAwaiting(true)
      } catch (e) {
        tab?.close()
        throw e
      }
    })
  }

  const pasteBox = (
    <div className="stack">
      <p className="hint">
        After signing in, Volvo sends you to an address that may not load. Copy the whole address from that tab’s address bar and paste it here.
      </p>
      <div className="row">
        <input type="text" className="grow" value={pasted} placeholder="https://…?code=…&state=…" onChange={(e) => setPasted(e.target.value)} aria-label="Address Volvo redirected to" />
        <button className="btn" disabled={busy || !pasted} onClick={() => run(async () => { await api('/volvo/exchange', 'POST', { url: pasted }); setPasted(''); setEditing(false); setAwaiting(false); reload(); return 'Connected.' })}>
          Finish sign-in
        </button>
      </div>
    </div>
  )

  return (
    <div className="card stack">
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <h2>Volvo connection</h2>
        <span className="row" style={{ gap: 8 }}>
          <span className={`dot ${v.auth_error ? 'bad' : v.connected ? 'good' : ''}`} />
          <span className="soft">{v.auth_error ? 'Sign-in expired' : v.connected ? 'Connected' : 'Not connected'}</span>
        </span>
      </div>

      {callbackError && <p className="error">Volvo sign-in failed: {callbackError}</p>}
      {v.auth_error && (
        <p className="error">
          Volvo stopped accepting the sign-in on {dateTime(v.auth_error.at)} ({v.auth_error.message}). Nothing has been logged since. Sign in again to resume.
        </p>
      )}

      {v.client_id && !v.client_id.startsWith('vid-') && (
        <p className="error">
          Volvo is moving its sign-in to a new service, and this application (client ID starting <code>dc-</code>) stops working after 31 January 2027.
          Create a new application on the Volvo developer portal (its client ID starts with <code>vid-dc-</code>), then choose Change API details and enter
          its API key, client ID and secret. You will sign in once more, and the old application can then be deleted.
        </p>
      )}

      {v.connected && !editing ? (
        <>
          <div className="kv" style={{ maxWidth: 520 }}>
            <span>Car</span><span>{[status.vehicle.model, status.vehicle.year].filter(Boolean).join(' ')} (VIN …{status.vehicle.vin_tail})</span>
            <span>Signed in</span><span>{dateTime(v.consent_at)}</span>
            <span>Last check</span><span>{ago(c.last_poll)}</span>
            <span>API calls today</span><span>{c.calls_today.toLocaleString()} of {c.daily_limit.toLocaleString()}</span>
            {c.unavailable.length > 0 && (<><span>Not available for this car</span><span>{c.unavailable.join(', ')}</span></>)}
          </div>
          {c.paused && <p className="error">Today’s API allowance is used up. Polling resumes at midnight UTC.</p>}
          <p className="hint">
            Volvo keeps a sign-in valid for up to six months as long as it keeps being refreshed, which the dashboard does on its own. If it lapses or is revoked, this page and the notification webhook will tell you, and signing in again takes a few seconds.
          </p>
          <div className="row">
            <button className="btn primary" disabled={busy} onClick={signIn}>Sign in again</button>
            <button className="btn" onClick={() => setEditing(true)}>Change API details</button>
            <button
              className="btn" disabled={busy}
              onClick={() => { if (confirm('Disconnect from Volvo? Your logged trips and charges are kept.')) run(async () => { await api('/volvo/disconnect', 'POST'); reload() }) }}
            >
              Disconnect
            </button>
            {note}
          </div>
          {awaiting ? pasteBox : (
            <details>
              <summary>Have the address from Volvo’s sign-in? Paste it here</summary>
              <div style={{ marginTop: 10 }}>{pasteBox}</div>
            </details>
          )}
        </>
      ) : (
        <>
          <ol className="steps soft">
            <li>
              Sign in at <a href="https://developer.volvocars.com/account/" target="_blank" rel="noreferrer">developer.volvocars.com/account</a> with
              your Volvo ID and create an API application. Copy its <strong>VCC API key (primary)</strong>.
            </li>
            <li>
              Publish the application. Tick every scope in the list below, and set the redirect URI to exactly{' '}
              <code>{form.redirect_uri || v.suggested_redirect_uri}</code>. Copy the <strong>client ID</strong> and <strong>client secret</strong> it shows you.
            </li>
            <li>Fill them in here and sign in with the Volvo ID that owns the car.</li>
          </ol>
          <details>
            <summary>Scopes to tick ({v.scopes.length})</summary>
            <p style={{ marginTop: 8 }}>{v.scopes.map((s) => <code key={s} style={{ marginRight: 6 }}>{s}</code>)}</p>
          </details>
          <div className="form">
            <label className="field">VCC API key<input type="password" autoComplete="off" value={form.api_key} placeholder={v.has_api_key ? 'Saved. Leave blank to keep' : ''} onChange={set('api_key')} /></label>
            <label className="field">Client ID<input type="text" autoComplete="off" value={form.client_id} onChange={set('client_id')} /></label>
            <label className="field">Client secret<input type="password" autoComplete="off" value={form.client_secret} placeholder={v.has_client_secret ? 'Saved. Leave blank to keep' : ''} onChange={set('client_secret')} /></label>
            <label className="field" style={{ gridColumn: '1 / -1' }}>
              Redirect URI
              <input type="url" value={form.redirect_uri} onChange={set('redirect_uri')} />
            </label>
          </div>
          <p className="hint">
            The redirect URI must match the one on the Volvo application character for character. If Volvo will not accept this address,
            register any address it does accept, enter the same one here, and use the paste box below after signing in.
          </p>
          <div className="row">
            <button
              className="btn primary"
              disabled={busy || !form.client_id || !form.redirect_uri || (!form.api_key && !v.has_api_key) || (!form.client_secret && !v.has_client_secret)}
              onClick={signIn}
            >
              Save and sign in with Volvo ID
            </button>
            {editing && <button className="btn" onClick={() => setEditing(false)}>Cancel</button>}
            {note}
          </div>
          {awaiting ? pasteBox : (
            <details>
              <summary>Signed in, but landed on a page that would not load?</summary>
              <div style={{ marginTop: 10 }}>{pasteBox}</div>
            </details>
          )}
        </>
      )}

      {v.signed_in && v.vehicles.length > 1 && (
        <label className="field" style={{ maxWidth: 320 }}>
          Car to log
          <select value={v.vin} onChange={(e) => run(async () => { await api('/volvo/vehicle', 'POST', { vin: e.target.value }); reload() })}>
            {!v.vin && <option value="">Choose a car</option>}
            {v.vehicles.map((vin) => <option key={vin} value={vin}>{vin}</option>)}
          </select>
        </label>
      )}
    </div>
  )
}

// -- general -------------------------------------------------------------------

const EVENTS = [
  ['trip_ended', 'Trip ended'],
  ['charge_started', 'Charging started'],
  ['charge_ended', 'Charging ended'],
  ['needs_reauth', 'Volvo sign-in expired'],
] as const

function General({ capacity, mqtt }: { capacity: number | null; mqtt: Status['connection']['mqtt'] }) {
  const { data } = useLive<Record<string, any>>('/settings')
  const [form, setForm] = useState<Record<string, any> | null>(null)
  const { busy, run, note } = useAction()
  const test = useAction()

  useEffect(() => {
    if (data && !form) setForm(data)
  }, [data]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!form) return null
  const set = (k: string) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value })
  const save = () => run(async () => { await api('/settings', 'PUT', form); return 'Saved.' })
  const toggle = (event: string) => {
    const on = new Set<string>(form.webhook_events)
    on.has(event) ? on.delete(event) : on.add(event)
    setForm({ ...form, webhook_events: [...on] })
  }

  return (
    <>
      <div className="card stack">
        <h2>Car and display</h2>
        <div className="form">
          <label className="field">Name<input type="text" value={form.vehicle_name} placeholder="EX40" onChange={set('vehicle_name')} /></label>
          <label className="field">
            Distances in
            <select value={form.units} onChange={set('units')}><option value="km">Kilometres</option><option value="mi">Miles</option></select>
          </label>
          <label className="field">Currency symbol<input type="text" value={form.currency} onChange={set('currency')} /></label>
          <label className="field">Time zone<input type="text" value={form.timezone} placeholder="Europe/London" onChange={set('timezone')} /></label>
          <label className="field">
            Usable battery (kWh)
            <input type="number" step="any" min="1" value={form.usable_capacity_kwh ?? ''} placeholder={capacity ? `${capacity} from Volvo` : ''} onChange={set('usable_capacity_kwh')} />
          </label>
          <label className="field">
            Price per kWh away from saved places ({form.currency})
            <input type="number" step="any" min="0" value={form.default_price_per_kwh ?? ''} placeholder="None" onChange={set('default_price_per_kwh')} />
          </label>
        </div>
        <p className="hint">
          Battery size turns percent into kWh for trips and for charges without power readings. Volvo may report the gross size; if you know the usable figure, enter it.
          Changing it affects new trips and charges; use Rebuild below to apply it to old ones.
        </p>
        <div className="row"><button className="btn primary" disabled={busy} onClick={save}>Save</button>{note}</div>
      </div>

      <div className="card stack">
        <h2>Checking the car</h2>
        <div className="form">
          <label className="field">While parked, every (seconds)<input type="number" min="60" value={form.idle_interval_s} onChange={set('idle_interval_s')} /></label>
          <label className="field">While driving or charging, every (seconds)<input type="number" min="30" value={form.active_interval_s} onChange={set('active_interval_s')} /></label>
          <label className="field">Join trips when a stop is shorter than (seconds)<input type="number" min="0" value={form.merge_gap_s} onChange={set('merge_gap_s')} /></label>
        </div>
        <label className="check">
          <input type="checkbox" checked={form.lookups_enabled} onChange={(e) => setForm({ ...form, lookups_enabled: e.target.checked })} />
          Look up street addresses and outside temperature for trips
        </label>
        <p className="hint">
          Volvo allows 10,000 API calls a day; the defaults use roughly a fifth of that. Lookups send trip start and end coordinates to OpenStreetMap and Open-Meteo.
        </p>
        <div className="row"><button className="btn primary" disabled={busy} onClick={save}>Save</button>{note}</div>
      </div>

      <div className="card stack">
        <div className="row" style={{ justifyContent: 'space-between' }}>
          <h2>MQTT and Home Assistant</h2>
          {form.mqtt_enabled && (
            <span className="row" style={{ gap: 8 }}>
              <span className={`dot ${mqtt.state === 'connected' ? 'good' : mqtt.state === 'error' ? 'bad' : 'warn'}`} />
              <span className="soft">
                {mqtt.state === 'connected' ? 'Connected' : mqtt.state === 'error' ? 'Cannot connect' : 'Not connected'}
              </span>
            </span>
          )}
        </div>
        <label className="check">
          <input type="checkbox" checked={form.mqtt_enabled} onChange={(e) => setForm({ ...form, mqtt_enabled: e.target.checked })} />
          Publish to an MQTT broker
        </label>
        {form.mqtt_enabled && (
          <>
            <div className="form">
              <label className="field">Broker address<input type="text" value={form.mqtt_host} placeholder="192.168.2.10" onChange={set('mqtt_host')} /></label>
              <label className="field">Port<input type="number" min="1" value={form.mqtt_port} onChange={set('mqtt_port')} /></label>
              <label className="field">Username<input type="text" autoComplete="off" value={form.mqtt_username} onChange={set('mqtt_username')} /></label>
              <label className="field">
                Password
                <input type="password" autoComplete="off" value={form.mqtt_password} placeholder={form.has_mqtt_password ? 'Saved. Leave blank to keep' : ''} onChange={set('mqtt_password')} />
              </label>
            </div>
            <label className="check">
              <input type="checkbox" checked={form.mqtt_tls} onChange={(e) => setForm({ ...form, mqtt_tls: e.target.checked })} />
              Use TLS
            </label>
            <details>
              <summary>Topics</summary>
              <div className="form" style={{ marginTop: 10 }}>
                <label className="field">Topic prefix<input type="text" value={form.mqtt_topic_prefix} onChange={set('mqtt_topic_prefix')} /></label>
                <label className="field">Home Assistant discovery prefix<input type="text" value={form.mqtt_discovery_prefix} onChange={set('mqtt_discovery_prefix')} /></label>
              </div>
            </details>
            {mqtt.state === 'error' && <p className="error">{mqtt.error}</p>}
            {mqtt.state === 'waiting' && mqtt.error && <p className="hint">{mqtt.error}</p>}
          </>
        )}
        <p className="hint">
          Home Assistant finds the car by itself through MQTT discovery: battery, range, charging, odometer, locks, doors and windows, last trip and charge,
          a map tracker, and buttons to lock, start or stop climate and check now. Unlocking is deliberately not offered over MQTT.
        </p>
        <div className="row"><button className="btn primary" disabled={busy} onClick={save}>Save</button>{note}</div>
      </div>

      <div className="card stack">
        <h2>Notifications</h2>
        <label className="field">
          Webhook URL
          <input type="url" value={form.webhook_url} placeholder="https://ntfy.sh/your-topic" onChange={set('webhook_url')} />
        </label>
        <div className="row">
          {EVENTS.map(([key, label]) => (
            <label key={key} className="check">
              <input type="checkbox" checked={form.webhook_events.includes(key)} onChange={() => toggle(key)} />{label}
            </label>
          ))}
        </div>
        <p className="hint">Works with ntfy, Discord and Slack webhooks, or anything that accepts a POST.</p>
        <div className="row">
          <button className="btn primary" disabled={busy} onClick={save}>Save</button>
          <button className="btn" disabled={test.busy || !form.webhook_url} onClick={() => test.run(async () => { await api('/settings', 'PUT', form); await api('/settings/test-webhook', 'POST'); return 'Test sent.' })}>Send test</button>
          {note}{test.note}
        </div>
      </div>
    </>
  )
}

// -- places ----------------------------------------------------------------------

interface Place { id: number; name: string; lat: number; lon: number; radius_m: number; price_per_kwh: number | null }

function Places({ status }: { status: Status }) {
  const { data: places, reload } = useLive<Place[]>('/places')
  const blank = { name: '', lat: '', lon: '', radius_m: '150', price_per_kwh: '' }
  const [form, setForm] = useState<Record<string, string> & { id?: string }>(blank)
  const { busy, run, note } = useAction()
  const set = (k: string) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value })
  const here = status.obs?.lat != null ? status.obs : null
  const cur = currencySymbol()

  return (
    <div className="card stack">
      <h2>Places and prices</h2>
      <p className="hint">
        Name the places you charge, with what electricity costs there. Charging sessions inside the radius get the name and the price automatically, including past ones.
      </p>
      {places && places.length > 0 && (
        <table className="plain">
          <thead><tr><th>Name</th><th>Price per kWh</th><th>Radius</th><th /></tr></thead>
          <tbody>
            {places.map((p) => (
              <tr key={p.id}>
                <td>{p.name}</td>
                <td className="nums">{p.price_per_kwh != null ? `${cur}${p.price_per_kwh}` : '–'}</td>
                <td className="nums">{p.radius_m} m</td>
                <td className="right">
                  <button className="btn plain" onClick={() => setForm({ id: String(p.id), name: p.name, lat: String(p.lat), lon: String(p.lon), radius_m: String(p.radius_m), price_per_kwh: p.price_per_kwh?.toString() ?? '' })}>Edit</button>
                  <button className="btn plain" onClick={() => { if (confirm(`Remove ${p.name}?`)) run(async () => { await api(`/places/${p.id}`, 'DELETE'); reload() }) }}>Remove</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {places && (() => {
        const pin = form.lat && form.lon && !isNaN(+form.lat) && !isNaN(+form.lon) ? [+form.lat, +form.lon] as [number, number] : null
        const start: [number, number] | null =
          pin ?? (here ? [here.lat!, here.lon!] : places.length ? [places[0].lat, places[0].lon] : null)
        return (
          <>
            <p className="hint" style={{ margin: 0 }}>Click the map to put the place there. The circle is the radius it covers.</p>
            <PickMap
              key={form.id ?? 'new'}
              center={start ?? [50, 10]} zoom={start ? 16 : 4}
              value={pin} radius={+form.radius_m || undefined}
              onPick={([lat, lon]) => setForm({ ...form, lat: lat.toFixed(6), lon: lon.toFixed(6) })}
              others={places.filter((p) => String(p.id) !== form.id).map((p) => ({ at: [p.lat, p.lon], label: p.name, radius: p.radius_m }))}
            />
          </>
        )
      })()}
      <div className="form">
        <label className="field">Name<input type="text" value={form.name} placeholder="Home" onChange={set('name')} /></label>
        <label className="field">Latitude<input type="number" step="any" value={form.lat} onChange={set('lat')} /></label>
        <label className="field">Longitude<input type="number" step="any" value={form.lon} onChange={set('lon')} /></label>
        <label className="field">Radius (m)<input type="number" min="25" value={form.radius_m} onChange={set('radius_m')} /></label>
        <label className="field">Price per kWh ({cur})<input type="number" step="any" min="0" value={form.price_per_kwh} onChange={set('price_per_kwh')} /></label>
      </div>
      <div className="row">
        <button
          className="btn primary" disabled={busy || !form.name || !form.lat || !form.lon}
          onClick={() => run(async () => {
            await api(form.id ? `/places/${form.id}` : '/places', form.id ? 'PUT' : 'POST', form)
            setForm(blank)
            reload()
            return 'Saved.'
          })}
        >
          {form.id ? 'Save place' : 'Add place'}
        </button>
        {here && <button className="btn" onClick={() => setForm({ ...form, lat: String(here.lat), lon: String(here.lon) })}>Use where the car is now</button>}
        {form.id && <button className="btn" onClick={() => setForm(blank)}>Cancel</button>}
        {note}
      </div>
    </div>
  )
}

// -- data and password -------------------------------------------------------------

function Data() {
  const { busy, run, note } = useAction()
  return (
    <div className="card stack">
      <h2>Data</h2>
      <p className="hint">
        Every answer Volvo’s API gives is kept in a raw log, and everything else (readings, trips, charging sessions) is worked out from it.
        Rebuild works it all out again from that log, which is how changes to battery size, trip joining or a new version’s detection reach old entries.
        Notes, prices and costs you entered are kept, and a backup is taken first. Backups are also made daily into the <code>backups</code> folder next to the database; the last 14 are kept.
      </p>
      <div className="row">
        <button className="btn" disabled={busy} onClick={() => run(async () => { const r = await api<{ trips: number; charges: number }>('/rebuild', 'POST'); return `Rebuilt ${r.trips} trips and ${r.charges} charging sessions.` })}>
          {busy ? 'Rebuilding…' : 'Rebuild trips and charges'}
        </button>
        <a className="btn" href="/api/export/trips.csv">Export all trips</a>
        <a className="btn" href="/api/export/charges.csv">Export all charges</a>
        <a className="btn" href="/api/export/raw.jsonl">Download raw log</a>
        {note}
      </div>
    </div>
  )
}

function Password({ sim }: { sim: boolean }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const { busy, run, note } = useAction()
  if (sim) return null
  return (
    <div className="card stack">
      <h2>Dashboard password</h2>
      <div className="form">
        <label className="field">Current password<input type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} /></label>
        <label className="field">New password<input type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} /></label>
      </div>
      <div className="row">
        <button className="btn" disabled={busy || !current || next.length < 8} onClick={() => run(async () => { await api('/password', 'POST', { current, new: next }); window.location.reload() })}>Change password</button>
        <button className="btn" onClick={async () => { await api('/logout', 'POST'); window.location.reload() }}>Sign out</button>
        {note}
      </div>
    </div>
  )
}

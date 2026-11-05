import { type FormEvent, useEffect, useState } from 'react'
import { api, useLive, type Status } from './api'
import { Icon, type IconName, Link, ThemeSwitch, useAction, usePath } from './components'
import { configure, day, now } from './format'
import { Charging } from './pages/Charging'
import { Overview } from './pages/Overview'
import { Settings } from './pages/Settings'
import { Stats } from './pages/Stats'
import { Trips } from './pages/Trips'

const TABS: [string, string, IconName][] = [
  ['/', 'Overview', 'gauge'],
  ['/trips', 'Trips', 'route'],
  ['/charging', 'Charging', 'bolt'],
  ['/stats', 'Stats', 'chart'],
  ['/settings', 'Settings', 'sliders'],
]

interface Session {
  setup_needed: boolean
  signed_in: boolean
  sim: string | null
}

export function App() {
  const [session, setSession] = useState<Session | null>(null)
  const load = () => api<Session>('/session').then(setSession)

  useEffect(() => {
    load()
    window.addEventListener('signed-out', load)
    return () => window.removeEventListener('signed-out', load)
  }, [])

  if (!session) return null
  if (session.setup_needed || !session.signed_in) return <Login setup={session.setup_needed} onDone={load} />
  return <Shell />
}

function Shell() {
  const path = usePath()
  const { data: status } = useLive<Status>('/status')
  if (!status) return null
  configure(status.settings, status.now)

  const c = status.connection
  const title = status.settings.vehicle_name || status.vehicle.model || 'Volvo Dash'
  const page =
    path === '/trips' ? <Trips /> :
    path === '/charging' ? <Charging /> :
    path === '/stats' ? <Stats /> :
    path === '/settings' ? <Settings status={status} /> :
    <Overview status={status} />

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <span className="brand"><span className="logo"><Icon name="bolt" /></span>{title}</span>
          <nav className="tabs">
            {TABS.map(([to, label, icon]) => (
              <Link key={to} to={to} className={path === to ? 'on' : ''}><Icon name={icon} />{label}</Link>
            ))}
          </nav>
          <ThemeSwitch />
        </div>
      </header>
      <main>
        {c.sim && (
          <div className="banner">
            <span className="dot warn" />
            <span className="grow">
              <strong>Simulated car.</strong> Nothing here comes from a real Volvo. The simulator runs faster than real time.
            </span>
          </div>
        )}
        {!c.sim && path !== '/settings' && !c.connected && (
          <div className="banner warn">
            <span className="grow"><strong>Not connected to Volvo yet.</strong> Add your API details to start logging.</span>
            <Link to="/settings" className="btn primary">Connect</Link>
          </div>
        )}
        {!c.auth_error && c.sign_in_until && c.sign_in_until - now() < 14 * 86400 && path !== '/settings' && (
          <div className="banner warn">
            <span className="grow">
              <strong>Volvo sign-in runs out {day(c.sign_in_until)}.</strong> Sign in again before then to keep logging without a gap.
            </span>
            <Link to="/settings" className="btn primary">Sign in again</Link>
          </div>
        )}
        {c.auth_error && path !== '/settings' && (
          <div className="banner bad">
            <span className="grow">
              <strong>Volvo sign-in has expired.</strong> Logging is paused until you sign in again.
            </span>
            <Link to="/settings" className="btn primary">Sign in again</Link>
          </div>
        )}
        {page}
      </main>
    </>
  )
}

function Login({ setup, onDone }: { setup: boolean; onDone: () => void }) {
  const [password, setPassword] = useState('')
  const { busy, run, note } = useAction()

  function submit(e: FormEvent) {
    e.preventDefault()
    run(async () => {
      if (setup) {
        const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone
        await api('/setup', 'POST', { password, timezone })
      } else {
        await api('/login', 'POST', { password })
      }
      onDone()
    })
  }

  return (
    <main>
      <form className="card stack login" onSubmit={submit}>
        <h1>Volvo Dash</h1>
        <p className="soft">
          {setup ? 'Choose a password for this dashboard. You will need it to sign in from now on.' : 'Sign in to see your car.'}
        </p>
        <label className="field">
          Password
          <input
            type="password"
            value={password}
            autoFocus
            autoComplete={setup ? 'new-password' : 'current-password'}
            onChange={(e) => setPassword(e.target.value)}
          />
        </label>
        <button className="btn primary" disabled={busy || password.length < (setup ? 8 : 1)}>
          {setup ? 'Set password' : 'Sign in'}
        </button>
        {setup && <span className="hint">At least 8 characters.</span>}
        {note}
      </form>
    </main>
  )
}

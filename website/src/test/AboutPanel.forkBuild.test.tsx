//
// Contract under test — the fork-identity readout in the About hero.
//
// This is a long-lived FORK, and `version` carries UPSTREAM's base literal
// because every comparison and packaging manifest keys on it. That left a fork
// build indistinguishable from the upstream build it forked — an upstream
// install one minor ahead even read as NEWER, which is the confusion this
// readout exists to end.
//
// - a fork build           -> "based on upstream <base> · fork g<sha>"
// - an upstream build      -> no attribution line AT ALL, never a placeholder:
//                             an empty `fork_revision` also covers an install
//                             where no revision could be derived, and asserting
//                             a fork there would be a guess
// - a dirty working tree   -> the uncommitted-changes marker, because the
//                             running bytes are then not any commit
// - a withheld update      -> the reason, instead of an unexplained "up to date"
//                             sitting beside a newer upstream version
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

function stubFetch() {
  const json = (body: unknown) => ({
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ 'content-type': 'application/json' }),
  })
  vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.includes('/api/update/check')) {
      return json({ check_status: 'succeeded', update_available: false, error_code: null })
    }
    if (url.includes('/api/changelog')) return json({ content: '' })
    return json({})
  }))
}

function mountWeb() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as const

describe('AboutPanel fork identity', () => {
  beforeEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
    stubFetch()
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('names the upstream base and the fork revision on a fork build', async () => {
    store.dispatch(sseStatus({
      ...BLANK_STATUS,
      version: '0.4.0-rc.9',
      version_display: '0.4.0-rc.9',
      upstream_base_version: '0.4.0-rc.9',
      fork_revision: '645d7289',
      fork_dirty: false,
    } as never))
    mountWeb()
    const line = await screen.findByTestId('about-fork-build')
    // Both halves in one line: the base the fork sits on, and the commit it is.
    expect(line.textContent).toContain('0.4.0-rc.9')
    // The `g` prefix is git's own, so the sha reads as an object name rather
    // than as part of the version number.
    expect(line.textContent).toContain('g645d7289')
    expect(screen.queryByTestId('about-fork-dirty')).toBeNull()
  })

  it('renders nothing at all for an upstream build', async () => {
    store.dispatch(sseStatus({
      ...BLANK_STATUS, version: '0.4.0-rc.9', version_display: '0.4.0-rc.9',
    } as never))
    mountWeb()
    // The version chip still renders, so the panel is mounted and this is a
    // real absence rather than an unrendered tree.
    expect(await screen.findByText('v0.4.0-rc.9')).toBeTruthy()
    expect(screen.queryByTestId('about-fork-build')).toBeNull()
  })

  it('marks an uncommitted working tree', async () => {
    store.dispatch(sseStatus({
      ...BLANK_STATUS,
      version: '0.4.0-rc.9',
      version_display: '0.4.0-rc.9',
      upstream_base_version: '0.4.0-rc.9',
      fork_revision: '645d7289',
      fork_dirty: true,
    } as never))
    mountWeb()
    expect(await screen.findByTestId('about-fork-dirty')).toBeTruthy()
  })

  it('explains a withheld upstream update instead of leaving it unexplained', async () => {
    store.dispatch(sseStatus({
      ...BLANK_STATUS,
      version: '0.4.0-rc.9',
      version_display: '0.4.0-rc.9',
      upstream_base_version: '0.4.0-rc.9',
      fork_revision: '645d7289',
      fork_dirty: false,
      update_fork_suppressed: true,
      update_latest_version: '0.4.1-insider.1',
      update_latest_version_display: '0.4.1-insider.1',
    } as never))
    mountWeb()
    const note = await screen.findByTestId('about-fork-update-withheld')
    // Names the upstream version, so the user can see that upstream moved even
    // though nothing here will install it.
    expect(note.textContent).toContain('0.4.1-insider.1')
  })

  it('says nothing about a withheld update when there is no candidate', async () => {
    store.dispatch(sseStatus({
      ...BLANK_STATUS,
      version: '0.4.0-rc.9',
      version_display: '0.4.0-rc.9',
      upstream_base_version: '0.4.0-rc.9',
      fork_revision: '645d7289',
      update_fork_suppressed: true,
    } as never))
    mountWeb()
    await screen.findByTestId('about-fork-build')
    expect(screen.queryByTestId('about-fork-update-withheld')).toBeNull()
  })

  it('badges and explains an upstream-ahead fork on the maintainer checkout', async () => {
    store.dispatch(sseStatus({
      ...BLANK_STATUS,
      version: '0.13.0.1',
      version_display: '0.13.0.1+fork.g22e041c8',
      upstream_base_version: '0.13.0',
      fork_revision: '22e041c8',
      fork_dirty: false,
      upstream_version: '0.14.0',
      fork_ahead_of_upstream: 4,
      fork_behind_upstream: 17,
      fork_sync_available: true,
    } as never))
    mountWeb()
    expect(await screen.findByTestId('about-fork-sync-badge')).toBeTruthy()
    const panel = await screen.findByTestId('about-fork-sync-panel')
    // The distance renders through the commits_behind plural, and the panel
    // names upstream's own version so the maintainer sees WHAT moved.
    expect(panel.textContent).toContain('17 commits behind')
    expect(panel.textContent).toContain('0.14.0')
  })

  it('stays silent for a fleet install whose checkout has no upstream remote', async () => {
    // A fork build with no probe answer: fork_sync_available never fired, so
    // neither the badge nor the panel may appear — fleet machines update from
    // the fork and must not be told to chase upstream.
    store.dispatch(sseStatus({
      ...BLANK_STATUS,
      version: '0.13.0.1',
      version_display: '0.13.0.1+fork.g22e041c8',
      upstream_base_version: '0.13.0',
      fork_revision: '22e041c8',
      fork_dirty: false,
    } as never))
    mountWeb()
    await screen.findByTestId('about-fork-build')
    expect(screen.queryByTestId('about-fork-sync-badge')).toBeNull()
    expect(screen.queryByTestId('about-fork-sync-panel')).toBeNull()
  })
})

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SchedulePage from '../pages/SchedulePage'
import type { CronJob } from '../types'

/**
 * A job with auto-pause off (or a raised limit) that keeps failing backs off
 * from its 5th failure in a row, and `next_run_ts` already carries the shift,
 * so the Next run column showed a later time with nothing saying why. This pins
 * the note the Next run cell renders from `failure_backoff_until`.
 */

// The page compares `failure_backoff_until` with `Date.now()`, so the tests pin
// `Date` to one instant and build every fixture from it. Only `Date` is faked:
// the page's async loads still run on real timers.
const NOW_MS = Date.UTC(2026, 0, 15, 12, 0, 0)
const nowSec = () => NOW_MS / 1000

const mkJob = (overrides: Partial<CronJob> = {}): CronJob => ({
  id: 'job-1',
  name: 'Nightly report',
  schedule: 'every 1d',
  message: 'send report',
  enabled: true,
  ...overrides,
} as CronJob)

vi.mock('../api/client', () => ({
  api: {
    crons: vi.fn(),
    cronFolders: vi.fn().mockResolvedValue([]),
    deleteCron: vi.fn(),
    batchDeleteCron: vi.fn(),
    createCron: vi.fn().mockResolvedValue({}),
    models: vi.fn().mockResolvedValue([]),
    updateCron: vi.fn().mockResolvedValue({}),
    toggleCron: vi.fn().mockResolvedValue({}),
    runCron: vi.fn().mockResolvedValue({}),
    cronToChat: vi.fn().mockResolvedValue({}),
    cronHistoryAll: vi.fn().mockResolvedValue({ runs: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    agentCatalog: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    defaultAgent: vi.fn().mockResolvedValue({ default_agent: '' }),
  },
}))

describe('SchedulePage Next run column — failure backoff note', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW_MS)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows the failure count when the backoff ends in the future', async () => {
    const until = nowSec() + 3600
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Failing job', consecutive_failures: 6, failure_backoff_until: until, next_run_ts: until })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Failing job')).toBeInTheDocument())

    const note = screen.getByText(/^Delayed until \S.* · 6 failures in a row$/)
    expect(note).toBeInTheDocument()
    // The cell's tooltip carries the note and what ends the delay, for a reader
    // who hovers the time.
    const title = note.closest('td')?.getAttribute('title') ?? ''
    expect(title).toContain(note.textContent ?? '')
    expect(title).toContain('A successful run ends the delay.')
  })

  it('renders no note when the job is not backing off (null)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Healthy job', consecutive_failures: 0, failure_backoff_until: null, next_run_ts: nowSec() + 60 })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Healthy job')).toBeInTheDocument())

    expect(screen.queryByText(/^Delayed until /)).not.toBeInTheDocument()
  })

  it('renders no note once the backoff time has passed', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Elapsed job', consecutive_failures: 7, failure_backoff_until: nowSec() - 3600 })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Elapsed job')).toBeInTheDocument())

    expect(screen.queryByText(/^Delayed until /)).not.toBeInTheDocument()
  })

  it('renders no note when the field is absent (older gateway)', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api).crons.mockResolvedValue({
      jobs: [mkJob({ name: 'Old gateway job', next_run_ts: nowSec() + 60 })],
    })

    renderWithProviders(<SchedulePage />)
    await waitFor(() => expect(screen.getByText('Old gateway job')).toBeInTheDocument())

    expect(screen.queryByText(/^Delayed until /)).not.toBeInTheDocument()
  })
})

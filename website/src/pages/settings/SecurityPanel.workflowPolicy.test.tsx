/**
 * Git publication mode + ssh-agent forwarding (Settings → Security → Rules).
 *
 * Both controls WIDEN what the agent may do, so the properties pinned here are
 * the ones that would be dangerous to get wrong on screen: the default renders
 * as protected, choosing the loosening option shows its cost, and the warning
 * appears only when the loosening position is actually active — a warning that
 * shows in both positions teaches the operator to ignore it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, cleanup } from '@testing-library/react'

import { renderWithProviders } from '../../test/helpers'
import type { WorkflowPolicyData } from '../../api/client'

vi.mock('../../api/client', () => ({
  api: {
    // The panel's rail reads these on mount regardless of the selected section.
    deniedCommands: vi.fn(),
    governancePolicy: vi.fn(),
    securityPosture: vi.fn(),
    kirocrewConfig: vi.fn(),
    patchConfig: vi.fn(),
    tailnetStatus: vi.fn(),
    workflowPolicy: vi.fn(),
    setWorkflowPolicy: vi.fn(),
  },
}))

import { api } from '../../api/client'
import { SecurityPanel } from './SecurityPanel'

/** Matched on the stable half: the parenthesised '(recommended)' would be read
 *  as a regex group by `new RegExp`, and silently match the wrong string. */
const PROTECTED_LABEL = 'Kiro Crew protected'
const GOVERNED_LABEL = 'Repository-governed'
const GOVERNED_WARNING = /Configure branch protection on the remote/i
const SSH_LABEL = 'Forward the ssh-agent socket'
const SSH_WARNING = /Every key your ssh-agent holds/i
const HANDOFF_LABEL = 'Let other agents hand work over'
const HANDOFF_WARNING = /Any program running as you can then start an unattended session/i

function policy(overrides: Partial<WorkflowPolicyData> = {}): WorkflowPolicyData {
  return {
    git_publication_mode: 'protected',
    forward_ssh_agent: false,
    allow_external_handoff: false,
    floor_enforced_ids: ['git-publish-push-bare'],
    ...overrides,
  }
}

/** Render the rules section and wait until the query has actually LANDED.
 *
 *  Waiting for the radios to exist is not enough: they paint on the first frame
 *  from the `?? 'protected'` fallback while the query is still in flight, and
 *  they are disabled until it resolves — so an assertion (or a click) made at
 *  that point reads the fallback, not the fixture. Waiting on the fixture's own
 *  mode being checked is what makes the barrier real. */
async function renderRules(data: WorkflowPolicyData = policy()) {
  ;(api.workflowPolicy as ReturnType<typeof vi.fn>).mockResolvedValue(data)
  const utils = renderWithProviders(<SecurityPanel />, { route: '/?section=rules' })
  // Two barriers, and both are needed. The radios paint on the first frame from
  // the `?? 'protected'` fallback while the query is still in flight, so waiting
  // for them to EXIST reads the fallback rather than the fixture; and the card's
  // controls are disabled until the query lands, so a click made before it does
  // is dropped. Waiting for the call, then for the fixture's own mode to be the
  // checked one, is what makes the barrier real for every fixture.
  await waitFor(() => expect(api.workflowPolicy).toHaveBeenCalled())
  const activeLabel = data.git_publication_mode === 'protected' ? PROTECTED_LABEL : GOVERNED_LABEL
  await waitFor(() => {
    expect(screen.getByRole('radio', { name: new RegExp(activeLabel, 'i') })).toHaveAttribute('aria-checked', 'true')
  })
  return utils
}

describe('git publication mode', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue({
      builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
    })
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue({ controls: [] })
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue({ scopes: [] })
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
      enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off',
    })
  })

  it('renders the protected mode selected and shows no warning', async () => {
    await renderRules()
    expect(screen.getByRole('radio', { name: new RegExp(PROTECTED_LABEL, 'i') })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByRole('radio', { name: new RegExp(GOVERNED_LABEL, 'i') })).toHaveAttribute('aria-checked', 'false')
    expect(screen.queryByText(GOVERNED_WARNING)).toBeNull()
  })

  it('warns only while the repository-governed mode is the ACTIVE one', async () => {
    await renderRules(policy({ git_publication_mode: 'repository_governed', floor_enforced_ids: [] }))
    expect(screen.getByRole('radio', { name: new RegExp(GOVERNED_LABEL, 'i') })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText(GOVERNED_WARNING)).toBeTruthy()
  })

  it('sends the mode the operator picked, and nothing else', async () => {
    await renderRules()
    ;(api.setWorkflowPolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      policy({ git_publication_mode: 'repository_governed', floor_enforced_ids: [] }),
    )
    fireEvent.click(screen.getByRole('radio', { name: new RegExp(GOVERNED_LABEL, 'i') }))
    await waitFor(() => {
      expect(api.setWorkflowPolicy).toHaveBeenCalledWith({ git_publication_mode: 'repository_governed' })
    })
    // The mode decides whether the deny rows render locked, so the rule list
    // must repaint rather than keep showing locks for a retired floor.
    expect(screen.getByText(GOVERNED_WARNING)).toBeTruthy()
  })

  it('does not re-send the mode already active', async () => {
    await renderRules()
    fireEvent.click(screen.getByRole('radio', { name: new RegExp(PROTECTED_LABEL, 'i') }))
    expect(api.setWorkflowPolicy).not.toHaveBeenCalled()
  })
})

describe('ssh-agent forwarding', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue({
      builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
    })
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue({ controls: [] })
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue({ scopes: [] })
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
      enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off',
    })
  })

  it('is off by default and carries no warning until it is on', async () => {
    await renderRules()
    expect(screen.queryByText(SSH_WARNING)).toBeNull()
  })

  it('states the cost while forwarding is active', async () => {
    await renderRules(policy({ forward_ssh_agent: true }))
    expect(screen.getByText(SSH_WARNING)).toBeTruthy()
  })

  it('sends only the ssh field, leaving the publication mode alone', async () => {
    await renderRules()
    ;(api.setWorkflowPolicy as ReturnType<typeof vi.fn>).mockResolvedValue(policy({ forward_ssh_agent: true }))
    fireEvent.click(screen.getByRole('switch', { name: SSH_LABEL }))
    await waitFor(() => {
      expect(api.setWorkflowPolicy).toHaveBeenCalledWith({ forward_ssh_agent: true })
    })
  })
})

describe('external handoff', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    ;(api.deniedCommands as ReturnType<typeof vi.fn>).mockResolvedValue({
      builtins: [], user_added: [], disable_all: false, effective_count: 0, governance_locked: false,
    })
    ;(api.securityPosture as ReturnType<typeof vi.fn>).mockResolvedValue({ controls: [] })
    ;(api.governancePolicy as ReturnType<typeof vi.fn>).mockResolvedValue({ scopes: [] })
    ;(api.kirocrewConfig as ReturnType<typeof vi.fn>).mockResolvedValue({})
    ;(api.tailnetStatus as ReturnType<typeof vi.fn>).mockResolvedValue({
      enabled: false, governance_pinned: false, host: '', origin: '', resolved_at: 0, state: 'off',
    })
  })

  it('is off by default and carries no warning until it is on', async () => {
    await renderRules()
    expect(screen.getByRole('switch', { name: HANDOFF_LABEL })).toBeTruthy()
    expect(screen.queryByText(HANDOFF_WARNING)).toBeNull()
  })

  it('states what it opens while it is active', async () => {
    await renderRules(policy({ allow_external_handoff: true }))
    expect(screen.getByText(HANDOFF_WARNING)).toBeTruthy()
  })

  it('sends only its own field, leaving the other two alone', async () => {
    await renderRules()
    ;(api.setWorkflowPolicy as ReturnType<typeof vi.fn>).mockResolvedValue(
      policy({ allow_external_handoff: true }),
    )
    fireEvent.click(screen.getByRole('switch', { name: HANDOFF_LABEL }))
    await waitFor(() => {
      expect(api.setWorkflowPolicy).toHaveBeenCalledWith({ allow_external_handoff: true })
    })
  })
})

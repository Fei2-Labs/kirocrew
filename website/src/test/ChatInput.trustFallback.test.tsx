/**
 * Unit contract for #5486: ChatInput's one-shot fallback must never create or
 * misreport a trust grant.
 *
 * The behavioral half of this fix is covered in ChatInput.approval.test.tsx
 * (Trust controls withheld when no slot backs the composer). What a DOM test
 * cannot reach is the defence-in-depth underneath: with the controls gated,
 * the downgrade path is unreachable from the UI, so it is pinned here at the
 * unit level against the exported `oneShotResolution` helper — a real
 * assertion on behavior, not a source-string match.
 *
 * The set-membership half guards the OTHER direction: every trust verb the
 * Trust controls can emit must be in `TRUST_DECISIONS`, or a future tier
 * would fall through to the one-shot path and resolve as a denial.
 */
import { describe, it, expect, vi } from 'vitest'

vi.mock('@radix-ui/react-dropdown-menu', async () => await import('./__mocks__/@radix-ui/react-dropdown-menu'))

import { render, screen, fireEvent } from '@testing-library/react'
import { TRUST_DECISIONS, oneShotResolution } from '../components/ChatInput'
import TrustDropdown from '../components/TrustDropdown'

describe('oneShotResolution (#5486 contract)', () => {
  it('downgrades every trust verb to a one-shot allow recorded as approved — never as the trust verb', () => {
    // The one-shot api.resolveApproval endpoint has no trust verb. A trust
    // decision that reaches it (no slot-backed path: unattended source, or no
    // activeSlot) must resolve as a plain allow AND be recorded as 'approved',
    // because no standing grant was created — recording 'trust' here is the
    // #5486 defect: the composer claiming a grant the backend never made.
    for (const verb of TRUST_DECISIONS) {
      expect(oneShotResolution(verb)).toEqual({ wire: 'approve', granted: 'approved' })
    }
  })

  it('honors approve/reject verbatim', () => {
    expect(oneShotResolution('approved')).toEqual({ wire: 'approve', granted: 'approved' })
    expect(oneShotResolution('rejected')).toEqual({ wire: 'reject', granted: 'rejected' })
  })

  it('fails closed on an unknown decision, and labels the denial', () => {
    // Deny-by-default is the fail-closed direction on a permission surface,
    // and recording 'rejected' (not the unknown verb) keeps the row wearing
    // the rejected affordance instead of an unlabeled resolved state.
    expect(oneShotResolution('trust_everything_forever')).toEqual({ wire: 'reject', granted: 'rejected' })
    expect(oneShotResolution('')).toEqual({ wire: 'reject', granted: 'rejected' })
  })
})

describe('TrustDropdown emissions stay inside TRUST_DECISIONS (#5486 contract)', () => {
  it('every verb the dropdown can emit routes to the slot-backed grant path', () => {
    // The routing guard in handleApprovalAction selects on TRUST_DECISIONS.
    // A tier added to TrustDropdown but not to the set would fall through to
    // the one-shot path and resolve as a DENIAL — this test reds first.
    const emitted: string[] = []
    render(
      <TrustDropdown
        fullCommand="ls /tmp"
        baseCommand="ls"
        isShell
        onAction={(action) => { emitted.push(action) }}
      />,
    )
    // Selecting an item closes the menu, so re-open it before each click.
    const openMenu = () => {
      if (screen.queryAllByRole('menuitem').length === 0) fireEvent.click(screen.getByText('Trust'))
      return screen.getAllByRole('menuitem')
    }
    const total = openMenu().length
    for (let i = 0; i < total; i++) fireEvent.click(openMenu()[i])
    expect(emitted.length).toBeGreaterThanOrEqual(3) // trust_command, trust_base, trust
    for (const verb of emitted) {
      expect(TRUST_DECISIONS.has(verb), `TrustDropdown emits '${verb}' which is not in TRUST_DECISIONS`).toBe(true)
    }
    // The standalone Trust-reads button emits 'trust_reads'; pin it too.
    expect(TRUST_DECISIONS.has('trust_reads')).toBe(true)
  })
})

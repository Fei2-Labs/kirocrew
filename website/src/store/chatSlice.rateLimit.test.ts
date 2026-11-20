// The plan-quota reading rides the `context_usage` frame. The reducer is what
// makes the context popover's plan-limit block reachable from a live session,
// so it is pinned here and not only at the component: a renderer fed by a prop
// nothing stores shows nothing in the product while its own tests stay green.
import { describe, expect, it } from 'vitest'
import reducer, { sseContextUsage } from './chatSlice'

const SLOT = 'slot-a'
const QUOTA = { status: 'allowed_warning', limit_type: 'five_hour', utilization: 81, resets_at: 1_787_000_000 }

describe('plan quota on the context_usage frame', () => {
  it('stores the reading per slot', () => {
    let state = reducer(undefined, { type: '@@init' })
    state = reducer(state, sseContextUsage({ slot: SLOT, pct: 12, used_tokens: 120, window_tokens: 1000, rate_limit: QUOTA }))
    expect(state.slotRateLimit[SLOT]).toEqual(QUOTA)
    expect(state.slotRateLimit['slot-b']).toBeUndefined()
  })

  it('keeps the reading through a frame without one and through a reset', () => {
    // Account state, not transcript state: a compaction or model switch does
    // not change how much of the plan is left.
    let state = reducer(undefined, { type: '@@init' })
    state = reducer(state, sseContextUsage({ slot: SLOT, pct: 12, used_tokens: 120, window_tokens: 1000, rate_limit: QUOTA }))
    state = reducer(state, sseContextUsage({ slot: SLOT, pct: 13, used_tokens: 130, window_tokens: 1000 }))
    expect(state.slotRateLimit[SLOT]).toEqual(QUOTA)
    state = reducer(state, sseContextUsage({ slot: SLOT, pct: 0, reset: true }))
    expect(state.slotRateLimit[SLOT]).toEqual(QUOTA)
  })

  it('replaces the reading with the newer one', () => {
    let state = reducer(undefined, { type: '@@init' })
    state = reducer(state, sseContextUsage({ slot: SLOT, pct: 12, rate_limit: QUOTA }))
    state = reducer(state, sseContextUsage({ slot: SLOT, pct: 12, rate_limit: { status: 'rejected' } }))
    expect(state.slotRateLimit[SLOT]).toEqual({ status: 'rejected' })
  })
})

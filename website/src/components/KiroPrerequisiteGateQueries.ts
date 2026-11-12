import { type KiroPrerequisiteStatus } from '../api/client'

export function kiroPrerequisiteRefetchInterval(
  status: KiroPrerequisiteStatus | undefined,
  kiroGated = false,
): number | false {
  // A closed LTS login gate keeps the fast poll even with `ready`: the
  // probe can latch ready a few seconds before the gate itself reopens.
  if (status?.ready && status.lts_login_required !== true) return 30_000
  if (status && status.setup_allowed === false) return 3_000
  if (kiroPrerequisiteIsBlocking(status, kiroGated)) return 5_000
  return 30_000
}

// True while the full-screen first-run gate is the only thing the user can see.
// Two behaviors key off it: the faster poll above, and forcing that poll to probe
// the HOST rather than read the boot-time latch.
//
// Forcing matters because Kiro Crew no longer performs setup — the user installs
// Kiro CLI from kiro.dev and may sign in from a terminal. Neither of those
// touches the gateway, and the latched status is refreshed only at boot or on an
// explicit request, so a latch-reading poll can never observe them and the gate
// would hold forever behind a Check again button. Bounded deliberately: it costs
// two short `kiro-cli` spawns per interval, runs ONLY on this blocking screen,
// and stops the moment `ready` flips. A returning user reaches it only through
// the out-of-date card below.
export function kiroPrerequisiteIsBlocking(
  status: KiroPrerequisiteStatus | undefined,
  kiroGated = false,
): boolean {
  // An LTS gateway whose login gate is closed serves nothing else, so the
  // sign-in screen blocks and its poll must probe to notice the new sign-in.
  if (status?.lts_login_required === true) return true
  if (!status || status.ready) return false
  // A non-owner cannot probe and is shown the "owner must finish setup" screen.
  if (status.setup_allowed === false) return false
  // The out-of-date card blocks an established install too, and its copy says
  // the page continues once the CLI is updated: a terminal update touches no
  // gateway, so only a probing poll can see it (the server clears a spawn-proven
  // too-old latch only when the probe finds a different binary). Only when a
  // loaded config runs a Kiro harness (*kiroGated*): another harness's user sees
  // the dashboard, not this card, and must not drive host probes behind it.
  if (status.acp_supported === false && kiroGated) return true
  return !status.initial_setup_complete
}


// renders them as the first sentence of a paragraph — terminate them so the
// next sentence does not read as one run-on line.
export function asSentence(message: string): string {
  const trimmed = message.trim()
  if (!trimmed) return trimmed
  return /[.!?:;…]$/.test(trimmed) ? trimmed : `${trimmed}.`
}

# Live scratch — merge-dropped fork deltas found 2026-10-29 session

NOT for commit (docs/handoffs/ must not be committed). Feed into the final
handoff + merge commit when the full-suite verdicts land.

## Fixed this session (electron auto-update fork delta)

Theirs-pick of `website/electron/auto-update.js` + test files dropped the fork
gate. Root shape: upstream refactored updater into `runtime/update/` owners;
the fork delta needed porting into the owner/facade layout.

- `runtime/update/state-reporter.js`: accept `forkRevision` arg, emit it in
  `getInfo()` payload between `packaged` and `downloadUrl` (fork's key order).
- `auto-update.js`: thread `forkRevision` into `createUpdateReporter({...})`.
- `test/auto-update-test-helpers.js`: `forkRevision = ""` opt plumbed into deps.
- `test/auto-update.test.js`: top import + `AUTO_UPDATE_EXPORTS` gained
  `readForkRevision` (facade pin 17→18 names); getInfo key-order pin gained
  `forkRevision` after `packaged`.
- verified: `node --test` both files 91/91; full `npm run test:electron`
  2,874 pass / 0 fail (4 skipped). Vitest half was already green: 3,277 files,
  54,134 passed, 1 expected-fail, 2 skipped.

## Fixed this session (port vocabulary pin)

Upstream added `const adopted = gatewayOwnership !== "none" && ...` in
gateway-supervisor.js `controlState()` — unclaimed sentinel literal.
Claimed in NON_PORT_SITES (supervisor's own ownership bookkeeping, not a
port-probe verdict; same class as the already-claimed initial value).

## FOUND, NOT YET FIXED: fork identity + update suppression (dashboard/backend)

- Installed wheel (~/.kiro/crew-venv, 0.8.0+fork.g496ff4d7) HAS the wiring:
  updates.py imports `is_fork_build, peek_revision, strip_local, warm` from
  fork_version; status_update_fields emits `fork_revision: fork_sha` (line 450)
  and `update_fork_suppressed: bool(_update_info.get("fork_suppressed"))`;
  warm_fork_revision executed off-loop at check time (line 809).
- Repo fork HEAD: wiring ABSENT from dashboard (only fork_version.py defines
  the helpers; state.py kept vestigial params upstream_base_version/
  fork_revision/fork_dirty/update_fork_suppressed with no caller). Installed
  wheel behavior is the reference the user wants kept.
- Merged tree: status_snapshot lives in state_parts/status_payload.py; fork
  params are gone there too (grep fork_revision: only fork_version.py hits).
  AboutPanel reads `status.fork_revision` → badge permanently absent now.
- Port plan (against merged layout): status_payload.py params+keys (fork's
  comments), status_update_fields fork fields, `_update_info["fork_suppressed"]`
  gate in the release-feed verdict path, off-loop warm at check time. Then
  pins. DO AFTER backend suite verdict — check whether existing fork tests
  already caught it (running suite F-count may include them).

## References
- Handoff checklist item still open: "fork_version.py + update suppression"
  semantic audit (handoff next-step 2).
- Electron side of the same feature now fixed (above).

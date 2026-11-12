import type { DashboardState } from '../dashboardSlice'

/** True when the local bundled gateway's policy provider governs this install.
 * Shared by `usePolicyGovernedNotification`, `UpdateFoundModal`, `AboutPanel`. */
export function selectPolicyGoverned(s: { dashboard: DashboardState }): boolean {
  const st = s.dashboard.status
  return st?.update_bundled_by_app === true && st?.update_managed_by === 'command' && st?.update_can_apply === true
}

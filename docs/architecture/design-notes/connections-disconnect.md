# Connections Disconnect: what "disconnected" is allowed to mean

Disconnect used to remove the MCP entry and nothing else. That is not a
disconnection — it is hiding one. kiro-cli's stored grant artifacts stayed on
disk, so the next Connect found a live refresh token and resumed the old grant
without asking, while the card had already told the user this machine's
connection was gone.

This note is about the gap between those two sentences, and the three separate
facts the endpoint now reports instead of one optimistic boolean.

## Three local things, and one thing that is not ours

`POST /api/connections/disconnect` does exactly three things, all local:

| Step | Why it is part of Disconnect |
|---|---|
| dispose any in-flight mint | a grant arriving moments after the user asked for the connection to be gone is not a race worth keeping |
| unlink the stored grant artifacts | this is the step that makes the word mean something; without it a reconnect resumes silently |
| remove the MCP entry from every scope | stop advertising a server the user just disowned |

What it deliberately does **not** do is revoke at the provider. Nothing in this
process can — only the provider can — so the response never claims the upstream
grant is dead, and the card keeps offering the provider's revoke page. The copy
was already honest about this before the grant was actually being deleted; the
change here is that the behaviour finally matches it.

## Why the response carries three answers

The grant artifacts are a **pair** (token + registration), and either half can
fail to unlink on its own. "The token went" is therefore not the same fact as
"the grant is gone", and neither implies the config entry came out.

| Field | Established |
|---|---|
| `grantRemoved` | at least one artifact was unlinked by this call |
| `grantSurviving` | labels of artifacts still on disk **after** the removal, re-stat'd rather than inferred from what the delete loop believed it removed |
| `entryRemoved` | the MCP entry was this provider's, and it is gone |
| `grantSharedWith` | other entries pointing at the same endpoint, which is why the grant was deliberately kept |

A survivor is the one outcome that must not be rounded up. The card renders it
through `role="alert"` rather than `role="status"` — announced by a screen
reader, and pointing the user at the provider's revoke page, because a local
grant outliving the click is precisely the state this endpoint exists to
prevent. The removal itself makes two passes: a transient failure (a lock, a
slow network home) should not strand a survivor, while a third pass would only
delay reporting a failure that is real.

## A grant is keyed by `grant_key`, so it is not always ours to delete

`grant_key` is a sha256 over origin + path — the query string dropped, the path
kept verbatim. **One artifact pair therefore serves every entry whose URL hashes
to that key**, whatever those entries are called. That makes the revoke a wider
act than the purge beside it, and it needs its own ownership question — asked
with the credential's OWN identity function, not the endpoint comparator:

- Entry identity is `normalized_endpoint` on name **and** url — the pair the card
  matches on. It keeps the query (a query can select a different server) and
  strips a trailing slash.
- Grant identity is `grant_key` equality, because the artifacts being protected
  are files *named by* `grant_key`. The two functions disagree in both
  directions: a `?workspace=` variant is a different endpoint but the same
  artifact pair; a trailing-slash variant is the same endpoint but a different
  pair. Testing the credential with the endpoint comparator would delete a
  shared grant in the first case, and in the second skip a revoke nobody needed
  skipped — reporting the stranded live grant as a deliberate keep.

The sharing sweep reads the **raw scope specs** (disabled entries included — a
switched-off server still owns its grant) with the probe view unioned in, so
neither disabling an entry nor holding it only in agent config makes its grant
deletable. Disconnect revokes only when no other entry shares the pair, and says
so through `grantSharedWith` — with artifacts surviving by design reported as a
`status`, never the `alert` a failed unlink earns. When the entry under our slug
is not ours by endpoint, the entry is left alone and the card says that too,
rather than reporting a removal that did not happen.

**Cancel never revokes at all.** A cancelled *new* connect is the sharpest form of
the same hazard: it suppresses its own feedback, so a shared grant would vanish
silently. Cancel keeps the entry-only removal it always had; only a deliberate
Disconnect touches the credential.

Both ownership questions are answered from a **single** locked pass, because they
gate different destructive acts and must not disagree with each other or with the
purge. The purge itself goes through `_offload_config_write`, not a bare
`to_thread`: a cancelled request task would otherwise release the lock while the
worker is still rewriting the store, letting a concurrent purge interleave with a
stale snapshot.

Four residuals, stated rather than papered over:

- The revoke happens *after* the locked section, so an entry created at the same
  endpoint inside that window loses a grant it has not yet used. Holding the lock
  across the unlinks would stall every config writer for as long as a
  network-mounted home does.
- The entry-identity check sees only each name's priority winner
  (`list_servers()` returns one row per name) while `_purge_server_config`
  deletes that name from every scope, so a same-named entry in a lower-priority
  scope with a different URL is still removed unseen. Closing that needs a
  scope-aware purge in `handlers/mcp.py`, which this slice does not touch.
- The handler awaits `cancel_mint` before revoking, and a wedged mint teardown is
  bounded only by its own shutdown timeout, so a Disconnect click can stay busy
  for up to that long. Accepted: firing it as a task would let a grant arrive
  after the user asked for the connection to be gone.
- The endpoint takes the MCP file lock but not the `/api/mcp/apply` mutex, so a
  concurrent apply and a Disconnect can interleave at the transaction level even
  though each file write is individually consistent. The apply path documents
  that mutex as its own serialization; folding Disconnect under it belongs to the
  same follow-up as the scope-aware purge.

## Identity is the endpoint, never the entry name

The card matches a provider to an entry on **name and url** together
(`connectionProviderForServer`). The purge honours the same pair, and reads the
inventory *inside* the MCP file lock rather than before it.

Removing by name alone would mean a user's own server that merely happens to be
called `notion` is deleted because they clicked Disconnect on the Notion card.
This is the rule `l1_smoke` already keeps for the same reason: a registry slug
is a label a caller can collide with, while an endpoint is the thing being
talked to. When the configured entry is not ours, `entryRemoved` is false and
the entry is untouched — but the grant is still revoked, because the grant is
keyed on the registry URL and never on whatever `mcp.json` currently holds.

## The credential boundary

The artifacts are stat'd and unlinked. They are never opened. kiro-cli owns the
OAuth chain and its store ([mcp-oauth-ownership.md](mcp-oauth-ownership.md)); the
gateway may observe and delete, never read. That is the same boundary
`grant_present` keeps, and a regression test pins it by making `open`,
`read_text` and `read_bytes` raise for the duration of a revoke.

Both reads run off the event loop for the same reason `grant_present` does: they
stat paths under the user's home, which stalls as long as a network mount does.
`mint.py` carries a fixed-point guard asserting no coroutine in the module
reaches the filesystem directly, so this is enforced rather than remembered.

The single-file `{sha256}.json` form that shares the cache directory belongs to
AWS SSO and is deliberately never touched.

## Deferred: proving the grant still works

Disconnect is the trust half of this slice. The other half — upgrading **Test**
from an MCP-level probe to an authenticated round-trip — is not here. It needs a
provider HTTP probe and a real runtime activation (kiro-cli holds the bearer, so
only the runtime can present it), plus a verdict vocabulary reconciled against
the narrower status enum that shipped with the tiers note. Test is not broken
today; it performs a real probe, just a shallower one than its name suggests.
Splitting it keeps the security-relevant fix from waiting on the expensive one.

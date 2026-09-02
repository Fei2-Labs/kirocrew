# Claude Code backend — dormant ACP seam

## Public provider boundary

`AgentConfig.provider` admits only the ACP provider. Claude Code is not a second
`LLMProvider`, an API-key provider path, or a dashboard provider choice. Harness
selection remains at `agent.acp_backend`, and `kiro-cli` remains the default.

This fork's baseline selectable set contains Kiro, KAS, GitHub Copilot, and
OpenCode. Claude is known and described, but deliberately absent from
`acp_backends.BASELINE_SELECTABLE_BACKENDS`. Consequently,
`acp_backends.resolve_selected_backend()` normalizes a persisted `"claude"`
selection to Kiro. An edition may make the seam reachable by calling
`acp_backends.register_selectable_backend(ACP_BACKEND_CLAUDE)` before config is
loaded; the ordinary `DefaultProviderRegistry` then constructs a
`SpecAdapterAcpProvider` for it without adding another provider value.

Claude stays withheld because Kiro Crew's routing seed may merge one key into an
operator-owned project settings file, while the current reset path removes that
whole file. Selection must remain closed until cleanup can remove only Kiro
Crew-owned state.

## Adapter process and credentials

`AcpClient._spawn` retains a positive Claude branch. `_resolve_claude_acp_bin`
searches an explicit override, complete project-local or vendored installs, mise,
and an augmented PATH, and returns both spawn argv and the PATH actually searched
for accurate diagnostics. `_resolve_claude_code_executable` independently finds
the Claude CLI used behind the adapter. Kiro Crew never bundles either binary.

A caller-supplied `CLAUDE_CONFIG_DIR` in `extra_env` is forwarded to the child; core
does not synthesize one or claim isolated Claude configuration by default.
`CLAUDE_CODE_EXECUTABLE` is set when the resolver finds the CLI and otherwise left
unset so the adapter reports its own installation error.

The Claude CLI owns sign-in. Kiro Crew stores no Claude token and never reads one,
but the descriptor names `.claude/.credentials.json` so the shared sensitive-path
floor prevents an agent from reading or rewriting the vendor-owned credential.

## Tool-gate routing

Claude is a public-spec adapter. It runs one process per session and does not gain
Kiro session sharing, MCP Tool Search, native agent-profile enforcement, or
mid-turn steer. Reasoning effort, slash commands, usage, and billing are adapted
through the generic spec-adapter seams where available.

The adapter decides whether to ask the client from
`<work_dir>/.claude/settings.local.json`. `acp/claude.py` reads
`permissions.defaultMode` back from that exact path. When no mode exists,
`ensure_routed_settings()` merges `"default"` into the file; this makes each tool
decision arrive as `session/request_permission` and reach Kiro Crew's PreToolUse
gate. It never overwrites an explicit mode. A bypass mode remains `BYPASSED`, an
unknown or unreadable mode remains `INDETERMINATE`, and either refuses the session
unless the named `agent.acp_backend_allow_ungated_tools` opt-out is enabled.

The optional `_write_claude_local_settings` hook may add companion-owned settings
such as a model allowlist, but it is not the security seed. Routing is established
by `tool_gate.enforce()` and verified by reading the adapter's settings path back.
`AcpClient._reset_state` removes the per-work-directory settings file, which is
why the backend remains nonselectable in this build.

Binary-resolution and wire details are also specified in
[`../modules/acp-client.md`](../modules/acp-client.md). Operator-facing adapter
status and installation guidance live in
[`src/kiro_crew/docs/experimental-acp-adapters.md`](../../../src/kiro_crew/docs/experimental-acp-adapters.md).

## Model registry

`src/kiro_crew/model_registry.json` is the shared model data source for
`model_registry.py` and `website/src/model_registry.json`.
`test_frontend_registry_matches_python_source` compares their parsed JSON, and
`website/src/providers/modelRegistry.ts` imports the frontend copy. Per-entry
`claude_code` provider IDs are mappings for this dormant adapter, not values
accepted by `AgentConfig.provider`.

`model_registry._build_indices` indexes canonical keys, provider IDs, and aliases.
`from_provider_id` uses that index to recover a canonical key from an advertised
adapter ID. `TestModelRegistry.test_bare_advertised_ids_fold_to_canonical_key`
pins the bare-ID case.

`model_registry.available_models` and `display_list` sort default entries first
rather than trusting JSON object order. This is load-bearing because the adapter
uses the resulting allowlist when automatic selection omits an explicit model.
`TestModelRegistry.test_fable_5_not_default` and
`TestModelRegistry.test_available_models_is_default_first` pin the default and
ordering behavior.

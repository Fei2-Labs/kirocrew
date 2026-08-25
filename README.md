# Kiro Crew (Fei2-Labs fork)

A fork of [Kiro Crew](https://github.com/kirodotdev/KiroCrew) — the open-source
personal AI agent — extended with **GitHub Copilot** and **BYOK
(bring-your-own-key)** support.

Kiro Crew runs an LLM-driven agent that lives alongside you rather than in a
single terminal tab. You can talk to it from a web dashboard, the CLI, or a
messaging channel like Slack or Discord, and it stays the same agent with the
same memory across all of them. It can run multi-step tasks unattended in the
background, get scheduled to run on a cron so it acts on its own on a
schedule, and keep memory across sessions so it doesn't start from zero every
time you open a new chat. Under the hood it drives an LLM through the KiroACP
provider (an ACP JSON-RPC adapter around `kiro-cli`) plus a set of MCP tools,
with a security layer that governs which tools and commands the agent may run.

This fork exists because the upstream project ties the agent to a single
first-class LLM harness. If you already pay for GitHub Copilot, or you'd
rather bring your own API key for a specific model provider, upstream doesn't
give you that choice. This fork adds it without touching upstream's security
model, dashboard, or architecture.

## What this fork adds

Upstream Kiro Crew drives a single first-class harness (`kiro-cli`) over the
KiroACP provider. This fork extends that with two additional backends you can
select from the same agent configuration:

- **GitHub Copilot support** — use a GitHub Copilot subscription as an LLM
  backend, so you can run Kiro Crew against models served through Copilot
  instead of the default `kiro-cli` harness. Useful if Copilot is already
  covered by your employer or you prefer its model lineup.
- **BYOK (bring-your-own-key)** — plug in your own API key for a supported
  model provider (via an OpenCode ACP backend) instead of being tied to the
  default backend. This gives you direct control over cost, provider
  entitlement, and exactly which model answers each request, and it reads the
  models you've already configured in your own OpenCode config rather than
  whatever a limited ACP advertisement exposes.

Both additions are adapters at the ACP backend-selection seam
(`agent.acp_backend`): they plug into the same tool-approval flow, the same
PreToolUse security gates, and the same session/slot model as the default
`kiro-cli` harness. Switching backends does not bypass Kiro Crew's own
governance layer — a tool call is still evaluated by Kiro Crew's own security
hooks regardless of which harness is answering.

Everything else — the dashboard, CLI, messaging channels, cron jobs, persistent
memory, subagents, and the security model — carries over from upstream.

## Relationship to upstream

This is a downstream fork. Core architecture, docs, and behavior track
[kirodotdev/KiroCrew](https://github.com/kirodotdev/KiroCrew); see that
repository for the full documentation tree. Fork-specific additions (Copilot and
BYOK integration) are documented here.

## Contributors

Thank you to everyone who has opened a merged pull request against this
fork. Upstream Kiro Crew credits its own contributors in the
[upstream README](https://github.com/kirodotdev/KiroCrew#contributors).

<a href="https://github.com/clarezoe" title="clarezoe"><img src="https://github.com/clarezoe.png?size=64" width="64" height="64" alt="clarezoe" /></a>

If you contributed and would like to be credited differently or removed,
please open an issue or a pull request.

## License

Inherited from upstream Kiro Crew.

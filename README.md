# Kiro Crew (Fei2-Labs fork)

A fork of [Kiro Crew](https://github.com/kirodotdev/KiroCrew) — the open-source
personal AI agent — extended with **GitHub Copilot** and **BYOK
(bring-your-own-key)** support.

Kiro Crew lets you chat from a web dashboard, the CLI, or a messaging channel
(Slack, Discord); run multi-step tasks unattended; schedule cron jobs; and keep
memory across sessions.

## What this fork adds

Upstream Kiro Crew drives a single first-class harness (`kiro-cli`) over the
KiroACP provider. This fork extends that with:

- **GitHub Copilot support** — use a GitHub Copilot subscription as an LLM
  backend, so you can run Kiro Crew against models served through Copilot.
- **BYOK (bring-your-own-key)** — plug in your own API key for a supported
  model provider instead of being tied to the default backend, giving you
  control over cost, entitlement, and model choice.

Everything else — the dashboard, CLI, messaging channels, cron jobs, persistent
memory, subagents, and the security model — carries over from upstream.

## Relationship to upstream

This is a downstream fork. Core architecture, docs, and behavior track
[kirodotdev/KiroCrew](https://github.com/kirodotdev/KiroCrew); see that
repository for the full documentation tree. Fork-specific additions (Copilot and
BYOK integration) are documented here.

## License

Inherited from upstream Kiro Crew.

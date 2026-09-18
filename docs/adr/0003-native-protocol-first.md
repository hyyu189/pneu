# ADR 0003 — native protocol first

- Status: accepted, 2026-08-27
- Interface ranking and integration sequence below superseded by
  [ADR 0005](0005-native-admission-validation.md); native semantics remain.

## Context

Every harness offers different control surfaces: Codex a first-class App
Server protocol with durable thread/turn/item semantics; DeepSeek Harness a
native plugin/event architecture; Claude Code lifecycle hooks; others only a
TUI. Universal agent protocols flatten exactly the semantics that make each
integration trustworthy (busy-turn safety, exact-session identity, resume),
and lowest-common-denominator wake paths (keyboard injection, headless
replacements) have already been retired here as failures.

## Decision

When a harness provides a maintained native control protocol, pneu uses it.
Adapter priority: native control protocol → native plugin/extension →
stable external injector with exact session identity → model-armed monitor
→ keyboard injection (retired legacy only). Harness semantics stay native:
notify, append-context, start-turn, steer-turn, and resume remain distinct
operations, never one generic `wake()`. A minimal common adapter contract
(discovery, binding, capability negotiation, health, structured errors,
opaque native IDs) is derived only after concrete integrations — reference
sequence Codex App Server, then Herdr as the surface reference, then
DeepSeek Harness — and never standardizes thread/turn/tool semantics.

## Consequences

- Adapter contracts pin to the schema generated from the installed vendor
  binary, and integrations are version-pinned until live-validated.
- Support is a per-capability matrix with recorded evidence, never one
  boolean.
- HarnessAdapter and SurfaceAdapter stay orthogonal; combinatorial adapters
  are forbidden.
- pneu never creates a parallel headless conversation and calls it
  adaptation.

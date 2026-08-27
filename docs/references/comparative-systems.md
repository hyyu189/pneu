# Comparative systems — what pneu borrowed and rejected

> Status: reference, rebaselined 2026-08-27. Never a product mandate.
> The source-grade teardowns of Paseo, Warp, and Orca/ADE live in the
> maintainer's local `research/` notes, deliberately untracked to keep
> competitive research out of the public repository; this document carries
> the durable conclusions.

## The comparison at a glance

| System | Owns conversation/agent loop | Owns terminal/surface | Imports history | Product shape |
| --- | --- | --- | --- | --- |
| pneu | No | No; navigates surfaces | No | thin local control plane |
| ORG2 | Yes (native harness) + managed external runs | Yes, full desktop workspace | Yes, extensively | ADE + system of record |
| DeepSeek Harness | Yes | Own web/headless/SDK surfaces | Own durable session log | extensible harness platform |
| Herdr | No | Yes, persistent terminal runtime | No | terminal/session runtime |

These systems overlap technically but solve different ownership problems.
Borrowing an implementation pattern without preserving that distinction
would expand pneu into a different product. pneu's advantage is the inverse
of the ADE promise: **it coordinates sessions without forcing users to
relocate into a new development environment.** That boundary is a product
moat, not a missing feature list.

## ORG2

ORG2's "20+ agents supported" spans several integration depths: a
first-party agent loop; managed structured CLI runs parsed into a
normalized timeline; native protocols (Codex App Server, ACP, hooks) where
available; embedded TUI hosting with injected session identity; a generic
launcher tier where "support" means launch/configure only; and deep
provider-specific local-history ingestion.

Borrow: the capability taxonomy (detected → launchable → bound → status →
wake → resume → history → live-validated); one canonical harness descriptor
for launch metadata; a native-session **binding ledger** (current and
superseded bindings with timestamps and reasons); provider-specific
empirical validation (exact resume form, cwd requirement, id shape, busy
behavior, validated version); explicit depth labels in UI and docs.

Reject: transcript ownership and history ingestion (privacy, retention,
schema drift — a system-of-record obligation); universal tool-call
normalization; the full workspace/task/role organization, key vaults, and
review UI; broad vendor-config editing beyond marked, reversible fragments.

## DeepSeek Harness

Demonstrates first-class native plugin seams: services and typed events,
reversible effects, durable session events distinct from live agent events,
profiles/bundles for composition.

Borrow: native plugin integration over external control hacks; the explicit
separation of durable facts from live events; reversible lifecycle effects;
capability seams. Reject: importing the agent/session/tool architecture into
pneu; assuming pneu owns the session log or model-visible context; building
a universal Cordis-like framework inside a small Python product. Its
developer-preview status also validates version pinning and conservative
support claims.

## Herdr

Owns persistent terminals, reattachment, pane state, and agent-aware status
while running existing tools unchanged.

Borrow: explicit pane/tab addresses as surface capabilities;
focus/jump/open-worktree APIs; status and attention signals; the clear
statement that it owns terminals, not the harness conversation. Reject:
terminal ownership as a pneu requirement; pane identity as delivery truth;
keyboard prompt injection as a wake strategy; a second terminal runtime
inside pneu. The relationship is compositional: Herdr may be one surface
adapter while the harness adapter handles the native session.

## Full ADEs (ORG2, Orca, Paseo) and Warp

Full ADEs legitimately own conversation rendering, orchestration, replay,
approval UI, and process lifecycle — a different product promise, not a
wrong architecture. The deep teardowns in `research/` add the specific
negative lessons pneu is built against: a delivery obligation held in an
in-memory closure silently dies with the daemon (Paseo — pneu's "the file is
the delivery" exists to make that impossible); asserted identity via a
daemon-wide token vs pneu's per-call revalidated capability chain (Paseo);
"the seat exists because a pane exists", keystroke-injection wake, and
unconsented multi-agent config edits (Orca); account-and-cloud round trips
for all multi-agent behavior (Warp). Worth stealing from that cohort:
version-matched skill docs served by the executing binary, and an explicit
context budget with "more messages queued" deferral.

## Distilled decisions

1. Many-harness support is a capability matrix, never one boolean.
2. Deep detail comes from native protocols, structured output, hooks, and
   provider-specific stores — not a magic abstraction.
3. pneu owns live coordination, not retrospective transcript normalization.
4. Rich harnesses keep their native semantics.
5. Terminal hosts are surface adapters, not harness adapters.
6. The generic adapter boundary is extracted after concrete integrations.
7. A new integration must prove it reaches the user's real visible session.
8. Product scope stays thin even where adapter engineering is deep.

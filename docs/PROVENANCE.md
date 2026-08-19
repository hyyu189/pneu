# Build Week provenance

This document makes the development boundary reviewable. It is not a claim that
the name “Roundtable” or every historical utility began during Build Week.

## Chronology

### Before the submission period: public cmux-centric v1 predecessor

The earlier implementation is publicly preserved, unchanged, as
[`hyyu189/h2o`](https://github.com/hyyu189/h2o). The exact public snapshot used
for this audit is:

- public commit `50683056c896bdb1ae2f74f6ac0740106b43bd36`;
- MIT-licensed;
- no Git tag or GitHub Release at the time of this audit.

That snapshot records a sync from the private development history at
`dc6f1e61d3fdf8c6b88c34b8af62bebd58be077f` and earlier commits. The public
snapshot therefore corroborates the pre-period baseline; it does not turn the
pre-period work into Build Week output. v1 coupled delivery to cmux topology,
surface identity, and keyboard-oriented wake behavior. It had a materially
different user and runtime architecture from Messaging v2.

The h2o repository will remain an unchanged historical predecessor rather than
being overwritten by this release. Any applicable v1 material retained in the
new implementation keeps its MIT notice in `NOTICE`; this repository as a
whole is distributed under Apache License 2.0.

### Submission period: Messaging v2 rewrite

From 2026-07-15 through 2026-07-17, eighteen source commits from
`0d49729dd2f56037158a57f6abe11224773e28ae` through
`bbc67d8f42a238e4887d389cb6766dc57ae76e47`, inclusive, replaced the delivery
architecture with:

- atomic per-project maildir delivery;
- drain, acknowledgement, and archive semantics;
- native Codex app-server wake and self-registration;
- offline persistence and quiet acknowledgement handling;
- project registry, launchers, diagnostics, and recovery controls;
- retirement of v1 keyboard delivery.

This is the architecture and deployable implementation evaluated as the Build
Week deliverable. The complete source-to-public mapping lives in
`docs/provenance/source-commits.tsv`.

### Public productization

This repository was initialized on 2026-07-18. Work starting with its first
commit covers sanitization, packaging, installation and removal, current Codex
compatibility, standalone validation, cmux decoupling, portable tests,
documentation, release artifacts, and the judge demo. This phase is led and
implemented through GPT-5.6 in Codex.

The release narrative treats the public h2o snapshot as predecessor evidence,
not as a release of this repository and not as work newly performed during the
submission period.

## Contributor boundary

Ocean is the human product lead and made the key product and architecture
decisions.

GPT-5.6 through Codex was the primary implementer of the Messaging v2 core and
is the sole AI development lead for public-repository productization unless the
human lead explicitly changes that plan.

Fable 5 self-reported the following historical roles for the seventeen source
commits after `0d49729`:

- direct code: `30c704e`, `7b1752f`, `1fecae8`;
- direct documentation or configuration: `706cb68`, `b545b29`, `fc26072`,
  `b716987`, `0d7f78e`, `0686cde`;
- design/specification and review, with implementation by GPT-5.6/Codex:
  `4651bc9`, `028008a`, `c691a0c`, `286db88`, `05247b8`, `b70a575`,
  `ec6f1f5`;
- specification for `bbc67d8`, implemented by GPT-5.6/Codex without a Fable 5
  review.

The requested Git range, `0d49729..bbc67d8`, correctly contains seventeen
commits because a two-dot range excludes its left endpoint. For the public
audit, the inclusive rewrite range is `0d49729^..bbc67d8` (eighteen commits).
A cross-repository audit found the corresponding Fable 5 specification and
approval plus Codex implementation evidence for the additional first commit,
so the ledger classifies `0d49729` as design/review by Fable 5 and
implementation by GPT-5.6/Codex. The original self-report remains unchanged.
Public commit messages preserve confirmed co-author attribution.

Hermes contributed harness-side reconnaissance and adapter work in the design
repository. No Hermes implementation is represented as imported product code
unless it is added to the source ledger explicitly.

## Pre-period material found inside period commits

Two known exceptions must not be counted as new Build Week implementation:

1. `30c704e` accidentally included `bin.bak-20260623T183827/`. The entire
   backup tree is excluded from the public import.
2. `b716987` created `docs/legacy-v1-keyboard.md` from the pre-period v1 skill
   documentation. It is retained only if needed as clearly labelled historical
   migration context.

Fable 5 reported no other known pre-period carry-over. That is a contributor
statement, not proof that the replay is clean. The independent path and content
audit remains a release gate; any additional case it finds must be recorded
here and in the ledger before release.

## Evidence and privacy policy

Evidence used for the audit includes dated Git objects, local Codex rollout
records, focused test results, and a concise Fable 5 self-attribution record.

The public repository does not contain:

- raw Codex, Fable 5, or Hermes transcripts;
- Roundtable mailbox archives or runtime registries;
- private session URLs;
- absolute personal paths, unrelated project names, credentials, or tokens;
- backup directories or untracked local drafts.

The Devpost submission will identify the primary Codex build session through
the required `/feedback` Session ID. Supporting session evidence is summarized
without publishing unrelated conversation content.

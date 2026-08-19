# Credits

Roundtable Messaging v2 is directed and maintained by Ocean (Haiyang Yu).

## Build Week roles

- **Ocean** — product direction, architecture decisions, requirements,
  validation, and final editorial authority.
- **GPT-5.6 through OpenAI Codex** — primary Messaging v2 implementation,
  regression work, runtime integration, and all productization begun in this
  public repository.
- **Claude Fable 5** — early protocol design and implementation review; direct
  implementation of the Claude-side tripwire and stop-gate components; and
  supporting documentation and configuration. The exact source commits are
  recorded in `PROVENANCE.md` and `docs/provenance/source-commits.tsv`.
- **Hermes** — harness-side adapter reconnaissance and specification work in
  the supporting design repository.

AI contribution labels describe the development process; the Git history and
license remain the authoritative legal records.

## Earlier public predecessor

The cmux-centric v1 predecessor remains available, unchanged, in
[`hyyu189/h2o`](https://github.com/hyyu189/h2o) at public snapshot
`50683056c896bdb1ae2f74f6ac0740106b43bd36`. It is MIT-licensed and predates
the Build Week Messaging v2 rewrite. Its existence and contributors are not
presented as new Build Week work; see `PROVENANCE.md` and `NOTICE` for the
development and license boundaries.

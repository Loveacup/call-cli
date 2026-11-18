# Reviewed deployed-source publication (2026-11-18)

This update publishes the reviewed observation/control R2 runtime and its preceding dialogue/recovery repairs, on top of remote main `70f7be377bffa4f3acd09925ee8aad0c2d5d66e7`.

## Included behavior

- Strictly read-only `list`, `status`, `events` and exact-task `recovery_status` across Hermes, CLI and OMP: SQLite read-only/query-only access, no implicit initialization, persistent host bind, intervention advancement or polling activation.
- Cross-route resource diagnosis exposes audited closed-set values and counts, not business text, paths, routes or bulk task discovery. Existing business/control authorization remains separate.
- A settled pending intervention advances through explicit same-`command_id` `intervene` retry or CLI/OMP `wait`, not observation.
- Exact multi-question `question_ids` binding, retry-safe intents and explicit `blocker_interaction_seq` supersession of incomplete questions. Dynamic forwards multi-question metadata but rejects unsupported blocker supersession.
- Native history reconciliation and replay deduplication, ended-owner durable-receipt recovery, reset/compression lineage boundaries, settled legacy recovery and old-task closure without reclaiming a successor's writer reservation.

## Source parity and publication boundaries

The reviewed/deployed manifest contains 126 product files. All runtime Python/JavaScript and plugin resources remain byte-identical to that manifest. The publication preserves existing remote assets, public historical verification records and previous fixture/example sanitization. No runtime dependencies were added; the Python core remains standard-library based, and existing optional ACPX/Node/host dependencies remain documented in the integration guide.

Intentional differences from the deployed snapshot are documentation-only publication qualifications in `docs/integrations.md`, `docs/usage.md`, `skill/SKILL.md`, and the skill references `core-requirements.md`, `hermes-host.md`, `native-commands.md`, `online-collab.md`; these replace stale author-deployment/review labels with installation qualifications. Three trailing blank lines are removed from `test_agy_native_owner.py`. This summary is new. No runtime behavior was adapted for publication.

Only explicit product inputs are staged. Live databases, logs, credentials, private plans, device bindings, deployment receipts and unrelated working-tree changes remain excluded. Publication does not modify the installed service, restart a host, update credentials, or release existing task locks. Old sessions may retain earlier tool schemas; source availability does not refresh them.

## Verification scope

Privacy review includes a whole-tree credential-pattern scan and detect-secrets scan. Flagged values were existing public attribution, exclusion prose, integrity hashes and synthetic test canaries, not live credentials. This is a bounded review, not a security certification.

Post-push acceptance is performed from a fresh clone of the exact published commit with isolated temporary/state directories, covering observation/control, dialogue/history, recovery, continuity and OMP integration fixtures. The repository has no GitHub Actions workflow at preparation time; no CI pass is implied. Local test results are reported against the final remote SHA, separately from earlier deployment acceptance.

Offline/synthetic tests do not prove live provider behavior, Windows/PowerShell execution, descendant cancellation, original-session model consumption, user-visible delivery or a full host installation. No live model calls or production database changes are part of this publication.

# Reviewed deployed-source publication (2026-11-16)

This update publishes the reviewed deployed runtime, not the entire maintainer working directory. It extends the existing native-skip/resource-recovery release with:

- AGY public-stream drain and history reconciliation, bounded retry for inverted tool replay, and preservation of live observations across replay;
- accepted-but-not-sent dialogue withdrawal/supersession using `target_command_id` and `withdraw`, without hot steering or replay;
- origin-local `group` filtering, sanitized display-only `origin_context`, and `runtime_links` for exact task/session/process navigation;
- forwarding safe diagnostic events to OMP follow-ups;
- explicit protocol-settled missing-final-answer evidence: business outcome stays unknown; strict `release_only` may close the bound idle native record once after probe/fence checks. Running or uncertain native status still refuses external close.

## Source closure and exclusions

The deployment evidence froze 121 source/resource paths. Runtime Python/JavaScript is byte-identical to the reviewed deployment. Public test fixtures replace a personal Windows username with `example`; a skill example uses `/absolute/call-cli` rather than a maintainer checkout. These are fixture/example changes, not runtime behavior changes.

The update starts from the current remote main and retains its newer bilingual READMEs, diagrams and public historical verification record. Private `PLAN.md`, local operational verification narratives, deployment receipts/manifests, machine bindings, logs, task state, credentials and unreviewed isolated native-cancel candidates are not distributed. The public summary here replaces private operational detail. Deployment and publication remain separate: this push neither changes installed entrypoints nor restarts a host.

## Acceptance and remaining boundaries

Post-publication checks must use the exact pushed commit in a fresh checkout; historical suite counts elsewhere are not fresh results. The repository currently has no GitHub Actions workflow, so no Actions CI result is implied by publication.

Offline fixtures do not establish live provider operation, arbitrary descendant-stop proof, cross-device Windows acceptance, original-session model consumption or user-visible delivery. Direct child/phase/log events and Dynamic cancellation cascade/hot script steering are not established. General asynchronous wake-up for an idle initiating CLI remains roadmap work, not a new daemon in this release. Unknown outcomes retain HOLD; explicit abandonment releases coordination ownership, not business success or native stop proof.

# Security

Call CLI is an experimental, single-user coordination tool, not a multi-tenant execution service or a security sandbox. Review the source and the target CLI's permission model before granting access.

## Trust boundaries

- SSH and the target agent run as the configured operating-system user. `--write` selects ACPX `approve-all`; it does not constrain writes to a directory.
- Treat prompts, workspaces, project ACPX configuration, agent commands, skills and remote hosts as trusted execution inputs. A project configuration can affect what program is launched.
- Client IDs are local capability handles, not authenticated user identities. Keep the task database, handles, prompts and answers private; do not expose the CLI as an unauthenticated public API.
- ACPX authentication is resolved on the execution device. Never put real credentials into task payloads, examples, issue reports or Git. Target agents may separately retain their own sessions and logs.
- The coordinator stores selected public events, not raw protocol streams. This does not guarantee that an assistant's public answer cannot contain sensitive task data; review exports and reports.
- Cancellation acknowledgement is not termination. After disconnects or unknown results, reconcile the exact native session and process before retrying or transferring write ownership.
- The Windows desktop bridge uses temporary loopback authentication and a one-shot scheduled task. It preserves the Codex sandbox but is not an OS user-isolation boundary; it requires the user's logged-in desktop.
- A first turn may perform an ACPX patch update after the code's source/version/busy checks. There is currently no disable-update setting. Review this behavior before use in a frozen environment.

## Reporting

Never publish credentials, raw transcripts or live exploit details in a public issue. Use GitHub's private vulnerability reporting through **Security → Report a vulnerability**, or [start a private report](https://github.com/Loveacup/call-cli/security/advisories/new). If that option is unavailable, do not fall back to posting sensitive details in a public issue.

Report the affected commit, operating system, CLI/ACPX versions, minimal synthetic reproduction and expected versus observed behavior. Redact hostnames, usernames, private addresses, task/client IDs and customer data where unnecessary. A targeted review or passing fixture is not a comprehensive security certification.

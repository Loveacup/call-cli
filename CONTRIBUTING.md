# Contributing

Call CLI is intentionally small: a POSIX coordination core, existing ACPX/SSH transport, portable clients, an optional skill and an optional Hermes adapter. Prefer fixing a demonstrated gap over adding another service or control plane.

## Start with a bounded change

Describe the goal, affected entry point and acceptance criteria. Keep new features separate from unrelated refactors, platform workarounds and dependency upgrades. A documentation or design discussion is not authorization to change an installed service or publish user data.

For bug fixes, reproduce the observed failure first, add a regression that fails on the old code, then make the smallest sufficient change. Distinguish transport fixtures, real native queue-owner integration, live models and installed-entry verification.

## Local checks

The Python core uses the standard library. Use a private scratch directory for temporary files and keep `evidence/`, databases, logs and authentication outside commits.

```sh
python3 -B -m unittest discover -v
node --test test_shared_prompt.cjs
node --test test_codex_ssh.cjs
node --check remote-client.cjs
git diff --check
```

The optional native integration creates and closes owned local ACPX sessions using a clearly synthetic ACP endpoint and isolated HOME; it makes no provider call:

```sh
RUN_NATIVE_ACPX_TESTS=1 python3 -B -m unittest -v test_native_shared
```

Windows PowerShell client tests require Windows and a controlled fake SSH fixture; passing Node/Python tests does not certify them. Live model tests require explicit execution scope and existing authorized authentication. Close owned sessions and reconcile uncertain work before repeating a business request.

## Preserve these contracts

- New sessions only; no takeover of manual interactive sessions.
- One lifecycle owner and writer per task/resource.
- Exact client/task/question/command IDs and turn-guarded state changes.
- Bounded wait, cursor-based events, and honest `busy/unknown` results.
- Public Shared Runtime results for settlement; no internal version-hashed imports or broad error suppression.
- Device-local authentication; no credentials or raw thought/protocol logs in task transport or commits.
- No claim of cancellation, installed operation or end-to-end success without evidence for that exact scope.

Include a concise summary of changed behavior, commands actually executed, their results, untested scope and compatibility impact. Do not present model-generated example output as observed execution. Public-readiness work and a repository visibility change are separate operations.

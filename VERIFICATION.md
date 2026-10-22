# Verification — v0.2.3

## v0.2.3: Shared Runtime settlement

A real OMP review exposed an allowed cross-direction JSON-RPC ID reuse: the active prompt and a recoverable filesystem callback both used ID 2. The CLI JSON formatter omits direction, so the old decoder conservatively returned `unknown` even after `exit 0 / end_turn`. A direction-only decoder prototype was tested but not retained because it could not recover information the CLI had discarded.

The candidate uses the formally public `acpx/runtime` `createSharedAcpRuntime()` API and its authoritative `turn.result`, not version-hashed internal imports. The existing CLI still creates, cancels and closes named sessions; the native record ID is persisted and the short-lived facade submits to the same CLI queue-owner. It does not create an independent direct SDK owner or replay a failed business request. The facade and prompt travel over stdin through existing local/SSH execution, with only public assistant text and minimal tool status forwarded. Foreign, duplicate, missing, failed and inconsistent terminal results remain rejected. Detaching a shared client is not reported as cancellation.

Candidate verification (not installed-release certification):

- Default Python discovery: 59 cases, exit 0, with the opt-in native integration case skipped (58 passed). Node facade: 11 cases passed, exit 0. Both review findings were reproduced RED before their fixes: execution-device auth was absent from the runtime factory, and a second turn incorrectly retained the first turn's version after a simulated compatible update.
- Opt-in native integration, no model: a synthetic ACP endpoint ran through the actual Node bootstrap, ACPX Shared Runtime and queue-owner, with an isolated HOME so no real user auth config reaches the fixture. Its read callback deliberately reused the active prompt ID and received a real missing-file error from the native client. The old decoder fails on the observed protocol shapes; the candidate reaches `completed / exit 0 / end_turn`. A second case requires a synthetic credential from the project auth config and also completes. A genuine main-prompt error remains `unknown / exit 1`; a blocked prompt cancelled through native control reaches `cancelled / exit 0`. Each of the four endpoints observed one prompt, and all owned sessions were closed with status readback.
- Before the final auth/version compatibility adjustments, fresh Mac mini OMP and PC Pi models read random fixture values correctly and completed with exit 0/end_turn. The OMP run did not itself produce a colliding callback error; neither run substitutes for the exact native-protocol test above.
- On the final runtime bytes, a fresh PC Pi model over unchanged Windows SSH asked a real numbered question, settled to `waiting_reply`, consumed the coordinator's answer, read a random code plus Chinese text in turn 2, and reached `completed / exit 0 / end_turn`. Its native record ID was unchanged across turns. This is a bounded coordinator-driven question/reply smoke, not a claim of actual package upgrading or user typing. All three real-model test sessions were closed and read back.

Auth is resolved locally on the execution device with the installed CLI's global/project precedence; literal credentials and auth policy are passed only into that device's native runtime, never the controller payload/events. Each turn obtains the current installed version under the existing shared installation lock; the old `version_check` remains historical and current `execution_version` is distinct. The compatible-update regression simulates the installed-version change without changing any real installation.

The final protocol/auth/cancel checks and final two-turn PC model run bind the same runtime bytes. Earlier model evidence retains its earlier hashes rather than being relabelled final. Independent targeted re-review passed both auth/version findings and independently reran Node 11 cases, runner 9 cases and the native integration's four scenarios; it did not rerun the full Python discovery suite. This scoped PASS is not an audit of every project feature. Evidence remains local under `evidence/shared-runtime-fix-*` and `evidence/shared-*.json`; no raw private ACP streams are copied into the repository. The synthetic endpoint is clearly labelled in `fixtures/native_shared_agent.py`; its opt-in test creates local owned ACPX records and short-lived owners but makes no provider call.

**Deployment is verified separately from source tests.** `shared-prompt.cjs` must be packaged beside `runner.py`, including in the physical Hermes plugin copy. Versioned payloads, native skill targets and the plugin files require hash readback; publishing source alone does not update old installations. This release needs existing Node/npm and an ACPX installation exposing the public shared runtime (tested at 0.20.0). It does not certify every agent/device combination, all races or restart recovery, and it does not retroactively turn prior unknown execution records into successful ones.

## Windows Codex independent SSH entry

The standalone `codex-ssh.cjs` starts a new desktop-context Codex through a one-shot InteractiveToken task, using a temporary authenticated loopback connection. It leaves ordinary Codex, ACPX defaults, sandbox settings and Windows ACLs unchanged. It requires a logged-in desktop and is not GUI/TUI takeover. Twenty-eight local Node tests passed, followed by real Windows Unicode/byte/exit forwarding and app-server EOF/disconnect/timeout checks. Lifecycle probes captured process identities and checked their absence, separately from Task removal.

A real PC Codex source used the native PowerShell client to coordinate MacBook OMP without Hermes dispatching or answering for it. The source consumed the real question, handled an early rejected reply by checking state, answered recorded questions and continued the same session. The CSV auditor's six tests and eight parent golden-case groups passed; the task closed. This business run used the pre-whitespace-guard client. No corresponding GUI or idle-wakeup claim is made.

Independent review found PowerShell could accept whitespace without throwing. Two new Windows fake-SSH cases reproduced false success before the fix; a non-whitespace guard made the identical test revision pass all six cases. Targeted independent review accepted the fix and evidence hashes. The fixed installed client passed real connect/status; the full model coding task was not repeated after this parser-only change. Node spawn SSH remains incompatible in the observed sandbox; the separate PowerShell client is an explicit alternative, not a claimed Node repair. Its remote decode currently targets macOS only.

Raw traces, model events, task data and local test receipts remain excluded under `evidence/`. See [CODEX_SSH.md](CODEX_SSH.md) for operating and evidence boundaries.

## Previously accepted v0.2.1 baseline

## Read-callback recovery and a real coding task

This patch distinguishes a correlated recoverable `fs/read_text_file` callback error from failure of the active `session/prompt`. A real task exposed a callback error followed by a normal public question and successful `end_turn`; the previous decoder incorrectly marked the whole turn unknown. The callback error alone does not establish that a file is missing. Unclassified errors, writes, consumed callback IDs, missing prompt correlation and prompt/callback ID collisions still fail closed. Successful settlement still requires the runner's existing exit, stop-reason and public-output checks.

The regression was observed RED before implementation, then all 53 local tests passed. Replay of the actual failed task trace correctly retained its normal question. A fresh native PC Pi origin used the deployed Node transport against the candidate core to coordinate remote OMP: plan and question, numbered reply, the same session's second turn, real test-first implementation and session closure. The origin, not the harness or Hermes, dispatched and answered.

The resulting standard-library CSV auditor includes a library API, CLI, six tests and README. The parent independently read back all three files, reran all six tests, and passed eight additional golden-case groups through the CLI (and successful cases through the library API): UTF-8 BOM and Unicode casefold, blank IDs, quoted values, header-only input, missing columns, malformed quoting, invalid UTF-8 and a missing input file. Inputs were synthetic test fixtures, not customer data. Detailed evidence remains local under `evidence/` and is not published with this summary.

After deployment, a second fresh native PC Pi session repeated the coding collaboration through the ordinary installed Bash entry, without a candidate entry override. It again completed numbered question/reply, test-first implementation and closure, and reported origin-side artifact checks. The parent independently reran the resulting six tests and all eight golden-case groups successfully. Deployment readback matched the reviewed runtime bytes on the coordination host and verified the payload hashes and native skill targets on all three devices; the two remote-only endpoints also passed installed-entry connect checks. These deployment-driver checks are not represented as native AI-origin tests on every device.

The independent read-only review accepted this decoder patch and ran all six `ProtocolTests` successfully. Its scope was the decoder diff, regression tests and runner settlement call sites; it did not independently rerun the full suite, live models or deployment. Ambiguous non-prompt cross-direction ID reuse beyond these fixtures remains unproven rather than claimed disambiguated.

This coding task is not a fresh certification of every intervention or device/CLI combination. The Windows Codex package and default command were repaired with the existing package manager; its headless restricted-shell execution separately failed with `0xC0000142`. No sandbox disabling, Windows ACL modification or new daemon was used to conceal that failure. The successful Pi-origin coding task does not count as repair of Codex shell execution.

## Previously accepted v0.2.0 baseline

## Accepted scope

A single POSIX coordination host, existing SSH, and new ACPX collaboration sessions. Originating OMP, Codex and Pi sessions consume progress, answer questions, request interventions and receive results without Hermes acting as a hidden intermediary.

The release passed 50 local automated tests. Three real cross-device paths each passed question/reply, progress-driven intervention and test-driver user-input intervention:

- OMP → remote Codex.
- Codex → remote Pi.
- Remote Pi → coordinator-host OMP.

Each intervention required a confirmed cancelled first turn, continuation in the same collaboration session, the revised result, independent artifact readback and session closure. An additional actual Hermes caller passed the generic question/reply/close path. The portable Node remote client passed real Windows→SSH→POSIX connect/list checks with payload hash readback.

## Evidence and limits

Detailed public-event records and local test outputs are deliberately excluded from Git by `evidence/`. The original development host retains their indexed checksums in `evidence/acceptance.json`. This document is the sanitized repository summary, not a replacement for those records.

An independent review accepted the cancellation evidence, initialization deadline, creation-attempt tracking and close-readback fixes at the 45-test checkpoint. Later focused changes were verified with test-first regressions and real runtime probes; that earlier review is not represented as a review of every later source change.

The first live-intervention fixture incorrectly accepted post-completion revision; that result was rejected, its predicate corrected, and the live cases repeated. Only the corrected cases count above.

User input was sent by a test driver through native RPC/turn-steer. This is not a physical terminal UI test or proof that the user personally typed the messages. The paths above do not certify every installed CLI/device combination.

Not covered: automatic wakeup after leaving the active collaboration, restart recovery, taking over manually opened sessions, distributed locks across separate coordination hosts, or native Telegram multi-topic delivery. The generic client does not authenticate a declared runtime as a native host session identity.

## Reproducing local tests

On a POSIX host with Python 3.9+ and Node:

```sh
mkdir -p "$HOME/.hermes/cache/scratch"
TMPDIR="$HOME/.hermes/cache/scratch" PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -v
node --check remote-client.cjs
```

Unit tests use fake transports and do not require model credentials. Live verification requires existing authorized ACPX/CLI authentication and isolated workspaces; do not replay uncertain business tasks just to repeat a smoke test.

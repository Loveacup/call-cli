# Verification

## Current local acceptance

The runtime candidate is installed on the local macOS host; the installed `__init__.py`, `bridge.py`, `collab.py`, `runner.py`, `shared-prompt.cjs`, and `models.cjs` match this source. Parent-run Python discovery: 85 cases, 82 passed and 3 opt-in skipped; Node: 41 passed. The known AGY error marker is covered by synthetic runner tests, not a deliberately induced live provider failure. A later installed generic-entry AGY Argon running-intervention retry passed; a preceding attempt failed, so this is not a stability claim. A fresh Hermes session's native plugin schema and actual Telegram progress/steering/delivery remain unverified. The sections below preserve historical source-stage checkpoints.

## AGY fatal error text is no longer reported as completed

- Source-only, scoped to user-visible settlement; no deployment, Git/GitHub writes, installed payload change or live model call. Root cause is deliberately not inferred.
- Observed wire shape (local ACPX journals of four real failed AGY turns): a single `agent_message_chunk` `"\n⚠ Agent execution terminated due to error.\n"` followed by `session/prompt` result `{stopReason: end_turn}`; ACPX recorded `completed / end_turn`.
- Rule: for `agy` only, if the final nonempty line of the turn's concatenated public output is exactly `⚠ Agent execution terminated due to error.`, the runner records `unknown` (exit code and `end_turn` retained as protocol facts), sets `settlement_note`, and emits a final `unknown` event stating not completed / outcome unknown / no automatic replay. Cancellation settlement is unchanged.
- RED: four new runner cases (single chunk, split across chunks, after partial output, after a question) settled `completed`/`waiting_reply` with exit 0. GREEN: all settle `unknown`, cannot be re-claimed, refuse reply/intervene, still allow owned close. Normal output, quoted/prefixed/suffixed text, the line followed by later content, and the same text from non-AGY agents remain `completed`.
- Boundary: this is a text marker, not protocol-level error status (AGY ACP does not return one). Other AGY failure wordings, or an error line followed by further output, are not detected.

## Scoped review fixes: late Hermes receipts and legacy creation evidence

- Source-only fixes; no deployment, Git writes, GitHub writes or real model calls. The existing shared Store lifecycle and agent selection behavior are retained.
- RED: the late-success regression replaced turn 2's process ID with turn 1's; late-error changed running turn 2 to `unknown`. GREEN: a durable launch claim prevents repeat dispatch, and receipt writes require both the original turn and launch identity. Same-launch uncertainty remains `unknown`; stale receipts do not invalidate cancellation of the active turn.
- RED: a legacy record without creation flags, cancelled before provider launch, resumed into `Cannot resolve existing native session`; evidence-free interventions were incorrectly accepted. GREEN: the exact first-turn runner cancellation event plus cancelled state permits initial creation once. Existing native record IDs are reused; absent evidence is rejected transactionally before intervention with a `new start` diagnostic. No inference from missing fields or cancelled state alone.
- Direct offline regression suite: **66 passed** (`test_plugin test_bridge test_runner test_collab test_review_regressions`). Full Python discovery: **79 cases, 76 passed, 3 opt-in integrations skipped**. Node facade/model/SSH: **41 passed**. `git diff --check` passed. Logs: `evidence/review-fixes-{targeted,python,node}.log` (ignored local evidence). New tests use isolated SQLite, synthetic host dispatch and fake executable transport; they do not certify live Hermes notification delivery or installed payloads.
- Hermes `status/events` can launch an already authorized pending intervention after settlement; they are not read-only. See `docs/integrations.md` for legacy refusal and continuation semantics.

## AGY runtime intervention and Hermes entry candidate

- Source only; no installed payload, gateway, authentication, network or global configuration was changed. No deployment or external issue/comment was performed. Hermes now exposes `intervene` with a stable required `command_id`. Generic and host paths share atomic Store intervention/advance/cancel logic, preserve exact ownership and native session identity, and never advance `unknown`. Native host `status/events` drive continuation after settlement; cancellation acceptance alone does not resume work. Pre-provider cancellation retains the need to create the first native session.
- Live discovery returned `claude-opus-5-5` and `gemini-4-argon` as exact ACP candidates (distinct from effort-suffixed display IDs). The real probe selected Opus on installed ACPX 0.22.0 using existing native login, with no effort override.
- Successful coordinator-driven probe: Before intervention, turn 1 was `running`, `AGY_ACTIVE_GATE` and tool `in_progress` events were persisted, and a foreground Python process was observed in its 5-second sleep after creating `old-started.txt`. Intervention set `cancelling`; turn 1 then settled `exit 0 / cancelled`. Only then did bounded `wait` launch turn 2 in the same record/session. The model read a random input, wrote and read back `REVISED:` plus that input, and completed `exit 0 / end_turn`. The coordinator independently checked exact bytes and absence of `old-result.txt`. Close read back `status_snapshot / no-session`; all three retained probe runner processes exited 0. Private public-event receipts and artifact readback are retained at `evidence/agy-runtime-intervention/receipt.json` (Git-ignored).
- Rejected first probe: AGY's terminal backgrounded a 120-second command after its 10-second tool limit, then the model completed. No intervention was submitted, and the session was closed. This is not running-intervention evidence. The accepted short foreground window proves the controlled case, not arbitrary tool termination or background-child cleanup.
- Subsequent `RUN_NATIVE_AGY_TESTS=1 CALLCLI_AGY_MODEL=claude-opus-5-5 python3 -B -m unittest test_native_agy -v` failed, not passed: the ordinary first turn returned `Agent execution terminated due to error.` while protocol settlement reported `completed`; the next explicit model selection failed before prompt submission. The running probe separately became `unknown` before its marker and could not confirm close. The original opt-in fixture removed its temporary directory even on failure; it now preserves failed workspaces/task DBs, checks the first sentinel before continuation, retains worker handles, and bounds retries only for SQLite contention/readiness. These revised live fixtures were not rerun against the failing provider. The known failed ordinary task's native record is closed; the running probe's creation result/identity was not captured. PID 39272 was subsequently absent. A native session-list control timed out after 30 seconds; no blind replay or broad process kill followed.
- Repository regression checkpoint: Python discovery **74 cases, 71 passed, 3 opt-in cases skipped**; Node facade/model/SSH **41 passed**, exit 0. Earlier shared Store regressions were observed RED for the missing lifecycle seam, then passed. Host tests use synthetic context/terminal dispatch and do not establish real host consumption.
- **Hermes current-chat / Telegram not verified.** This process has no native `HERMES_SESSION_ID/KEY/PLATFORM/CHAT_ID/THREAD_ID`; `capture_origin` returns `No host session identity; use a live Hermes session`. No fake source identity or direct Telegram injection was used. A real enabled source-matching plugin in the original Hermes session/topic must consume progress, accept user input, invoke `intervene`, resume via `status/events`, read the revised result, and deliver completion/close events. That requires the user's later deployment/host acceptance; generic receipts and mocked host routing are not substitutes.

## AGY/model forwarding candidate — ACPX 0.22.0

- Source supports native `agy acp` in generic collaboration and the Hermes schema, explicit `model`, and optional separately advertised `effort`. Task JSON preserves selection through continuation without migrating or adding fields to old tasks. Model IDs are discovered via public `inspectAgentModels`, not a baked-in catalog. Normal task dispatch checks versions but does not upgrade packages.
- Default Python discovery: **67 cases, 65 passed, 2 opt-in integrations skipped**, exit 0. Node facade/model-discovery/Windows-SSH tests: **41 passed**, exit 0. New behavior had failing tests before implementation; Hermes schema/reply and busy-maintenance tests are post-implementation regression coverage.
- `RUN_NATIVE_AGY_TESTS=1 CALLCLI_AGY_MODEL=gemini-3.8-flash CALLCLI_AGY_EFFORT=low python3 -B -m unittest test_native_agy -v`: **passed on installed ACPX 0.22.0 and AGY 1.5.3**. The model was selected from live ACP discovery, not inferred by stripping an effort suffix. Both real prompts returned the requested sentinel, both turns emitted model-selection readback, the native record ID stayed unchanged, only one latest-version check was recorded, and final session metadata confirmed model plus `thought_level=low`. Close/status readback succeeded. This is coordinator-driven read-only smoke, not native Codex initiation, GUI use or remote-device certification.
- `RUN_NATIVE_ACPX_TESTS=1 python3 -B -m unittest test_native_shared -v`: **passed on 0.22.0**; four synthetic protocol scenarios (callback-ID collision recovery, synthetic auth, genuine prompt failure, cancellation) retained expected outcomes and one prompt per scenario. All owned sessions closed. This is real ACPX transport with a synthetic endpoint, not model evidence.
- An initial live spike failed before prompt because Shared Runtime control methods require a running owner. The failed session was closed. The retained implementation instead uses native CLI `set` plus exact-session `show` readback before submitting through Shared Runtime. Offline mocks alone did not reveal this boundary.
- `gh issue view 1 --repo Loveacup/call-cli --json title,body,comments,state` confirmed the empty-notification report remains open with no comments at inspection. No call-cli-local notification defect was reproduced; no notification workaround or Hermes core changes were made. **Telegram is not claimed fixed**, and no external issue/comment was written by this implementation task.

On the local macOS installation, the deployed runtime files match the candidate source. Parent-run installed `collab`/`runner` smoke passed two turns, model/effort readback and close for `gemini-3.8-flash` / low, `claude-opus-5-5` / medium, and `gemini-4-argon` / medium. This does not verify the fresh Hermes native plugin schema, remote devices, file tools, or Telegram. A prior AGY error text was emitted with `completed` despite failure: protocol completion is not evidence of business success; that observed behavior remains unresolved. See issues #1 and #2. Historical 0.20.0 results below are not retests of this candidate.

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

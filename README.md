<div align="center">

<img src="docs/assets/call-cli-banner.png" alt="Call CLI — different CLIs, one conversation" width="100%">

**Give your CLI a teammate, not just another subprocess.**

<a href="README.md"><strong>English</strong></a> · <a href="README.zh-CN.md">简体中文</a>

<a href="VERIFICATION.md"><img src="https://img.shields.io/badge/version-0.2.3-8b7cf8?style=flat-square&amp;labelColor=161b22" alt="Version 0.2.3"></a>
<a href="https://github.com/openclaw/acpx"><img src="https://img.shields.io/badge/transport-ACPX-49c5b6?style=flat-square&amp;labelColor=161b22" alt="Powered by ACPX"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8cbaff?style=flat-square&amp;labelColor=161b22" alt="MIT license"></a>

[Quick start](#quick-start) · [Core capabilities](#capabilities) · [Choose your entry](#choose-your-entry) · [How it works](#how-it-works) · [Documentation](#documentation)

</div>

---

Call CLI connects **OMP, Codex, Pi and AGY** through a lightweight coordination layer. Start a fresh collaboration session, follow its live progress, answer numbered questions, pass task-pinned skills and steer direction while it works — **locally or over your existing SSH connections**.

**No mandatory Hermes dependency. No additional always-on service. No takeover of your manually opened terminal or GUI sessions.**

## Why Call CLI?

You plan in one CLI. Another agent implements on the same machine or over SSH. A third reviews the result. Questions, skill bundles and mid-run course corrections travel through the initiating CLI instead of being copied between terminals by hand.

| | Capability | What you get |
| :-- | :-- | :-- |
| 💬 | **Multi-turn Q&A and live steering** | Answer numbered questions with `reply` or redirect a running turn with `intervene` in the same target session. |
| 📡 | **Resumable event stream** | Cursor-based `wait` and `events` (`--after SEQ`) return public stages, deduplicated safe tool statuses, pending `question_ids` and terminal `answer_tail`. |
| 🤝 | **Multi-CLI concurrency** | Run AGY, OMP, Codex and Pi in parallel under isolated task/client/session identities with single-writer workspace reservation (`--write`, `--resource`). |
| 🤖 | **First-class AGY & default tool approval** | Discover exact advertised ACP models (`models.cjs`), run native `agy acp` with persisted default `approve-all` tool policy, and settle unrecovered tool failures honestly as `unknown`. |
| 📦 | **Task-pinned skill passing** | Hermes or any initiating CLI can pass `required`, `optional` or `reference` skills from `origin` or `target`, pinned by SHA-256 across turns with `CALLCLI_SKILL` receipts. |
| 🔌 | **Modular backend adapters** | Shared task/turn coordination (`bridge.py`, `runner.py`) is decoupled from ACPX and AGY adapters via `backends.py`; future backends do not require ACP. |

<a id="quick-start"></a>
## Quick start

### Ask your AI to set it up

Paste this into the OMP, Codex, Pi or Hermes session you already use:

```text
Read the README and docs/configuration.md in
https://github.com/Loveacup/call-cli, then set up Call CLI for this CLI.

Discover the existing installation, SSH aliases, logins and workspaces first.
Install only the entry, skill or module I need. Preserve existing files and
credentials; do not create an always-on service. Explain the first-turn npm
version check before starting a task; upgrades need separate authorization.

Verify with a real read-only collaboration, close your test sessions,
and report what works and what remains blocked.
```

The setup guide is **an execution manual for your AI**, not a configuration file or questionnaire for you to fill out by hand. Detailed AI guides are maintained in **Simplified Chinese**; both README editions cover the exact same commands and boundaries.

<details>
<summary><strong>Prefer the terminal? Run directly from source.</strong></summary>

#### Prerequisites

- **Coordinator:** POSIX Python 3.9+ with `fcntl`. End-to-end coordination is verified on macOS; Windows is supported as a client or execution device, not as the Python coordinator host.
- **Execution device:** Node.js, npm, ACPX and a configured, authenticated target CLI (`omp`, `codex`, `pi` or `agy`).
- **Compatibility:** requires the public ACPX Shared Runtime (`acpx/runtime`). Local live AGY/OMP/skill sessions are verified on **0.22.0** (cross-device Windows SSH OMP on **0.21.1**; historical baseline **0.20.0**), without a hardcoded exact-version pin. See the [AI setup guide](docs/configuration.md) for adapter configuration and platform boundaries.

```sh
git clone https://github.com/Loveacup/call-cli.git
cd call-cli
python3 collab.py --help
python3 collab.py start --help
acpx --version
```

Call CLI is source-distributed; there is no separate Call CLI npm/pip package or one-line global installer.

#### Start a read-only collaboration

Run this in the same POSIX shell from the repository checkout. No write permission is granted:

```sh
CALL_CLI_HOME="$PWD"
CALL_CLI_DB="$HOME/.local/state/call-cli/tasks.sqlite3"

CLIENT_ID="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" \
  connect --runtime omp | python3 -c 'import json,sys; print(json.load(sys.stdin)["client_id"])')"

TASK_ID="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" \
  --client "$CLIENT_ID" start --device local --agent omp --cwd "$PWD" \
  --timeout 120 --prompt 'Read this project and summarize its main entry points. Do not modify files.' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
  wait --task "$TASK_ID" --after 0 --timeout 5
```

Keep the returned `client_id`, `id` (`TASK_ID`) and `after` cursor:

- **Follow progress / Q&A:** repeat short `wait` calls advancing `--after`. If the target asks a question (`waiting_reply`), answer the exact question ID with `reply --task "$TASK_ID" --question-id QUESTION_ID --prompt '...'`.
- **Steer a running task:** submit `intervene --task "$TASK_ID" --command-id STABLE_ID --prompt '...'`, then continue short `wait` calls so the coordinator cancels the old turn, confirms settlement and launches the revised turn in the same session.
- **Target AGY or attach a skill:** discover models first, then set `MODEL_ID` to an exact ID from that output. Replace the skill path below with a directory you have inspected and intend to share.

```sh
# Discover exact advertised ACP model IDs on the execution device first
node "$CALL_CLI_HOME/models.cjs" "$PWD"

# Start an AGY task with a pinned coordinator-side skill bundle
python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
  start --device local --agent agy --model "$MODEL_ID" --cwd "$PWD" \
  --skill required=/absolute/path/to/skill-dir \
  --prompt 'Follow the passed skill and inspect the repository read-only.'
```

- **Close when done:** after the turn settles (`completed` or `cancelled`), verify the result and run `close --task "$TASK_ID"`. Closing a running task is not a substitute for cancellation.

A client ID is a local capability handle; `--runtime omp` is a caller declaration, not authenticated native provenance. See the [online collaboration guide](docs/usage.md) for the full lifecycle.

</details>

> **Version checks are not upgrades:** each logical new task queries `installed`/`latest` once and persists the result. Normal dispatch never installs packages; separately authorized maintenance must wait until the device is idle. [Details](docs/configuration.md#acpx-版本检查与更新)

<a id="capabilities"></a>
## Core capabilities

### 1. AGY support & default tool approval

- **Live model discovery:** run `node models.cjs /absolute/workdir` on the execution device to inspect advertised ACP models via `inspectAgentModels`, then pass `--agent agy --model ID` (Hermes: `agent: "agy", model: "ID"`). Optional `--effort VALUE` uses the advertised `thought_level` option—never guess or strip model suffixes. Explicit user choice wins; otherwise the initiating CLI selects a candidate, persists it and verifies readback before each turn without silent provider fallback.
- **Default `approve-all` policy vs. write reservation:** new AGY tasks persist `agy_approval: "approve-all"` by default (even when `write` is `false`), allowing authorized shell and native tools within task scope while prompt guidance still prefers native read/search tools. For strict read-only approvals, pass `--agy-approval approve-reads` (Hermes: `agy_approval: "approve-reads"`) so shell/write permission prompts are denied non-interactively. Approvals are permission policies, **not** a filesystem sandbox; pass `--write` separately for authorized edits and workspace reservation. Replies and interventions preserve the task's policy; legacy tasks without the field keep their original `write` mapping, and OMP/Codex/Pi defaults (`approve-reads` unless `--write`) are unchanged.
- **Honest outcome settlement:** if an AGY tool fails and the turn ends with neither a final answer nor a pending question—or if the final non-empty output line matches the exact fatal marker `⚠ Agent execution terminated due to error.`—the turn settles as `unknown` (no automatic replay) even when the protocol reports `end_turn` / exit `0`. Pending questions remain answerable (`waiting_reply`) unless overridden by the fatal line; a recovered tool failure followed by a final answer can complete. Only safe tool identifiers, categories and statuses are retained; public Shared Runtime events do not expose permission decisions, so diagnostics report `unobservable` rather than guessing a denial cause. See the [AGY model & approval workflow](skill/references/agy-models.md).

### 2. Task skill passing (Hermes or any initiating CLI)

- **Initiator-agnostic contract:** Hermes, OMP, Codex, Pi or terminal scripts can select and pass task skills through the same `start` interface: repeatable `--skill MODE=PATH` (`location: "origin"`), `--target-skill MODE=PATH` (`location: "target"`, including Windows absolute paths), or `--skills-json ARRAY` (`skills` in Hermes / Python `Collaboration.start`).
- **Local-first selection & public discovery signals:** the initiating CLI checks local skills first and reads `SKILL.md`, relative references/scripts and target dependencies before dispatching. When searching public repositories (e.g. `hermes skills search` or GitHub search), repository stars and recent substantive updates (distinguishing real workflow/code updates from license-only or formatting commits) serve as discovery signals rather than the sole measure of quality. Selected public skills are pinned to an exact commit SHA in scratch (`source` metadata) without running global installs.
- **Modes (`required` / `optional` / `reference`):**
  - `required`: target must read `SKILL.md` and needed resources and follow applicable instructions, or report `not_applicable` / `unavailable` with a specific reason. If staging a `required` skill fails, no business prompt is submitted.
  - `optional`: target reads and adopts the skill when useful, or reports `not_adopted` with its reason.
  - `reference`: target reads the bundle as non-binding reference material.
- **Pinned `origin` vs. `target` staging & receipts:** `origin` skills are snapshotted on the coordinator at task creation with a per-file SHA-256 manifest (skipping dotfiles, `node_modules`, `__pycache__`, symlinks and common credential files; capped at 4 MiB/file, 16 MiB/bundle, 1000 files) and staged over the existing local/SSH channel into the execution device's temporary directory (`call-cli-skills/<task>/<skill>/<sha>/`) before each turn. `target` skills pin and verify an existing directory on the execution device. Continuations (`reply`, `intervene`) reuse the pinned bundle; `close` removes the coordinator snapshot and cleans up the target temporary directory. Each turn records `CALLCLI_SKILL <id> <read|adopted|not_adopted|not_applicable|unavailable> <reason>` self-reports (`reported` vs. `unverified` when a `required` report is missing, without overwriting valid business completion).
- **Read boundary:** ACPX 0.22.0's `fs/read_text_file` callback only permits paths inside the `cwd` subtree, whereas agent-native file tools (verified with OMP on macOS/Windows and AGY on macOS) read staged temporary bundles directly without modifying `cwd`. Callback-only targets report `unavailable` rather than expanding permissions or writing into user workspaces. See the [skill passing guide](skill/references/skill-passing.md).

### 3. Multi-CLI concurrency, resumable events & modular backends

- **Concurrent multi-CLI tasks:** run multiple CLIs (such as AGY and OMP) simultaneously on the same host or across SSH under separate client/task/session identities. Single-writer reservations (`--write` and optional shared `--resource`) block conflicting writers while allowing read-only tasks (including AGY `approve-all` with `write: false`) to run alongside an active writer.
- **Resumable events & atomic settlement:** runner notifications and `events`/`wait` share durable `seq` cursors. Repeated status updates for the same tool are deduplicated (ordinary tool notices are bounded per turn while all tool failures and full `events` queries remain intact). Turn completion commits task state and a single `<turn>:settled` event (`question_ids`, `answer_tail`, and pending-intervention next action) in one SQLite transaction so cursor reads recover settlement even if stdout observation was interrupted.
- **Pluggable backend seam:** `bridge.py` and `runner.py` manage task ownership, state transitions, turns, interventions and writer reservations independently of ACPX. `backends.py` registers adapters implementing `prepare_start(model, effort, approval)`, `run(store, task, emit)` and `control(task, action, timeout)`. `acpx_backend.py` and `agy.py` implement ACPX Shared Runtime and AGY policies today; non-ACP backends can register in the same table without adding daemons or separate stores (verified by the fake non-ACP lifecycle contract in `test_backends.py`). See the [backend extension interface](docs/integrations.md#后端扩展接口).

<a id="choose-your-entry"></a>
## Choose your entry

| You work in… | Use | Scope |
| :-- | :-- | :-- |
| A terminal or script | `python3 collab.py` | Complete generic coordination CLI (`connect`, `start`, `list`, `status`, `events`, `wait`, `reply`, `intervene`, `cancel`, `close`). |
| OMP, Codex or Pi | The `skill/` bundle + a configured entry | AI-operated collaboration and skill passing; skills do not install dependencies or authenticate agents. |
| Another machine | `remote-client.cjs` | One-shot SSH transport to the coordinator host. |
| A restricted Windows Codex environment | `remote-client.ps1` | Native PowerShell/.NET alternative to the observed Node SSH spawn `EPERM`; verified with a macOS coordinator only. |
| Hermes | Optional `call_cli` plugin | Native origin binding, background notifications, skill passing and cancel/settle/same-session `intervene`. New tool schemas require a fresh Hermes session; live current-chat consumption and Telegram delivery (Issue #1) require separate host verification. |

**Terminal installation and in-CLI loading belong to the same setup path.** The [integration guide](docs/integrations.md) explains which runtime files must be installed together, how the AI loads the skill or plugin, and how to verify the active entry without stacking conflicting copies.

<a id="how-it-works"></a>
## How it works

```text
Initiating CLI                   Call CLI                       Target CLI
   │                                │                               │
   ├──── start (+ optional skills) ─►│──── local / SSH + ACPX ──────►│
   │◄─── stages / tools / questions ─┤◄─── public events (seq) ──────┤
   ├──── reply / intervene ─────────►│──── same-session + pinned ───►│
   │◄─── result + skill receipts ────┤◄─── authoritative turn.result ┤
```

The coordinator owns a small SQLite task store and bounded per-turn runners. ACPX supplies native session lifecycle and queue-owner serialization, while its public `acpx/runtime` API provides the authoritative `turn.result`. Public events drive progress, tool visibility, skill receipts and numbered Q&A—never guesses about final settlement.

Runtime intervention follows an explicit contract: **request `intervene` → cancel the running turn → confirm cancellation settlement → continue in the same native session → verify the revised result**. It is not a claim of universal lossless hot-editing or arbitrary child-process pause/resume.

<a id="documentation"></a>
## Documentation

### For the AI operating Call CLI

| Guide | What it covers | Language |
| :-- | :-- | :-- |
| [Installation & configuration](docs/configuration.md) | Environment discovery, SSH, adapters, authentication, permissions and acceptance checks. | 简体中文 |
| [CLI & module integration](docs/integrations.md) | Skill installation, optional Hermes plugin, runtime file list and backend adapter interface. | 简体中文 |
| [Online collaboration](docs/usage.md) | Progress cursors, Q&A, runtime intervention, cancellation, verification and closure. | 简体中文 |
| [Task skill passing](skill/references/skill-passing.md) | Local-first selection, public repo signals, `required`/`optional`/`reference` modes, staging and `CALLCLI_SKILL` receipts. | 简体中文 |
| [AGY models & approvals](skill/references/agy-models.md) | `models.cjs` discovery, explicit `model`/`effort`, persistent `agy_approval` and outcome diagnostics. | 简体中文 |
| [Windows Codex SSH](CODEX_SSH.md) | Desktop-context execution and native PowerShell client templates. | 简体中文 |

### For users and contributors

[Verification](VERIFICATION.md) · [Roadmap](ROADMAP.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

<details>
<summary><strong>Scope and safety — read before granting access</strong></summary>

- **Fresh collaboration sessions only.** Call CLI never takes over manually opened terminals, TUIs or desktop GUIs.
- `queued` / `pending` means submitted, not applied. Reconcile `unknown` / `DO_NOT_REPLAY` against the existing session and workspace before retrying.
- OMP/Codex/Pi use `approve-reads` by default and select `approve-all` with `--write`. New AGY tasks persist `agy_approval: "approve-all"` by default (or explicit `approve-reads`). Neither approval policy nor `write: false` is an OS/filesystem sandbox; SSH and the target CLI run with the configured OS account's permissions.
- Skill staging filters dotfiles, symlinks, `node_modules`, `__pycache__` and common key filenames, and enforces size/file-count caps, but it is **not** a malware sandbox or automated privacy audit. Only pass directories and resources intended for the target model.
- Keep task databases, client handles, prompts, answers and authentication private. “Public event” means non-internal coordinator output, not redacted user data.
- One POSIX coordinator owns state and locks. Distributed locking across multiple coordinators, crash/restart notification replay and automatic wakeup after leaving a collaboration are not promised.
- Source tests, locally installed runtime checks, cross-device probes and live host/Telegram delivery prove different claims. See [VERIFICATION.md](VERIFICATION.md).

</details>

<a id="development"></a>
## Development & verification

The Python coordinator uses the standard library. New capabilities—including AGY model/effort forwarding and persistent default `approve-all`, modular backend adapters, resumable settlement events, concurrent AGY + OMP runs, and task skill passing—have been installed locally and exercised in real local collaborations, alongside source-level cross-device Windows SSH OMP skill-passing checks and offline/synthetic test suites. Fresh in-memory Hermes sessions require reload to expose updated tool schemas, and live Telegram empty-notification delivery (Issue #1) and upstream AGY error reporting (Issue #2) remain open; see [VERIFICATION.md](VERIFICATION.md) for exact evidence boundaries.

```sh
python3 -B -m unittest discover -v
node --test test_skill_stage.cjs test_shared_prompt.cjs test_models.cjs test_codex_ssh.cjs
# Optional: owned native ACPX sessions with synthetic endpoint (no live model call)
RUN_NATIVE_ACPX_TESTS=1 python3 -B -m unittest test_native_skills test_native_shared -v
```

Live model and cross-device verification require explicit execution authorization and existing target CLI logins; private run journals are excluded from Git. The optional banner generator in `docs/assets/` uses Pillow and is **not** a runtime dependency.

## Acknowledgements

Built on [ACPX](https://github.com/openclaw/acpx) and the native capabilities of the target CLIs. Call CLI is not affiliated with those projects.

<div align="center">

**Different tools. Shared direction.**

[MIT License](LICENSE) · [简体中文](README.zh-CN.md)

</div>

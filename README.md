<div align="center">

<img src="docs/assets/call-cli-banner.png" alt="Call CLI — Cross-CLI & cross-device collaboration across OMP, Codex, Pi and AGY (with optional Hermes integration)" width="100%">

**Give your CLI a teammate, not just another subprocess.**

<a href="README.md"><strong>English</strong></a> · <a href="README.zh-CN.md">简体中文</a>

<a href="https://github.com/openclaw/acpx"><img src="https://img.shields.io/badge/transport-ACPX%200.22%2B-49c5b6?style=flat-square&amp;labelColor=161b22" alt="Powered by ACPX Shared Runtime"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-8cbaff?style=flat-square&amp;labelColor=161b22" alt="MIT license"></a>

[Quick start](#quick-start) · [Choose your entry](#choose-your-entry) · [Core capabilities](#capabilities) · [Architecture & lifecycle](#how-it-works) · [Evidence & boundaries](#evidence-and-boundaries) · [Documentation](#documentation)

</div>

---

Call CLI connects **OMP, Codex, Pi and AGY** through a lightweight POSIX coordination layer. Start a fresh collaboration session, follow live stage progress and return flow in your initiating session, answer numbered questions, pass task-pinned skills, and redirect work in flight via explicit **cancel-and-resume (`intervene`)** — **locally or over your existing SSH connections**.

**What it is not:** No mandatory Hermes dependency. No additional always-on service or daemon. No in-turn hot `steer` disguised as lossless editing. No takeover of your manually opened terminal, TUI or desktop GUI sessions.

## Why Call CLI?

You plan in one CLI. Another agent implements on the same machine or over SSH. A third reviews the result in parallel. Stage progress, numbered questions, pinned skill bundles, and mid-run course corrections flow back and forth through the initiating CLI instead of being copied between terminals by hand.

| | Capability | What you get |
| :-- | :-- | :-- |
| 🔄 | **Continuous collaboration & return flow** | Follow resumable stage progress, questions, and final answers from your initiating CLI, with periodic 600s review checkpoints instead of rigid execution timeouts. |
| 🧭 | **Single-shot control & `cancel+resume`** | Answer numbered questions with `reply`, or redirect work in flight with `intervene` (cancel active turn → confirm settlement → resume in the same target session, never in-turn `steer`). |
| 🤖 | **First-class AGY & model-aware Swarm** | Default AGY **G4C** tasks run native Swarm with **G4H** workers, while **G4A** defaults to single-agent `off` for writing and knowledge work—both overridable per task, with configurable tool approval. |
| 🤝 | **Parallel multi-CLI concurrency** | Run OMP, Codex, Pi and AGY side by side in isolated sessions, protected against conflicting edits by single-writer workspace locks. |
| 📦 | **Task-pinned skill passing** | Attach local or target-side skills (`required`, `optional`, or `reference`) pinned to an immutable snapshot across turns, with per-turn adoption receipts. |
| 🔌 | **Flexible entries & optional host bridges** | Coordinate from any POSIX shell, SSH client, or CLI skill, with optional single-instance OMP RPC polling, an optional Hermes plugin, and a separate, unapplied Hermes silence compatibility patch. |

<a id="quick-start"></a>
## Quick start

### Option 1: Ask your AI to set it up (Recommended)

Paste this prompt into the OMP, Codex, Pi or Hermes session you already use:

```text
Read README.md and docs/configuration.md in https://github.com/Loveacup/call-cli,
then inspect collab.py and docs/integrations.md to set up Call CLI for this CLI.

1. Discover first (read-only): check python3, node, npm, acpx, existing Call CLI
   checkouts, SSH aliases, skill directories and target CLI logins before asking
   for missing values. Preserve existing files, configurations and credentials.
2. Prepare only what I need: install only the entry, skill or optional Hermes
   module required for my workflow. Do not create an always-on service or daemon,
   and do not auto-apply upstream compatibility patches. Explain the once-per-task
   npm version check before starting work; package upgrades require separate
   authorization on an idle device.
3. Verify safely and distinguish boundaries: run a local no-model check
   (connect + list) first. Obtain approval before launching a real read-only
   collaboration or external effects, close owned test sessions (confirming
   released/no-session), and report source checks, host compatibility and real
   end-to-end results separately.
```

The linked [AI setup guide (`docs/configuration.md`, 简体中文)](docs/configuration.md) is **an execution manual written for your AI agent**, not a questionnaire for you to fill out by hand. Detailed AI reference manuals are maintained in **Simplified Chinese**; both README editions cover the exact same commands, parameters and boundaries.

<details open>
<summary><strong>Option 2: Run directly from a POSIX terminal (continuous path)</strong></summary>

#### Prerequisites

- **Coordinator host:** POSIX Python 3.9+ with `fcntl` (standard library only; no `pip install`). End-to-end coordination is verified on macOS; Windows is supported as an initiating client or execution device, not as the Python coordinator host.
- **Execution device (for Stage B live runs):** Node.js (>=22.13.0), npm, ACPX exposing the public Shared Runtime (`acpx/runtime`), and an installed, natively logged-in target CLI (`omp`, `codex`, `pi` or `agy`).
- **ACPX & AGY compatibility:** Local live AGY (`1.5.3` / `1.5.7`), OMP and skill-passing sessions are verified on **ACPX 0.22.0** (cross-device Windows SSH OMP on **0.21.1**; Shared Runtime baseline **0.20.0**) without a hardcoded version lock.

#### Stage A: Local coordinator smoke (no target model or credentials required)

Clone into a new directory (or `cd` into an existing checkout) and verify the coordinator store in a single POSIX shell (does not call any target model; performs only local temporary file writes):

```sh
# If you do not already have a checkout, clone into a new directory:
git clone https://github.com/Loveacup/call-cli.git
cd call-cli

# 1. Inspect CLI entry points (read-only)
python3 collab.py --help
python3 collab.py start --help

# 2. Create an isolated SQLite task store and capability handle (local temporary write respecting TMPDIR)
CALL_CLI_HOME="$PWD"
CALL_CLI_DB="$(mktemp "${TMPDIR:-/tmp}/call-cli-quickstart.XXXXXX")"

CLIENT_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" connect --runtime omp)"
printf '%s\n' "$CLIENT_JSON"

CLIENT_ID="$(printf '%s' "$CLIENT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["client_id"])')"

# 3. Verify task listing with the issued handle
python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" list
```

**Observable success criteria (Stage A):**
- `connect` exits `0` and prints JSON containing a UUID `client_id` and `"declared_runtime": "omp"`.
- `list` exits `0` and prints `[]`, confirming local SQLite store initialization (`0600`) and capability-handle validation without calling any target model.

#### Stage B: Real read-only collaboration (requires ACPX & logged-in target CLI)

Continue in the **same shell** (reusing `CALL_CLI_HOME`, `CALL_CLI_DB` and `CLIENT_ID`) to launch a fresh read-only session. Normal collaboration has no execution deadline: the coordinator reviews progress (with at most one review notice every 600s) before choosing to wait, intervene or cancel. Legacy CLI `--timeout SECONDS` remains an uncommon explicit hard-budget option (unavailable on `--agy-swarm native`); bounded `wait` is only a short poll slice.

```sh
# Check ACPX on the execution device
acpx --version

# Start a read-only OMP task in the current repository (no --write granted)
TASK_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" \
  --client "$CLIENT_ID" start --device local --agent omp --cwd "$CALL_CLI_HOME" \
  --prompt 'Read collab.py and summarize the CLI subcommands in 3 bullet points. Do not modify files.')"
printf '%s\n' "$TASK_JSON"

TASK_ID="$(printf '%s' "$TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

# Bounded wait (up to 5s per call): save WAIT_JSON and extract the updated after cursor into AFTER
WAIT_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
  wait --task "$TASK_ID" --after 0 --timeout 5)"
printf '%s\n' "$WAIT_JSON"

AFTER="$(printf '%s' "$WAIT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["after"])')"
```

**Observable success criteria & lifecycle follow-through (Stage B):**
- **Start:** `start` returns JSON with a 16-hex `id` (`TASK_ID`), `"session": "call-<id>"`, `"state": "queued"` (or `"running"`), `"turn": 1`, and a credential-free `recovery` card.
- **Progress & settlement:** On each subsequent poll, pass `--after "$AFTER"` and update `WAIT_JSON` and `AFTER` from the returned `"after"` integer cursor (`WAIT_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" wait --task "$TASK_ID" --after "$AFTER" --timeout 5)"` and `AFTER="$(printf '%s' "$WAIT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["after"])')"`). When the turn finishes, `task.state` becomes `"completed"` (or `"waiting_reply"` if the agent emitted `CALLCLI_QUESTION`), `task.exit_code` is `0`, `task.stop_reason` is `"end_turn"`, and `events` includes `"token": "1:settled"` with `answer_tail`.
- **Answer questions or redirect work (`reply` / `intervene`):**
  - If `task.state` in `WAIT_JSON` is `"waiting_reply"`, extract `QUESTION_ID` from `task.question_id` and answer that exact token:
    ```sh
    QUESTION_ID="$(printf '%s' "$WAIT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["task"]["question_id"])')"
    python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
      reply --task "$TASK_ID" --question-id "$QUESTION_ID" --prompt 'Your answer'
    ```
  - To redirect a running or settled turn in the same session (`cancel+resume`, not `steer`), pass a retry-safe `--command-id` (and optional `--expected-turn` or `--clear-timeout`):
    ```sh
    python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
      intervene --task "$TASK_ID" --command-id rev-1 --prompt 'Revised scope'
    ```
    then keep calling `wait` (updating `WAIT_JSON` and `AFTER`) so the coordinator cancels the running turn, confirms authoritative settlement and launches turn 2 in the same session.
- **Target AGY (model-aware Swarm defaults & read-only approval) or pass a task skill:** Omitting `--model` on AGY uses **G4C**, which defaults to **native Swarm** (`--agy-swarm native`) with live-resolved **G4H** workers (`--agy-worker-model` override). **G4A** defaults to single-agent **`off`** (`--agy-swarm off`) for writing/knowledge tasks and can be overridden with `--agy-swarm native`; G4C also supports explicit `--agy-swarm off` when paired with an advertised model ID discovered via `models.cjs`. New AGY tasks default to `approve-all` even when `write` is `false`; pass `--agy-approval approve-reads` for strict read-only tool approval (neither approval mode nor `write: false` is an OS/filesystem sandbox; `--write` separately declares edit authorization and reserves the workspace):
  ```sh
  # 1. Discover exact ACP model IDs and thought_level.options advertised on this execution device
  node "$CALL_CLI_HOME/models.cjs" "$CALL_CLI_HOME"

  # 2. Set MODEL_ID to an exact availableModelIds value from this discovery run,
  #    start an independent read-only AGY task, wait until settled, and close it
  MODEL_ID="gemini-4-argon"  # example; choose an exact ID returned on your execution device
  AGY_TASK_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
    start --device local --agent agy --agy-swarm off --agy-approval approve-reads --model "$MODEL_ID" \
    --cwd "$CALL_CLI_HOME" --skill reference="$CALL_CLI_HOME/skill" \
    --prompt 'Read the passed skill as reference and list the supported actions. Do not modify files.')"
  printf '%s\n' "$AGY_TASK_JSON"

  AGY_TASK_ID="$(printf '%s' "$AGY_TASK_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"

  # Bounded wait: save AGY_WAIT_JSON and extract AGY_AFTER (repeat passing --after "$AGY_AFTER" until AGY_TASK_ID settles)
  AGY_WAIT_JSON="$(python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
    wait --task "$AGY_TASK_ID" --after 0 --timeout 5)"
  printf '%s\n' "$AGY_WAIT_JSON"

  AGY_AFTER="$(printf '%s' "$AGY_WAIT_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["after"])')"

  # Close AGY_TASK_ID after its task.state settles (completed or cancelled)
  python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" \
    close --task "$AGY_TASK_ID"
  ```
- **Close and verify cleanup:** Once the OMP task (`$TASK_ID`) is settled (`completed` or `cancelled`), close its session:
  ```sh
  python3 "$CALL_CLI_HOME/collab.py" --db "$CALL_CLI_DB" --client "$CLIENT_ID" close --task "$TASK_ID"
  ```
  `close` commits a durable SQLite release fence (`release_fences`) and returns `"state": "closed"` with `close_receipt.state == "released"` only after verifying exact local closed-record readback and scoped `no-session` (or single-owner native worker/session cleanup for `--agy-swarm native`) and removing task-pinned skill snapshots. On `unknown` tasks, `close` runs in read-only probe mode without re-issuing an uncertain close. Do not delete a shared `$CALL_CLI_DB` merely because one task closed—confirm via `list` that all tasks in that database (such as `$TASK_ID` and `$AGY_TASK_ID`) are `closed` so no live session is orphaned.

</details>

> [!NOTE]
> **Version checks are not automatic upgrades:** Each logical new task queries `acpx --version` and `npm view acpx dist-tags.latest` once and persists `version_check`. Normal task dispatch never installs packages; separately authorized maintenance upgrades (`updates.py`) require an idle device lock and verified official repository metadata.

<a id="choose-your-entry"></a>
## Choose your entry

| You work in… | Entry point | Single-shot control, return flow & verification boundary |
| :-- | :-- | :-- |
| **A POSIX terminal or script** | `python3 collab.py` | Complete generic coordinator CLI (`connect`, `start`, `list`, `status`, `events`, `wait`, `reply`, `intervene`, `cancel`, `close`). Every call is a single-shot action; caller polls bounded `wait` / `events --after SEQ`. `connect --runtime {omp,codex,pi,hermes}` records a caller declaration (`client_id` capability handle), not authenticated native host identity. |
| **OMP, Codex or Pi** | `skill/` bundle + configured entry | Guides the initiating AI through environment discovery, continuous online coordination, `seq` polling, Q&A, `cancel+resume` intervention and skill passing. Skills do not install dependencies or replace native target CLI logins. |
| **A new owned OMP RPC host (experimental)** | `omp --mode rpc --no-ui --no-extensions -e /absolute/call-cli/omp-extension.mjs` | Single-shot extension load in a new owned RPC instance only (never globally enabled or hot-attached). Native `ctx` supplies identity to `omp_bridge.py`; managed 1s non-blocking `wait` polling (`busy_timeout=0`, including across pending intervention cancellation and AGY `unknown` attention notices) queues `sendMessage` `followUp` turns—not stdout push or `steer`. Session switch/exit stops host delivery without cancelling business tasks. Zero-model lifecycle `close` is verified; ordinary TUI, ACP defer, restart recovery and final live-business E2E remain unverified. |
| **Another machine over SSH** | `remote-client.cjs` | Single-file Node SSH client forwarding single-shot JSON actions to `collab.py` on the POSIX coordinator host. |
| **Restricted Windows Codex** | `remote-client.ps1` / `codex-ssh.cjs` | `remote-client.ps1` avoids the observed Node `spawn(ssh) EPERM` in the Windows Codex sandbox (verified against a macOS coordinator). `codex-ssh.cjs` launches a fresh desktop-context Codex via a one-shot InteractiveToken task to avoid Session 0 `0xC0000142`. |
| **Hermes host (optional plugin)** | `call_cli` plugin (`__init__.py`, `plugin.yaml`) | Captures native `HERMES_SESSION_*` origin identity, dispatches background runners (`notify=true, heartbeat=60`), advances authorized `intervene` on `status`/`events`, and supports source-level P1 compression continuity (`native_continuity.py`), action-scoped ended-owner recovery (`reconcile`/`close`), and operator fixed-target release (`dashboard/plugin_api.py`). Loading updated schemas requires a host-supported tool-table refresh or a new Hermes session; live current-chat consumption and Telegram E2E delivery (Issue #1) still require separate host acceptance. |
| **Hermes source checkout (optional compatibility patch)** | [`compat/hermes-silence/`](compat/hermes-silence/README.md) | **Independent of Call CLI core and plugin; never auto-applied.** Pinned strictly to `NousResearch/hermes-agent` commit `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19` and gated by read-only `compat/hermes-silence/check.py`. Requires manual check/apply on an idle clean source checkout and manual reverse rollback before upstream upgrades. |

**Terminal installation and in-CLI skill/plugin loading are two steps of one setup path.** See the [integration guide (`docs/integrations.md`, 简体中文)](docs/integrations.md) for entry-specific runtime dependency closures and loading commands.

<a id="capabilities"></a>
## Core capabilities

### 1. Continuous cross-CLI collaboration, single-shot control & return flow

- **Durable `seq` cursors & atomic settlement (`bridge.py`, `runner.py`, `events.py`):** Runner stdout notifications and `events`/`wait` share durable SQLite `rowid` (`seq`) cursors. Repeated identical statuses for the same tool call are deduplicated (ordinary tool notices on stdout are capped at 12 per turn while all tool failures and full `events` queries remain intact). Turn completion commits the task state and a single `<turn>:settled` event (`question_ids`, `answer_tail` up to 1000 chars, and pending-intervention `next_action`) in one SQLite transaction, preventing `close`/`intervene` races from dropping settlement notifications.
- **Numbered `CALLCLI_QUESTION` identity & deduplication:** Target agents ask questions via `CALLCLI_QUESTION Q1 <text>` (`Q[1-9][0-9]*` + space + body). Within a task/turn, identical label and complete-body fingerprint (SHA-256 and character length computed before the 8000-char public truncation cap, up to a 64000-char line buffer) are deduplicated to the first answerable `question_id` token so restating the same question requires only one `reply`. Different labels (`Q1`, `Q2`) or revised text under the same label remain distinct answerable tokens (with `question_note`), preserving the full output stream.
- **Coordinator-first review checkpoints, not automatic termination:** Hermes (`call_cli`) and OMP (`call_cli_omp`) model tools reject `timeout` execution budgets (including stale cached arguments). Instead, the runner loop emits at most one review checkpoint (`quiet_notice: true`, `notice_kind: "quiet"|"checkpoint"`) every **600 seconds**, shared across quiet and sustained-active turns so continuous tool activity cannot postpone review indefinitely. Review notices never auto-cancel, auto-close or auto-resume; the initiating agent inspects actual progress before waiting, intervening or cancelling. Legacy CLI `--timeout SECONDS|none` and Python `start(timeout=...)` retain explicit hard budgets for non-swarm ACPX tasks until cleared on a supported continuation via `--clear-timeout` (`clear_timeout` on `reply`/`intervene`; refused while `queued`).
- **Turn-guarded continuations, recovery cards & exact-proof `close` (`recovery.py`, `release.py`):**
  - `reply` and `intervene` accept `--expected-turn` (`expected_turn`) and retry-safe `--command-id` (`command_id`) to prevent stale intents from advancing an unobserved turn.
  - Task and settlement outputs include a credential-free `recovery` card (`task_id`, `turn`, `control_epoch`, `controller`, `seq`, `questions`, `commands`, `reservation`, `capabilities.exact_record_reconcile`, and `responsibility` honestly reporting `coverage: "uncovered"`, `pre_exit_hook: false`, `exit_interception: "uncovered"`, and `successor_takeover: "uncovered"`).
  - All public `close` actions (`collab.py`, OMP, Hermes) share `release.close_task` over durable SQLite `release_fences`. Local ACPX tasks commit the fence/attempt before calling `close` and require exact closed-record readback plus scoped `no-session` (`findSession(...) === undefined`). On `unknown` tasks, `close` (CLI/OMP) and `reconcile` (Hermes) run in **read-only probe mode (`probeTarget`, zero external `close` calls)** and never blindly repeat an uncertain close. CLI/OMP responses project a whitelisted `close_receipt` (`state`, `phase`, `stage`, `reason_code`, `external_close_attempted`); missing-record or remote tasks stay `unknown` (`exact_record_unavailable`) with workspace locks retained.
- **Same-principal compaction & ended-owner recovery (Hermes native source candidates):** `native_continuity.py` preserves native `call_cli` task access across rotating session compaction within the same trusted Hermes profile and route by walking committed read-only `state.db` schema-32 `compression` lineage without mutating task `Origin`. When an original owner session has a committed close reason (`agent_close`, `cli_close`, `tui_close`), same-profile/route sessions can inspect read-only status/events (without advancing pending interventions) or take over settled continuation via `expected_turn` + `command_id` (incrementing `control_epoch`). Separate operator-only fixed-target release routes (`/api/plugins/call-cli/release/*`) require host authentication and a private `0600` `release-policy.json`.

### 2. First-class AGY support, model-aware Swarm defaults & persistent approvals (`agy.py`, `agy_native.py`, `models.cjs`)

- **Model-specific Swarm defaults (`G4C` native / `G4H` worker vs. `G4A` off):**
  - Omitting `--model` (`model`) on AGY selects **G4C (`gemini-4-carbon`)**, which defaults to **native Swarm (`agy_swarm: "native"`)** with live-resolved **G4H (`gemini-4-hydrogen`)** execution workers (`--agy-worker-model` / `agy_worker_model` override).
  - Selecting **G4A (`gemini-4-argon`)** defaults to single-agent **`off` (`agy_swarm: "off"`)** for writing and knowledge tasks; pass `--agy-swarm native` (`agy_swarm: "native"`) to enable native Swarm on G4A.
  - G4C can also be overridden per task with `--agy-swarm off`. When using `--agy-swarm off`, discover exact advertised IDs via `node models.cjs /absolute/workdir` and pass an explicit `--model` ID (omitting `--model` with `--agy-swarm off` persists the G4C family on the legacy ACPX path, which is unverified on AGY 1.5.7 where tier-ID readback may reject before prompt submission). Existing tasks keep their persisted route; other CLIs are unchanged. See [`skill/references/agy-models.md` (简体中文)](skill/references/agy-models.md).
- **Single-owner AGY native Swarm settlement (`agy_native.py`):** Because public ACPX Shared Runtime does not expose `session/load`, `--agy-swarm native` runs a single ACP owner directly over `agy acp --disable-telemetry`, resolving live advertised models/effort and reading back `models.currentModelId`. The first `end_turn` settles only the foreground RPC while the task stays `running` and reserved; the owner serializes full `session/load` snapshots to observe native board wakeups without sending periodic prompts. Normal settlement and `close` release require the current task/turn's `native_result`, matching native worker `add`/`kill` completed events, and session unload (`native-add-kill-completed-and-session-unload`, without requiring ACP-omitted `rawOutput` and without claiming all arbitrary descendant processes died). Turn advances clear prior result/cleanup/roster state. Fatal output (`⚠ Agent execution terminated due to error.`), disconnects, missing worker cleanup, or uncertain cancellation retain `unknown` / HOLD (`native_workers_unproven`) and emit an atomic `attention_required: true` event routed via runner stdout, OMP `followUp`, or Hermes terminal notification without automatic replay.
- **Default `approve-all` vs. workspace write reservation:** New AGY tasks persist `agy_approval: "approve-all"` by default (even when `write` is `false`), permitting authorized shell and native tool calls within task scope while prompt guidance prioritizes native read/search tools. For strict read-only tool approvals, pass `--agy-approval approve-reads` (Hermes: `agy_approval: "approve-reads"`), which denies shell/write permission requests non-interactively. **Tool approval policies and `write: false` are not OS or filesystem sandboxes;** pass `--write` separately to declare edit authorization and reserve the workspace. `reply` and `intervene` preserve `agy_approval`; legacy tasks without the field keep their original `write` mapping, and OMP/Codex/Pi defaults (`approve-reads` unless `--write`) remain unchanged.
- **Safe authentication diagnostics (off / legacy ACPX path):** Failed turns project exact `error.detailCode: "AUTH_REQUIRED"`, or use public `runtime.watchSession()` for bounded passive replay (1s / 1000 matching events / 10000 total events) when exact host request, turn start, unique main-prompt RPC/response and failed turn result match with structured `error.code === -32000` and `data.authRequired === true`. Collisions, duplicates, missing journals or timeouts leave the cause unknown; only a fixed category and source label reach `settlement_note` (no raw error strings, AGY log scanning, credential edits or auto-replay).

### 3. Task-pinned skill passing (`skills.py`, `skill-stage.cjs`)

- **Initiator-agnostic contract:** Pass task skills from `collab.py`, OMP or Hermes through the same task model: repeatable `--skill MODE=PATH` (`location: "origin"` on the coordinator), `--target-skill MODE=PATH` (`location: "target"` on the execution device, including Windows paths), or `--skills-json ARRAY` (`skills` array in Hermes / OMP / Python `Collaboration.start` with optional `source` provenance).
- **Local-first selection & public discovery signals:** Inspect local skills first (`SKILL.md`, relative `references/`/`scripts/` and target dependencies). When searching public repositories, star counts and recent substantive content commits (distinguishing real workflow/script updates from license-only or formatting commits) serve as selection signals rather than an automatic quality guarantee; selected public bundles are pinned to an exact commit SHA in scratch without global installs.
- **Modes (`required` / `optional` / `reference`), SHA-256 snapshots & receipts:**
  - `required`: Target must read `SKILL.md` and needed resources and follow applicable instructions, or report `not_applicable` / `unavailable` with a concrete reason. If staging a `required` skill fails, no business prompt is submitted.
  - `optional` / `reference`: Target adopts when useful (or reports `not_adopted`) or consults as non-binding reference.
  - `origin` skills are snapshotted (`0700`/`0600`) at task creation with a per-file SHA-256 manifest—skipping dotfiles, symlinks, `node_modules`, `__pycache__`, `.pem`/`.key`, `id_*`, `credentials*` and `auth.json`, and enforcing caps of **4 MiB/file, 16 MiB/bundle and 1000 files**—and staged over stdin into `<os.tmpdir()>/call-cli-skills/<task>/<skill>/<sha>/`. `reply` and `intervene` reuse the pinned bundle; verified `close` removes the coordinator snapshot and target temp directory. Each turn records `CALLCLI_SKILL <id> <read|adopted|not_adopted|not_applicable|unavailable> <reason>` (`skill_verification: "reported"` vs. `"unverified"`, without downgrading valid business completion to `unknown`).
- **Read boundary:** ACPX 0.22.0's `fs/read_text_file` callback restricts reads to the `cwd` subtree, whereas agent-native file tools (verified on macOS/Windows OMP and macOS AGY) read the temporary skill directory directly without polluting `cwd`. Callback-only targets report `unavailable` honestly. Details: [`skill/references/skill-passing.md` (简体中文)](skill/references/skill-passing.md).

### 4. Parallel multi-CLI concurrency, progress-vs-completion & optional Hermes compatibility patch

- **Parallel multi-CLI execution & single-writer locks:** Run multiple target CLIs (e.g., AGY and OMP) concurrently on the same host or across SSH under isolated client/task/session identities. `--write` and optional cross-device `--resource` enforce single-writer workspace reservation while allowing read-only tasks (including AGY `approve-all` with `write: false`) to run in parallel alongside an active writer. A relay that only dispatches and consumes events must leave `write: false`; only the actual editor reserves that workspace.
- **Progress is not completion:** All four ACPX-backed targets (OMP, Codex, Pi and off/legacy AGY) require exit `0`, a valid owned `end_turn` result and a nonempty decoded answer after the last tool event (or a pending question in `waiting_reply`). `CALLCLI_STAGE`, `CALLCLI_SKILL`, empty output and tool status alone settle as `unknown`, retain the writer reservation and never authorize automatic replay. Late duplicate tool updates reset the final-answer window even when stdout tool notices are deduplicated.
- **Pluggable backend seam (`backends.py`, `acpx_backend.py`):** `backends.py` maps agent names to adapters implementing `prepare_start(model, effort, approval, ...)`, `run(store, task, emit)` and `control(task, action, timeout)`. Non-ACP backends can register in `BACKENDS` without adding daemons or separate stores. Details: [`docs/integrations.md#后端扩展接口` (简体中文)](docs/integrations.md#后端扩展接口).
- **Optional Hermes silence compatibility patch ([`compat/hermes-silence/README.md`](compat/hermes-silence/README.md)):**
  - **Independent & never auto-applied:** `compat/hermes-silence/hermes-2bd378ed-silence.patch` is an optional upstream Hermes source patch, completely separate from Call CLI core and the `call_cli` plugin. Call CLI never imports, installs or applies it, and never modifies Hermes profiles or restarts gateways.
  - **Exact baseline & read-only verification gate:** Strictly pinned to `NousResearch/hermes-agent` commit [`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`](https://github.com/NousResearch/hermes-agent/commit/2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19) (not observed later `main` `33cbf1e813d4ff35c7709b21c809f55eb3af9288`). `compat/hermes-silence/manifest.json` pins pre/post-image SHA-256 hashes across all 10 modified gateway files and 3 test files, verified by the read-only standard-library gate `python3 compat/hermes-silence/check.py "$HERMES" --state before|after`.
  - **Scope & maintenance discipline:** In isolated unit/synthetic tests on the pinned archive (`138` passed across the 5-file gateway suite via `compat/hermes-silence/run_tests.py`), it removes a bot opener's silence exemption when a human redirect requiring a reply is consumed, withholds bare silence-marker interim commentary while preserving segment/TTS flush boundaries, strips an exact unquoted leading silence control line before nonblank text instead of suppressing that text, and prevents commentary ticks from flushing buffered bare markers early. Operators must check and apply it manually only on a clean, idle checkout at the exact commit, reverse-apply (`git apply --reverse`) against a verified `after` state before upgrading Hermes, and retire the patch once upstream ships and verifies equivalent fixes.

<a id="how-it-works"></a>
## Architecture & collaboration lifecycle

<div align="center">

<img src="docs/assets/call-cli-architecture.png" alt="Call CLI conceptual architecture: an initiating CLI entry coordinates through the Call CLI POSIX bridge and fans out in parallel to independent target CLIs (OMP, Codex, Pi, AGY) locally or over SSH" width="100%">

</div>

[Open the full-resolution architecture diagram](docs/assets/call-cli-architecture.png) — on a phone, open the image and zoom in for module labels.

*Conceptual architecture diagram (not a live runtime screenshot): one initiating entry owns the task in Call CLI (`bridge.py` / `runner.py`), which dispatches turns to **parallel, isolated target CLI sessions** (`OMP`, `Codex`, `Pi`, `AGY`) locally or over SSH. Target CLIs are shown side by side as parallel targets—the diagram lines do not represent sequential target-to-target dependencies (this does not prohibit user-authorized hierarchical coordination)—and Call CLI never attaches to manually opened terminals.*

### Single-task turn & intervention sequence

```text
Initiating CLI                   Call CLI (Store + Runner)            Target CLI (via ACPX / Native ACP)
      │                                      │                                        │
      ├──── 1. start (+ optional skills) ───►├──── lock + session + stage skills ────►│
      │◄─── 2. wait / events (--after seq) ──┤◄─── CALLCLI_STAGE / tool / QUESTION ───┤
      │     (or 600s review checkpoint)      │     (or AGY native session/load)       │
      ├──── 3a. reply (--question-id) ──────►├──── turn+1 in same native session ────►│
      ├──── 3b. intervene (--command-id) ───►├──── cancel turn → confirm settled ────►│
      │     (next wait/status advances) ────►├──── launch revised turn + skills ─────►│
      │◄─── 4. <turn>:settled + close ───────┼◄─── turn.result + fenced close proof ──┤
```

**Control semantics (`steer` vs. `queue` vs. `cancel+resume`):** Public ACPX 0.22.0 `startTurn` rejects in-turn `steer`, and in-process queueing (`queue`) waits for the active turn to finish rather than redirecting it mid-flight. Call CLI `intervene` therefore implements explicit **cancel-and-resume**: it records a retry-safe `command_id` (tracking `pending` → `queued` → `target_started` → `target_completed` / `target_cancelled` / `target_unknown` / `superseded` / `withdrawn`), requests cancellation of the running turn, waits for authoritative cancellation settlement, and only then launches the revised prompt in the **same native session** on the next `wait` (generic CLI / OMP adapter) or `status`/`events` call (Hermes). `pending`/`queued` means recorded, not yet applied; `unknown` results are preserved for reconciliation and never replayed automatically.

<a id="evidence-and-boundaries"></a>
## Evidence layers & verification boundaries

Call CLI strictly separates **source implementation, offline/synthetic tests, locally installed runtime checks, live cross-CLI execution and end-user host delivery** ([`skill/references/core-requirements.md`](skill/references/core-requirements.md), [`VERIFICATION.md`](VERIFICATION.md)):

| Layer | Verified scope | Explicit boundary / not claimed |
| :-- | :-- | :-- |
| **Source & offline test suites** | Python unittest suite (`261` discovered cases, including `12` opt-in native/live probes skipped by default; see slice runs in `VERIFICATION.md`) and `77` Node tests passing across `6` test files (`test_skill_stage.cjs`, `test_shared_prompt.cjs`, `test_models.cjs`, `test_codex_ssh.cjs`, `test_omp_extension.mjs`, `test_release_backend.cjs`). | Fake transports, synthetic endpoints, and isolated SQLite fixtures do not prove live model behavior, installed host payloads, or end-user notification routing. |
| **Native ACPX & AGY protocol probes (`RUN_NATIVE_ACPX_TESTS=1`)** | Opt-in `test_native_shared`, `test_native_skills`, and `test_native_release` pass against installed ACPX 0.22.0 with isolated `HOME` and synthetic ACP endpoints; zero-prompt and single-worker `session/load` replay verified on AGY 1.5.7 (`test_agy_native_owner.py`). | Real ACPX / ACP transport with synthetic endpoints or bounded probes—not general live LLM compliance for all multi-turn/cancellation permutations. |
| **Live local & SSH collaborations** | Exercised on macOS with **ACPX 0.22.0** and **AGY 1.5.3 / 1.5.7** (`gemini-4-carbon-medium` / `gemini-4-hydrogen-medium`, `gemini-4-argon` / `medium`, `claude-opus-5-5` / `medium`, `gemini-3.8-flash` / `low`) plus local **OMP**: model/effort readback, concurrent AGY + OMP execution, mid-turn `intervene` cancel+resume, and `origin`/public PDF skill passing. Cross-device Windows SSH **OMP** (`ACPX 0.21.1`) and **Pi**/**Codex** paths verified for their tested scenarios. | Each probe certifies only the exact CLI, model, entry point and device exercised—not every untested CLI/device permutation, live AGY swarm foreground/background cancellation, arbitrary detached descendant process kill (`arbitraryDetachedDescendantsProven: false`), or GUI automation. |
| **Host session & Telegram delivery** | Optional Hermes plugin (`__init__.py`), P1 compression continuity (`native_continuity.py`), ended-owner recovery (`release.py`), experimental OMP RPC extension (`omp-extension.mjs`), and optional unapplied Hermes silence patch (`compat/hermes-silence/`, `138` passed in 5-file isolated gateway suite) are verified in source/synthetic suites. | **Never extrapolated to all hosts or Telegram E2E:** loading updated schemas requires a host-supported tool-table refresh or a new session; native registration, local deployment, or synthetic collector tests do **not** prove live host consumption across all CLIs, live gateway patch reload, Telegram empty-notification resolution (**Issue #1**), upstream AGY error reporting (**Issue #2**), or idle wakeup after leaving a collaboration. |

```sh
# Run the standard offline verification suites (no model credentials or network required)
python3 -B -m unittest discover -v
node --test test_skill_stage.cjs test_shared_prompt.cjs test_models.cjs test_codex_ssh.cjs test_omp_extension.mjs test_release_backend.cjs

# Optional: real local ACPX queue-owner + synthetic ACP endpoints (no live LLM calls)
RUN_NATIVE_ACPX_TESTS=1 python3 -B -m unittest test_native_skills test_native_shared test_native_release -v
```

<details>
<summary><strong>Scope and safety — read before granting access</strong></summary>

- **Fresh sessions only:** Call CLI never attaches to or injects input into manually opened terminals, TUIs or desktop GUIs.
- **Approvals and release fences are not OS sandboxes:** OMP/Codex/Pi use `approve-reads` by default and `approve-all` with `--write`. New AGY tasks persist `agy_approval: "approve-all"` by default (or explicit `--agy-approval approve-reads`). Neither approval mode, `write: false`, nor SQLite `release_fences` is an OS/filesystem sandbox or proof that arbitrary detached descendant processes stopped; SSH and target CLIs run with your OS account's permissions.
- **Reconcile before retry:** `queued`/`pending` means submitted, not applied. If a turn settles as `unknown` or a client returns `do_not_replay: true`, keep the workspace reservation on HOLD and use exact-record probe (`close` in probe-only mode on CLI/OMP, or `reconcile` on Hermes) before retrying.
- **Skill bundle hygiene:** Snapshot staging skips dotfiles, symlinks, `node_modules`, `__pycache__` and common private key filenames and enforces size/file caps, but it is **not** a malware sandbox or automated privacy scrubber. Only pass directories intended for the target model.
- **Single-coordinator state:** Keep SQLite task databases, `client_id` handles, prompts, `release-policy.json` and credentials in private user directories. One POSIX coordinator owns the SQLite store and locks; distributed multi-host locking, crash stdout replay and offline idle wakeup are not promised.

</details>

<a id="documentation"></a>
## Documentation

### For the AI operating Call CLI

| Guide | What it covers | Language |
| :-- | :-- | :-- |
| [Installation & configuration (`docs/configuration.md`)](docs/configuration.md) | Environment discovery, SSH aliases, ACPX agent config, auth, permissions, version checks and acceptance steps. | 简体中文 |
| [CLI & module integration (`docs/integrations.md`)](docs/integrations.md) | OMP/Codex/Pi skill installation, experimental single-load OMP RPC adapter, optional Hermes plugin, ended-owner recovery / release fences, runtime dependency closures and backend seam (`backends.py`). | 简体中文 |
| [Online collaboration (`docs/usage.md`)](docs/usage.md) | `seq` cursor polling, numbered `CALLCLI_QUESTION` identity, `reply`, `intervene` cancel+resume, 600s review checkpoints, `--clear-timeout`, and `close`. | 简体中文 |
| [Core requirements (`skill/references/core-requirements.md`)](skill/references/core-requirements.md) | Canonical maintenance requirements for continuous bidirectional collaboration, AGY native Swarm, ended-owner recovery and layered evidence accounting. | 简体中文 |
| [Task skill passing (`skill/references/skill-passing.md`)](skill/references/skill-passing.md) | Local-first selection, public repo signals, `required`/`optional`/`reference` modes, staging limits and `CALLCLI_SKILL` receipts. | 简体中文 |
| [AGY models, Swarm & approvals (`skill/references/agy-models.md`)](skill/references/agy-models.md) | G4C native Swarm / G4H worker and G4A off defaults, single-owner `agy_native.py` settlement, `models.cjs` discovery, persistent `agy_approval` and `unknown` diagnostics. | 简体中文 |
| [Hermes host & continuity (`skill/references/hermes-host.md`)](skill/references/hermes-host.md) | Native origin capture, P1 rotating-compaction continuity, ended-owner recovery/reconcile, and `[SILENT]` notification turn guidance. | 简体中文 |
| [Maintenance & version checks (`skill/references/maintenance.md`)](skill/references/maintenance.md) | Once-per-task source/installation checks and idle-only ACPX maintenance boundaries. | 简体中文 |
| [Windows Codex SSH (`CODEX_SSH.md`)](CODEX_SSH.md) | Desktop-context `codex-ssh.cjs` bridge and native PowerShell client (`remote-client.ps1`). | 简体中文 |
| [Optional Hermes silence patch (`compat/hermes-silence/README.md`)](compat/hermes-silence/README.md) | Unapplied, optional patch pinned to `NousResearch/hermes-agent` `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`, read-only `check.py` gate, manual apply/rollback, and isolated test runner. | English |

### For users and contributors

[Verification (`VERIFICATION.md`, English)](VERIFICATION.md) · [Roadmap (`ROADMAP.md`, 简体中文)](ROADMAP.md) · [Security (`SECURITY.md`, English)](SECURITY.md) · [Contributing (`CONTRIBUTING.md`, English)](CONTRIBUTING.md) · [License (`LICENSE`)](LICENSE)

*Note: `docs/assets/render_banner.py` uses Pillow only when regenerating repository artwork; Pillow is not a runtime or test dependency of Call CLI.*

## Acknowledgements

Built on [ACPX](https://github.com/openclaw/acpx) and the native capabilities of OMP, Codex, Pi, AGY and Hermes. Call CLI is an independent project and is not affiliated with those upstream tools.

<div align="center">

**Different tools. Shared direction.**

[MIT License](LICENSE) · [简体中文](README.zh-CN.md)

</div>

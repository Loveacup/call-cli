# Optional Hermes silence compatibility patch

This is an **unapplied, optional upstream-source patch**, not Call CLI core or a
plugin-install step. Nothing in Call CLI imports, installs or applies it. It does
not modify Hermes automatically, install dependencies, change profiles, or restart
anything. Applying source changes does not prove a running gateway loaded them.
No restart or production-effect promise is made.

## Exact scope and provenance

Only supported Hermes source revision:
[`2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`](https://github.com/NousResearch/hermes-agent/commit/2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19).
Do not apply to another revision, even if Git reports that hunks apply.
The observed official `main` on 2026-11-11 was
`33cbf1e813d4ff35c7709b21c809f55eb3af9288`, not this pinned base.
Whether either fix is present there remains unverified. This package makes no
claim of compatibility with any existing live checkout.

The patch was regenerated from a clean `git archive` of that public commit, with
no local source overlay. It changes ten gateway files and includes a new mixed-turn
regression file plus four stream regression cases. `manifest.json` pins the patch
and every complete affected file before/after using SHA256 (including required
absence of the new test before application). `check.py` is a standard-library,
read-only gate: it imports no Hermes code and has no apply/install command. An
archive alone is not an applicable checkout; the gate also requires exact Git HEAD.
There is no private vault/provider, code-prompt scope, personal path, config,
credential, dialogue or historical-log dependency in this package.

The two fixes are:

- A successfully consumed human steer/redirect needing a reply (including unknown
  addressing) removes a machine/bot opener's silence exemption for that running
  turn. Diagnostic muting cannot hide its normal silence warning. Independent
  queued turns keep their own policy; original opener provenance remains stored.
- Completed interim commentary that is exactly a recognized silence marker is
  withheld. Streamed marker completion keeps segment/TTS flush boundaries and the
  next substantive answer. Normal answers, prose mentioning markers and visible
  silence warnings remain visible. Healthy explicit room `allow_silence`, bot-only
  and known-unaddressed behavior remain supported; failures are not globally hidden.

## Manual check, application and rollback

Use an already available Python 3 and Git, and a separately approved **idle source
checkout**, preferably a disposable checkout first. This package installs neither.
Do not point it at a profile directory. Do not patch a live working tree with other
changes, use fuzzy `patch`, force application, or bypass a failing gate. Keep the
package outside the target checkout. Set these paths locally:

```sh
COMPAT=/absolute/path/to/call-cli/compat/hermes-silence
HERMES=/absolute/path/to/approved/hermes-source
```

Before application, require the whole checkout to have no tracked changes or
non-ignored untracked files; inspect only an authorized source checkout. Each
command must succeed before continuing. Stop on any mismatch and obtain a reviewed
rebase; never overwrite another change.

```sh
cd "$HERMES"
test -z "$(git status --porcelain --untracked-files=all)" &&
python3 "$COMPAT/check.py" "$HERMES" --state before &&
git apply --check "$COMPAT/hermes-2bd378ed-silence.patch"
```

That is the read-only check, not application. After explicit local approval, run
the gate again and apply manually; this does not stage or commit files:

```sh
cd "$HERMES" &&
test -z "$(git status --porcelain --untracked-files=all)" &&
python3 "$COMPAT/check.py" "$HERMES" --state before &&
git apply --check "$COMPAT/hermes-2bd378ed-silence.patch" &&
git apply "$COMPAT/hermes-2bd378ed-silence.patch" &&
python3 "$COMPAT/check.py" "$HERMES" --state after
```

The gate rejects any affected file with different bytes, even outside the changed
hunks; it rejects symlinked affected paths and an existing new-test path. Check/apply
is not a filesystem lock: keep other writers and updates stopped through the
operation. Review local diff and run authorized isolated checks before considering
separate service activation. No service/configuration command is included here.

Rollback is also manual and only for an exact postimage, with no intervening edits:

```sh
cd "$HERMES" &&
python3 "$COMPAT/check.py" "$HERMES" --state after &&
git apply --reverse --check "$COMPAT/hermes-2bd378ed-silence.patch" &&
git apply --reverse "$COMPAT/hermes-2bd378ed-silence.patch" &&
python3 "$COMPAT/check.py" "$HERMES" --state before
```

If the postimage gate fails, stop; do not reset, delete or overwrite newer work.
Rollback changes source bytes only, not code already loaded by a running process.

## Observed verification, not deployment

On macOS arm64 with existing Python **3.11.13**, pytest **9.1.1**, synthetic homes,
an allowlisted environment, bootstrap disabled, blocked network connections/child
processes and an outside-write audit:

| Check on exact public archive | Before product patch | After patch |
| --- | --- | --- |
| Original S1-S7 plus controls | 8 passed, S5 failed | 9 passed |
| Literal investigation probes | 29 passed, 6 failed | 35 passed |
| Literal + existing stream/filter/preflight | not run as combined baseline | 126 passed total |
| Mixed human/queue/bot/room policy suite | not run as combined baseline | 99 passed |
| Shipped mixed-turn regression file alone | 13 failed, 8 passed (test added to pristine product) | 21 passed |
| Adjacent silence/redirect/preflight/voice cases | not run as combined baseline | 32 passed |

The 126 includes the 35 probes; totals across suites overlap and are not a unique
coverage count. RED preceded product edits. Literal RED included a real worker
callback emitting a marker to a synthetic collector, four context-free consumer
rows reaching the real Telegram adapter's **mock** Bot API, and a streamed marker
boundary leaking before the next answer. Four consumer rows are not four producer
end-to-end proofs. Scheduled-heartbeat wiring already suppressed callbacks before
the patch. A separate non-pytest busy-handler -> OOB user row -> response shaping
-> first-delivery collector smoke observed a visible warning and preserved internal
opener. Packaged-patch application/reversal and deliberate version/hash mismatches
were separately exercised in scratch.

### Reproduce the nine-file check

Use a disposable, already patched source checkout at the exact pinned revision,
with no live service using it. Set `HERMES` as above and `PYTHON` to an **absolute**
path to an existing environment containing Hermes' test/runtime dependencies,
`pytest` and `pytest-asyncio`. No installation is performed here. The observed
environment was Python 3.11.13 / pytest 9.1.1; this is not supported-runtime CI.
The command below runs the nine public test files together (217 passed locally).
It needs no private harness, profile, credentials, model, or platform access.

Run from a shell with `mktemp`, `mkdir` and `env`. `RUN` is a new disposable scratch
directory; retain it for evidence. Environment variables are allowlisted, homes
and all pytest outputs are explicitly redirected there, bytecode/cache plugins
are disabled, and the inline bootstrap blocks network connections, child processes
and writes outside `RUN`. Bootstrap recovery/installation is disabled. This is a
test guard, not an OS security sandbox; review test dependencies before execution.
Do not use an ordinary `pytest` invocation against a live profile instead.

```sh
PYTHON=/absolute/path/to/existing/test-environment/bin/python
RUN=$(mktemp -d "${TMPDIR:-/tmp}/hermes-silence-check.XXXXXX") &&
mkdir -p "$RUN/home/.hermes" "$RUN/tmp" &&
env -i PATH=/usr/bin:/bin LANG=en_US.UTF-8 HOME="$RUN/home" \
  HERMES_HOME="$RUN/home/.hermes" HERMES_TEST_ISOLATION="$RUN/home/.hermes" \
  TMPDIR="$RUN/tmp" PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  PYTHONPATH="$HERMES" RUN="$RUN" "$PYTHON" -B - <<'PY'
import os, socket, sys, types
from pathlib import Path
root = Path(os.environ['PYTHONPATH']).resolve()
sandbox = Path(os.environ['RUN']).resolve()
os.chdir(sandbox)
sys.path.insert(0, str(root))
sys._hermes_relaunched = True
sys.modules['hermes_bootstrap'] = types.ModuleType('hermes_bootstrap')
def blocked(*args, **kwargs):
    raise RuntimeError('TEST_NETWORK_BLOCKED')
socket.socket.connect = socket.socket.connect_ex = blocked
socket.create_connection = blocked
sys.modules['hermes_bootstrap']._happy_eyeballs_create_connection = blocked
def audit(event, args):
    if event == 'open':
        path, mode, flags = args
        writing = (isinstance(mode, str) and any(c in mode for c in 'wax+')) or (
            isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if writing and isinstance(path, (str, bytes, os.PathLike)):
            if not Path(os.fsdecode(path)).resolve().is_relative_to(sandbox):
                raise PermissionError('TEST_WRITE_OUTSIDE_SCRATCH')
    if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.rename'):
        for path in args[:2] if event == 'os.rename' else args[:1]:
            if isinstance(path, (str, bytes, os.PathLike)):
                if not Path(os.fsdecode(path)).resolve().is_relative_to(sandbox):
                    raise PermissionError('TEST_MUTATION_OUTSIDE_SCRATCH')
    if event in ('subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn'):
        raise PermissionError('TEST_CHILD_PROCESS_BLOCKED')
sys.addaudithook(audit)
import pytest
targets = ['test_mixed_turn_silence.py', 'test_stream_consumer.py',
           'test_response_filters.py', 'test_room_allow_silence.py',
           'test_bot_peer_runtime_notices.py', 'test_gateway_silence_tokens.py',
           'test_busy_redirect_anchor.py', 'test_silence_after_reply_line.py',
           'test_authority_busy_preempt_parity.py']
print('Evidence directory:', sandbox, flush=True)
raise SystemExit(pytest.main(['-p', 'pytest_asyncio.plugin', '-p', 'no:cacheprovider',
    '--confcutdir', str(root / 'tests'), '--basetemp', str(sandbox / 'tmp/pytest'),
    '--log-file', str(sandbox / 'pytest.log'), '-q',
    *[str(root / 'tests/gateway' / name) for name in targets]]))
PY
```

The full synthetic reproduction harness and RED/GREEN receipts are retained in the
maintainer's scratch handoff, not bundled as an installer or private evidence dump.
These results are **not** full upstream CI (runtime support is Python 3.14;
`requires-python` declares `>=3.11,<3.15`, with 3.11 retained as an updater bridge),
real-model execution, actual Telegram delivery/message IDs, native vault
integration, or proof of the cause of any user's historical event. Stored transcript
markers are not proof of platform delivery. Desktop hydration/native TUI history
presentation is unchanged; already-streamed speech before completion classification
is not claimed universally fixed. No private history was inspected.

## Retirement and license

This is temporary compatibility material. On an upstream release containing an
equivalent fix, independently verify both behaviors and regression boundaries,
remove this local patch using its exact postimage before updating, and retire the
package/link once no supported deployment needs it. Never auto-reapply it after an
upgrade. Latest upstream coverage is unknown; a newer main revision alone does not
establish that either fix shipped.

Hermes code/context is MIT, Copyright (c) 2025 Nous Research. The upstream notice
and complete terms are retained in [LICENSE.hermes](LICENSE.hermes); Call CLI's
root MIT license remains separate. No claim of upstream endorsement or acceptance.

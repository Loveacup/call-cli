/**
 * Thin ESM bridge connecting Call-CLI's OarBackend to real @botiverse/oar public modules.
 * Speaks newline-delimited JSON on stdin/stdout; fails closed when CALL_CLI_OAR_DIST is unavailable.
 */
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { pathToFileURL } from "node:url";

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
function toOarInputUuid(rawId) {
  if (!rawId) { return undefined; }
  if (UUID_RE.test(rawId)) { return rawId; }
  const h = crypto.createHash("sha256").update(String(rawId)).digest("hex");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-4${h.slice(13, 16)}-8${h.slice(17, 20)}-${h.slice(20, 32)}`;
}

let rl = null;

function emit(payload) {
  process.stdout.write(JSON.stringify(payload) + "\n");
}

function emitAndExit(payload, code) {
  rl?.close();
  const line = JSON.stringify(payload) + "\n";
  if (process.stdout.destroyed || process.stdout.writableEnded) {
    process.exit(code);
    return;
  }
  const timer = setTimeout(() => process.exit(code), 500);
  timer.unref();
  process.stdout.write(line, () => {
    clearTimeout(timer);
    process.exit(code);
  });
}

function fail(stage, code, message, extra = {}) {
  emit({ type: "error", stage, code, message: String(message), ...extra });
}

function resolveOarDist() {
  const raw = (process.env.CALL_CLI_OAR_DIST || "").trim();
  if (!raw) {
    throw new Error("CALL_CLI_OAR_DIST is not configured");
  }
  const resolved = path.resolve(raw);
  if (!fs.existsSync(resolved)) {
    throw new Error(`CALL_CLI_OAR_DIST does not exist: ${resolved}`);
  }
  const hasPkg = fs.existsSync(path.join(resolved, "package.json"));
  const distDir = fs.existsSync(path.join(resolved, "dist", "observe", "index.js"))
    ? path.join(resolved, "dist")
    : resolved;
  const pkgPath = hasPkg ? path.join(resolved, "package.json") : path.join(path.dirname(distDir), "package.json");
  const pkg = fs.existsSync(pkgPath) ? JSON.parse(fs.readFileSync(pkgPath, "utf8")) : {};
  const observePath = path.join(distDir, "observe", "index.js");
  const agyPath = path.join(distDir, "runtimes", "antigravity", "index.js");
  const testingPath = path.join(distDir, "testing", "index.js");
  if (!fs.existsSync(observePath) || !fs.existsSync(agyPath)) {
    throw new Error(`Incomplete OAR dist directory at ${distDir}`);
  }
  return {
    pkgName: pkg.name || "@botiverse/oar",
    oarVersion: pkg.version || "unknown",
    distDir,
    observePath,
    agyPath,
    testingPath,
  };
}

let oar;
try {
  oar = resolveOarDist();
} catch (err) {
  fail("oar_load", "oar_dist_unavailable", err instanceof Error ? err.message : String(err));
  process.exit(1);
}

const observe = await import(pathToFileURL(oar.observePath).href);
const { antigravityRuntime } = await import(pathToFileURL(oar.agyPath).href);

const {
  eventsOf,
  initialConversation,
  reduceConversation,
  initialSessionView,
  reduceSessionView,
  initialStatus,
  reduceStatus,
} = observe;

let session = null;
let driverName = "antigravity";
let unsubscribe = null;
let exitedCode = null;
let unexpectedExit = false;
let disposed = false;

const actions = new Map();
let statusState = initialStatus;
let convState = initialConversation();
let viewState = initialSessionView();
const trackedSteers = new Map(); // logicalInputId -> { oarInputId, requestId, acceptedSeq, observed }
const requestToInput = new Map(); // requestId -> logicalInputId
const oarToLogicalInput = new Map(); // oarInputId -> logicalInputId

function buildScriptedTurnHandler(spec = {}) {
  const mode = spec.mode || "normal";
  return async (turn) => {
    if (mode === "steer_wait") {
      turn.say("CALLCLI_STAGE waiting_for_steer\n");
      const deadline = Date.now() + (spec.waitMs ?? 3000);
      while (turn.steered.length === 0 && !turn.signal.aborted && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 15));
      }
      if (turn.signal.aborted) {
        return;
      }
      if (turn.steered.length > 0) {
        turn.say(`Steered output: ${turn.steered.join(" | ")}\n`);
      } else {
        turn.say("No steer arrived\n");
      }
      return;
    }
    if (mode === "abort_wait") {
      turn.say("CALLCLI_STAGE waiting_for_cancel\n");
      await new Promise((resolve) => {
        if (turn.signal.aborted) { resolve(); return; }
        turn.signal.addEventListener("abort", resolve, { once: true });
      });
      return;
    }
    if (mode === "outside_turn") {
      const bg = turn.task({ id: "bg-1", type: "command", title: "bg-task" });
      turn.say("Initial answer before outside-turn task\n");
      setTimeout(() => {
        bg.update("late background progress");
        bg.end("completed", "late background done");
      }, spec.delayMs ?? 25);
      return;
    }
    if (mode === "exit_mid_turn") {
      turn.say("CALLCLI_STAGE partial_before_exit\n");
      await new Promise((r) => setTimeout(r, 20));
      turn.exit(spec.exitCode ?? 2);
      return;
    }
    if (spec.toolName) {
      await turn.tool(spec.toolName, spec.toolInput ?? "{}", () => spec.toolResult ?? "ok");
    }
    turn.say(spec.responseText ?? `CALLCLI_STAGE running_scripted\nScripted response to: ${turn.input}\n`);
  };
}

function attachSessionObservers(activeSession) {
  unsubscribe = activeSession.rawEvents((record) => {
    if (record.kind === "request" && record.direction === "toRuntime") {
      const known = (record.body.kind === "native" || record.body.kind === "open")
        ? null
        : (record.body.kind === "withdraw" ? record.body : record.body.kind);
      if (known !== null) {
        actions.set(record.id, known);
      }
      if (record.body.kind === "steer" && record.body.inputId) {
        requestToInput.set(record.id, oarToLogicalInput.get(record.body.inputId) ?? record.body.inputId);
      }
    }

    const prevStatus = statusState;
    statusState = reduceStatus(statusState, record, activeSession.id);
    convState = reduceConversation(convState, record);
    viewState = reduceSessionView(viewState, record);

    const flat = eventsOf(record, actions);

    if (record.kind === "response" && record.body.kind === "accepted") {
      const inputId = requestToInput.get(record.requestId);
      if (inputId && trackedSteers.has(inputId)) {
        const entry = trackedSteers.get(inputId);
        entry.acceptedSeq = record.seq;
      }
    }

    if (record.kind === "response") {
      actions.delete(record.requestId);
    }

    // Detect ACP user_message_chunk echo (retained verbatim in Frame.body.native even when events is []).
    if (
      record.kind === "frame" &&
      record.sessionId === activeSession.id &&
      record.agentPath.length === 0 &&
      record.body?.native?.sessionUpdate === "user_message_chunk"
    ) {
      for (const [inputId, entry] of trackedSteers.entries()) {
        if (!entry.observed && entry.acceptedSeq !== null && record.seq > entry.acceptedSeq) {
          entry.observed = true;
          emit({
            type: "input_observed",
            inputId,
            requestId: entry.requestId,
            kind: "acp_user_message_chunk",
            seq: record.seq,
          });
        }
      }
    }

    for (const ev of flat) {
      const isRoot = ev.sessionId === activeSession.id && ev.agentPath.length === 0;
      const outsideTurn = Boolean(
        isRoot &&
        record.kind === "frame" &&
        ev.kind !== "turn_ended" &&
        ev.kind !== "usage" &&
        ev.kind !== "model" &&
        ev.kind !== "effort" &&
        (viewState.messages.at(-1)?.kind === "activity" ||
          (prevStatus.kind === "idle" && statusState.kind === "idle"))
      );

      if (isRoot && (ev.kind === "input_handled" || (ev.kind === "user_message" && ev.inputId))) {
        const mapped = ev.inputId ? (oarToLogicalInput.get(ev.inputId) ?? ev.inputId) : undefined;
        const inputId = mapped ?? requestToInput.get(ev.requestId);
        if (inputId && trackedSteers.has(inputId) && !trackedSteers.get(inputId).observed) {
          const entry = trackedSteers.get(inputId);
          entry.observed = true;
          emit({
            type: "input_observed",
            inputId,
            requestId: entry.requestId ?? ev.requestId ?? null,
            kind: ev.kind,
            seq: record.seq,
          });
        }
      }

      if (
        isRoot &&
        !outsideTurn &&
        (ev.kind === "text_delta" || ev.kind === "tool_call_started" || ev.kind === "turn_ended")
      ) {
        for (const [inputId, entry] of trackedSteers.entries()) {
          if (!entry.observed && entry.acceptedSeq !== null && record.seq > entry.acceptedSeq) {
            entry.observed = true;
            emit({
              type: "input_observed",
              inputId,
              requestId: entry.requestId,
              kind: `post_steer_${ev.kind}`,
              seq: record.seq,
            });
          }
        }
      }

      if (ev.kind === "turn_ended" && isRoot) {
        emit({
          type: "turn_ended",
          outcome: ev.outcome,
          model: activeSession.model().value,
          effort: activeSession.effort().value,
          status: activeSession.status().value,
          seq: ev.seq,
        });
      } else if (ev.kind === "exited") {
        exitedCode = ev.code;
        if (!disposed) {
          unexpectedExit = true;
        }
        emit({
          type: "exited",
          code: ev.code,
          reason: ev.reason ?? null,
          seq: ev.seq,
        });
      } else if (ev.kind === "model" && isRoot) {
        emit({ type: "event", event: { kind: "model", model: ev.model }, outsideTurn: false, seq: ev.seq });
      } else if (ev.kind === "effort" && isRoot) {
        emit({ type: "event", event: { kind: "effort", effort: ev.effort }, outsideTurn: false, seq: ev.seq });
      } else if (
        ev.kind === "text_delta" ||
        ev.kind === "tool_call_started" ||
        ev.kind === "tool_call_ended" ||
        ev.kind === "task_started" ||
        ev.kind === "task_updated" ||
        ev.kind === "task_ended"
      ) {
        emit({ type: "event", event: ev, outsideTurn, seq: ev.seq });
      }
    }
  }, { sessionId: activeSession.id, afterSeq: -1 });
}

async function handleMessage(msg) {
  try {
    if (msg.op === "open") {
      driverName = msg.driver || process.env.CALL_CLI_OAR_TEST_DRIVER || "antigravity";
      let runtime;
      let installation;
      if (driverName === "scripted") {
        if (!fs.existsSync(oar.testingPath)) {
          throw new Error(`OAR testing module missing at ${oar.testingPath}`);
        }
        const testing = await import(pathToFileURL(oar.testingPath).href);
        runtime = testing.scriptedRuntime({
          id: "scripted-agy",
          model: msg.model || "scripted-1",
          turn: buildScriptedTurnHandler(msg.script || {}),
        });
        installation = await runtime.installation();
      } else if (driverName === "antigravity") {
        runtime = antigravityRuntime;
        installation = await runtime.installation({ env: process.env });
      } else {
        throw new Error(`Unsupported OAR driver: ${driverName}`);
      }

      if (installation.kind !== "available") {
        fail("installation", installation.kind, installation.reason || "OAR runtime unavailable");
        process.exitCode = 1;
        return;
      }

      const sessionOptions = {
        cwd: msg.cwd,
        ...(driverName === "antigravity" && msg.model ? { model: msg.model } : {}),
        ...(driverName === "antigravity" && msg.effort ? { effort: msg.effort } : {}),
        ...(driverName === "antigravity" && msg.appendSystemPrompt ? { appendSystemPrompt: msg.appendSystemPrompt } : {}),
        ...(msg.resume ? { resume: msg.resume } : {}),
        env: process.env,
      };
      session = await runtime.session(installation, sessionOptions);

      const openedRecord = session.records().find((r) => r.kind === "response" && r.body.kind === "opened");
      const openFrame = session.records().find(
        (r) => r.kind === "frame" && (r.body.type === "session/new" || r.body.type === "session/resume" || r.body.type === "session/load")
      );
      emit({
        type: "opened",
        driver: driverName,
        runtimeId: runtime.id,
        oarPackage: oar.pkgName,
        oarVersion: oar.oarVersion,
        sessionId: session.id,
        openMethod: openFrame?.body?.type ?? (msg.resume ? "session/resume" : "session/new"),
        model: session.model().value,
        effort: session.effort().value,
        capabilities: session.capabilities,
        placement: openedRecord?.body?.placement ?? null,
      });
      attachSessionObservers(session);
      return;
    }

    if (!session) {
      fail("control", "no_session", "Session is not open");
      return;
    }

    if (msg.op === "prompt") {
      const oarInputId = toOarInputUuid(msg.inputId);
      if (msg.inputId && oarInputId) { oarToLogicalInput.set(oarInputId, msg.inputId); }
      const outcome = await session.prompt(msg.input, oarInputId ? { inputId: oarInputId } : undefined);
      emit({
        type: "control",
        op: "prompt",
        inputId: msg.inputId ?? null,
        oarInputId: oarInputId ?? null,
        requestId: outcome.requestId ?? null,
        outcome,
      });
      return;
    }

    if (msg.op === "steer") {
      const inputId = msg.inputId;
      const oarInputId = toOarInputUuid(inputId);
      if (inputId) {
        if (oarInputId) { oarToLogicalInput.set(oarInputId, inputId); }
        trackedSteers.set(inputId, { oarInputId: oarInputId ?? null, requestId: null, acceptedSeq: null, observed: false });
      }
      if (typeof session.steer !== "function") {
        emit({
          type: "control",
          op: "steer",
          inputId: inputId ?? null,
          oarInputId: oarInputId ?? null,
          requestId: null,
          outcome: { kind: "rejected", code: "unsupported", reason: "Session does not support steer" },
        });
        return;
      }
      const outcome = await session.steer(msg.input, oarInputId ? { inputId: oarInputId } : undefined);
      if (inputId && trackedSteers.has(inputId)) {
        const entry = trackedSteers.get(inputId);
        entry.requestId = outcome.requestId ?? null;
        if (outcome.kind === "accepted" && entry.acceptedSeq === null) {
          entry.acceptedSeq = outcome.seq ?? null;
        }
      }
      emit({
        type: "control",
        op: "steer",
        inputId: inputId ?? null,
        oarInputId: oarInputId ?? null,
        requestId: outcome.requestId ?? null,
        outcome,
      });
      return;
    }

    if (msg.op === "abort") {
      const outcome = await session.abort();
      emit({
        type: "control",
        op: "abort",
        requestId: outcome.requestId ?? null,
        outcome,
      });
      return;
    }

    if (msg.op === "dispose") {
      if (!disposed) {
        disposed = true;
        await session.dispose();
        unsubscribe?.();
      }
      emitAndExit({
        type: "disposed",
        sessionId: session.id,
        sessionClosed: !unexpectedExit,
        processExited: true,
        exitedCode,
        arbitraryDetachedDescendantsProven: false,
      }, 0);
      return;
    }
  } catch (err) {
    const errPayload = {
      type: "error",
      stage: msg.op || "bridge",
      code: err?.code || err?.name || "error",
      message: err instanceof Error ? err.message : String(err),
    };
    if (msg.op === "open" || msg.op === "dispose") {
      process.exitCode = 1;
      emitAndExit(errPayload, 1);
      return;
    }
    emit(errPayload);
  }
}

rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  const trimmed = line.trim();
  if (!trimmed) { return; }
  let msg;
  try {
    msg = JSON.parse(trimmed);
  } catch (err) {
    fail("stdin", "invalid_json", err instanceof Error ? err.message : String(err));
    return;
  }
  void handleMessage(msg);
});
rl.on("close", () => {
  if (!disposed && session) {
    disposed = true;
    void session.dispose().finally(() => process.exit(0));
  }
});

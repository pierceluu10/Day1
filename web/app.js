// The live run, built only from the API: statuses and numbers from Atlas, notes and terminal from the event stream.

const STEP_TITLES = {
  t_base_tools: "Base tools installed",
  t_ssh_keygen: "SSH key generated",
  b_sign_in: "Signed in to Gitea",
  b_add_ssh_key: "SSH key added in browser",
  b_open_welcome: "Welcome issue opened",
  t_clone: "acme-api cloned",
  t_runtime: "Python runtime",
  t_system_package: "System packages",
  t_install_deps: "Dependencies installed",
  t_settings: "Settings file",
  t_init_db: "Database initialised",
  t_run_tests: "Tests pass",
  b_checklist: "Welcome checklist ticked",
};

const STATUS_WORDS = {
  not_started: "not started",
  pending: "pending",
  verified: "verified",
  skipped: "skipped, not re-run",
  fixed_from_memory: "fixed from memory",
  fixed_by_reasoning: "fixed by reasoning",
  stale: "stale",
  failed: "failed",
};

const SOURCE_WORDS = { exact_memory: "exact match", semantic_memory: "similar error", reasoning: "reasoning" };

// Events that change the ledger or the run; each one schedules a re-read of steps and the comparison.
const LEDGER_EVENTS = new Set([
  "run_started", "run_resumed", "run_finished", "run_failed",
  "step_pending", "step_status", "drift_detected", "fix_attempt", "error_captured",
]);

const DONE_STATUSES = new Set(["verified", "skipped", "fixed_from_memory", "fixed_by_reasoning"]);
// Notes that explain what is happening now; once the step is done its evidence line says enough.
const TRANSIENT_NOTES = new Set(["error", "locator", "locating", "moved"]);

const REFRESH_DEBOUNCE_MS = 200;
const RUNNING_REFRESH_MS = 1000;
const CHANGED_HIGHLIGHT_MS = 1400;
const TERMINAL_MAX_LINES = 800;

const pageLoadedAt = new Date();
const $ = (id) => document.getElementById(id);

const view = {
  hire: null,
  stepOrder: [],
  steps: new Map(), // step_id -> ledger doc
  notes: new Map(), // step_id -> { text, kind }
  checking: new Set(),
  reconciled: new Set(), // settled by its check this pass, nothing run; the ledger keeps the earlier status
  notices: [], // { text, kind }: resume, drift and README lines for this run, oldest first
  running: false,
  summary: null,
  source: null,
  refreshTimer: null,
  tickTimer: null,
  lastCommand: null,
};

async function getJSON(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail ?? `${response.status} ${response.statusText}`);
  return body;
}

function formatDuration(seconds) {
  const total = Math.round(seconds);
  const minutes = Math.floor(total / 60);
  const rest = String(total % 60).padStart(2, "0");
  return minutes ? `${minutes}m ${rest}s` : `${total}s`;
}

function formatTime(iso) {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

const recordedEarlier = (summary) => summary?.finished_at && new Date(summary.finished_at) < pageLoadedAt;

function plural(count, word) {
  return `${count} ${word}${count === 1 ? "" : "s"}`;
}

// --- rendering -------------------------------------------------------------------

function renderHires(hires) {
  const nav = $("hires");
  nav.replaceChildren(...hires.map((summary) => {
    const link = document.createElement("a");
    link.href = `#${summary.hire}`;
    link.textContent = summary.hire;
    if (summary.hire === view.hire) link.setAttribute("aria-current", "page");
    return link;
  }));
}

function stepRow(stepId) {
  const row = document.createElement("li");
  row.className = "step";
  row.dataset.step = stepId;
  const parts = ["step-title", "step-status", "step-line", "step-runs", "step-note"].map((className) => {
    const part = document.createElement("span");
    part.className = className;
    return part;
  });
  parts[0].textContent = STEP_TITLES[stepId] ?? stepId;
  row.append(...parts);
  return row;
}

function updateRow(row, highlight) {
  const stepId = row.dataset.step;
  const doc = view.steps.get(stepId) ?? { status: "not_started", run_count: 0 };
  const reconciled = view.reconciled.has(stepId) && DONE_STATUSES.has(doc.status);
  const shown = reconciled ? "skipped" : doc.status;
  if (highlight && row.dataset.status && row.dataset.status !== shown) {
    // The row is reused, so the wash fades out through the .step transition once the attribute goes.
    row.dataset.changed = "";
    clearTimeout(row.changedTimer);
    row.changedTimer = setTimeout(() => row.removeAttribute("data-changed"), CHANGED_HIGHLIGHT_MS);
  }
  row.dataset.status = shown;
  row.toggleAttribute("data-checking", view.checking.has(stepId));

  const [, status, line, runs, noteLine] = row.children;
  status.textContent = view.checking.has(stepId) ? "checking" : STATUS_WORDS[shown] ?? shown;
  line.textContent = doc.evidence || "";
  // Only a repeat is worth reading: every step should run at most once per hire.
  runs.textContent = doc.run_count > 1 ? `ran ${doc.run_count}×` : "";
  const note = view.notes.get(stepId);
  const showNote = note && !(DONE_STATUSES.has(doc.status) && TRANSIENT_NOTES.has(note.kind));
  noteLine.hidden = !showNote;
  noteLine.textContent = showNote ? note.text : "";
  if (showNote) noteLine.dataset.kind = note.kind;
}

// highlight: wash rows whose shown status changed (off while a run's history is being replayed).
function renderSteps(highlight) {
  const list = $("steps");
  if (list.childElementCount !== view.stepOrder.length) list.replaceChildren(...view.stepOrder.map(stepRow));
  for (const row of list.children) updateRow(row, highlight);
}

function renderRunHead() {
  const summary = view.summary;
  $("run-hire").textContent = summary?.machine ? `${view.hire} on ${summary.machine}` : view.hire;
  const state = $("run-state");
  const pending = [...view.steps.values()].find((doc) => doc.status === "pending");
  let text;
  let kind;
  if (summary === undefined) {
    [text, kind] = ["reading Atlas…", "idle"];
  } else if (!summary) {
    [text, kind] = ["no run yet", "idle"];
  } else if (summary.finished_at) {
    const outcome = summary.ok ? "finished" : "failed";
    text = `${outcome} in ${formatDuration(summary.elapsed_s)}`;
    kind = summary.ok ? "finished" : "failed";
  } else if (view.running) {
    [text, kind] = [`running, ${formatDuration(summary.elapsed_s)}`, "running"];
  } else {
    text = pending ? `stopped, ${STEP_TITLES[pending.step_id] ?? pending.step_id} pending` : "stopped";
    kind = "stopped";
  }
  state.textContent = text;
  state.dataset.state = kind;
  if (recordedEarlier(summary)) {
    const label = document.createElement("span");
    label.className = "recorded";
    label.textContent = `, recorded earlier today at ${formatTime(summary.finished_at)}`;
    state.append(label);
  }
  $("resume").hidden = !summary || Boolean(summary.finished_at) || view.running;

  $("notices").replaceChildren(...view.notices.map(({ text, kind }) => {
    const item = document.createElement("li");
    item.textContent = text;
    item.dataset.kind = kind;
    return item;
  }));
}

function comparisonLine(summary, label) {
  const line = document.createElement("p");
  const name = document.createElement("strong");
  name.textContent = label;
  const machine = summary.machine ? ` (${summary.machine})` : "";
  line.append(
    name,
    `${machine}: ${formatDuration(summary.elapsed_s)}, ${plural(summary.errors, "error")}, ` +
      `${summary.fixes_from_memory} from memory, ${plural(summary.llm_calls, "LLM call")}` +
      (recordedEarlier(summary) && label !== view.hire ? ", recorded earlier" : ""),
  );
  return line;
}

function renderCompare(comparison) {
  const section = $("compare");
  const lines = [];
  if (comparison.first) lines.push(comparisonLine(comparison.first, `${comparison.first.hire}, hire 1`));
  if (comparison.current && comparison.current.run_id !== comparison.first?.run_id) {
    lines.push(comparisonLine(comparison.current, comparison.current.hire));
  }
  section.replaceChildren(...lines);
}

function appendTerminal(command, chunk) {
  const pre = $("terminal");
  const atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
  pre.querySelector(".placeholder")?.remove();
  for (const text of chunk.split(/(?<=\n)/)) {
    if (!text) continue;
    const span = document.createElement("span");
    if (text.startsWith("$ ")) span.className = "command";
    span.textContent = text;
    pre.append(span);
  }
  while (pre.childElementCount > TERMINAL_MAX_LINES) pre.firstElementChild.remove();
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function resetTerminal() {
  const placeholder = document.createElement("span");
  placeholder.className = "placeholder";
  placeholder.textContent = "Commands the agent runs in the laptop appear here.";
  $("terminal").replaceChildren(placeholder);
}

// --- data -------------------------------------------------------------------------

// One request in flight per resource; a refresh asked for meanwhile runs once after it. Replies therefore
// arrive in order, and a slow comparison query never holds back the step list.
function coalesced(load) {
  let running = false;
  let again = false;
  return async function run() {
    if (running) {
      again = true;
      return;
    }
    running = true;
    try {
      await load(view.hire);
    } finally {
      running = false;
      if (again) {
        again = false;
        run().catch(showError);
      }
    }
  };
}

const refreshSteps = coalesced(async (hire) => {
  const steps = await getJSON(`/api/hires/${hire}/steps`);
  if (hire !== view.hire) return;
  const replayed = view.steps.size === 0;
  view.steps = new Map(steps.map((doc) => [doc.step_id, doc]));
  renderSteps(!replayed);
  renderRunHead();
});

const refreshCompare = coalesced(async (hire) => {
  const comparison = await getJSON(`/api/compare/${hire}`);
  if (hire !== view.hire) return;
  view.summary = comparison.current;
  renderRunHead();
  renderCompare(comparison);
});

function refresh() {
  return Promise.all([refreshSteps(), refreshCompare()]);
}

async function refreshHires() {
  renderHires(await getJSON("/api/hires"));
}

function scheduleRefresh() {
  clearTimeout(view.refreshTimer);
  view.refreshTimer = setTimeout(() => refresh().catch(showError), REFRESH_DEBOUNCE_MS);
}

function setNote(stepId, text, kind) {
  if (stepId) view.notes.set(stepId, { text, kind });
}

function memoryNote(data) {
  const parts = [`${SOURCE_WORDS[data.source] ?? data.source} from ${data.source_hire ?? "memory"}`];
  if (data.similarity != null) parts.push(`similarity ${Number(data.similarity).toFixed(2)}`);
  if (data.adapted_from) parts.push(`adapted from ${data.adapted_from}: ${data.commands.join(" && ")}`);
  return parts.join(", ");
}

// Applies one Atlas event to the notes, the checking set and the terminal. Statuses come from refresh().
function applyEvent(event) {
  const { type, step_id: stepId, data } = event;
  switch (type) {
    case "run_started":
      view.notes.clear();
      view.checking.clear();
      view.notices = [];
      view.reconciled.clear();
      resetTerminal();
      break;
    case "run_resumed":
      view.reconciled.clear();
      view.notices.push({ text: "Resumed: every finished step is re-checked before anything runs.", kind: "resume" });
      break;
    case "step_checking":
      view.checking.add(stepId);
      break;
    case "step_check_result":
      view.checking.delete(stepId);
      break;
    case "step_pending":
      view.notes.delete(stepId);
      break;
    case "step_status":
      if (data.reconciled) view.reconciled.add(stepId);
      else view.reconciled.delete(stepId);
      break;
    case "error_captured":
      setNote(stepId, data.error_text.split("\n")[0], "error");
      break;
    case "memory_hit":
      setNote(stepId, memoryNote(data), "memory");
      break;
    case "memory_miss":
    case "reasoning_started":
      setNote(stepId, "no fix in memory, reasoning", "reasoning");
      break;
    case "fix_attempt":
      if (data.source === "reasoning") {
        setNote(stepId, `reasoned fix: ${data.commands.join(" && ")}`, "reasoning");
      }
      break;
    case "experience_saved":
      if (view.notes.get(stepId)?.kind === "reasoning") {
        setNote(stepId, `${view.notes.get(stepId).text}; saved to memory`, "reasoning");
      }
      break;
    case "drift_detected":
      if (data.from && data.to) {
        view.notices.push({ text: `Remote HEAD moved ${data.from} → ${data.to}. Downstream steps re-verified.`, kind: "drift" });
      } else {
        setNote(data.step_id ?? stepId, `re-check failed: ${data.reason}`, "drift");
      }
      break;
    case "locator":
      if (data.mode === "memory") setNote(stepId, `${data.goal}: locator from memory`, "locator");
      else if (data.mode === "moved") setNote(stepId, `This moved. Saved locator for ${data.goal} failed`, "moved");
      else setNote(stepId, `${data.goal}: finding it by reasoning`, "locating");
      break;
    case "readme_issue_opened":
      view.notices.push({ text: `Opened a README issue: ${data.title ?? data.url}`, kind: "readme" });
      break;
    case "terminal_output":
      if (data.command !== view.lastCommand && !data.chunk.startsWith("$ ")) appendTerminal(data.command, `$ ${data.command}\n`);
      view.lastCommand = data.command;
      appendTerminal(data.command, data.chunk);
      return;
  }
  if (type.startsWith("run_")) refreshHires().catch(showError);
  if (LEDGER_EVENTS.has(type)) scheduleRefresh();
  else renderSteps(true);
  renderRunHead();
}

function watch(hire) {
  view.source?.close();
  clearInterval(view.tickTimer);
  Object.assign(view, {
    hire, steps: new Map(), notes: new Map(), checking: new Set(), reconciled: new Set(), notices: [], running: false, summary: undefined, lastCommand: null,
  });
  $("run").hidden = false;
  $("empty").hidden = true;
  resetTerminal();
  $("steps").replaceChildren(...view.stepOrder.map(stepRow));
  renderSteps(false);
  renderRunHead();

  // EventSource resends Last-Event-ID on reconnect, so a dropped stream resumes where it stopped.
  const source = new EventSource(`/api/hires/${hire}/events`);
  source.addEventListener("dayone", (message) => applyEvent(JSON.parse(message.data)));
  source.addEventListener("process", (message) => {
    view.running = JSON.parse(message.data).running;
    clearInterval(view.tickTimer);
    if (view.running) view.tickTimer = setInterval(() => refresh().catch(showError), RUNNING_REFRESH_MS);
    scheduleRefresh();
  });
  view.source = source;
  refresh().catch(showError);
  refreshHires().catch(showError);
}

function showError(error) {
  const box = $("form-error");
  box.textContent = error.message;
  box.hidden = false;
}

// --- wiring -----------------------------------------------------------------------

$("start").addEventListener("submit", async (submit) => {
  submit.preventDefault();
  const form = new FormData(submit.target);
  const button = submit.target.querySelector("button");
  $("form-error").hidden = true;
  button.disabled = true;
  try {
    const run = await getJSON("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hire: form.get("hire").trim().toLowerCase(), machine: form.get("machine") }),
    });
    location.hash = run.hire;
  } catch (error) {
    showError(error);
  } finally {
    button.disabled = false;
  }
});

$("resume").addEventListener("click", async () => {
  $("form-error").hidden = true;
  try {
    await getJSON(`/api/runs/${view.hire}/resume`, { method: "POST" });
  } catch (error) {
    showError(error);
  }
});

window.addEventListener("hashchange", () => watch(location.hash.slice(1)));

async function boot() {
  const config = await getJSON("/api/config");
  view.stepOrder = config.steps;
  $("hire-names").replaceChildren(...config.hires.map((name) => Object.assign(document.createElement("option"), { value: name })));
  const initial = location.hash.slice(1) || (await getJSON("/api/hires")).at(-1)?.hire;
  if (initial) watch(initial);
  else $("empty").textContent = "No runs in Atlas yet. Type a hire's name, pick their laptop and press Start.";
}

boot().catch(showError);

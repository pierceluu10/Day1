// Read-only view of one hire's laptop, made for a phone: every command the agent runs in the container and its
// output, from the same Atlas event stream as the main page. Follows whichever hire is running unless ?hire= is set.

import { STEP_TITLES } from "./steps.js";

const FOLLOW_INTERVAL_MS = 2000;
const MAX_LINES = 1500;
const BOTTOM_SLACK_PX = 32;
const SOURCE_WORDS = { exact_memory: "exact match", semantic_memory: "similar error", reasoning: "reasoning" };
const ERROR_LINE = /\b(error|errno|fatal|failed|not found)\b/i;
const PINNED_HIRE = new URLSearchParams(location.search).get("hire");

const screen = document.getElementById("screen");
const view = { hire: null, source: null, command: null, startedAt: null, cursor: document.createElement("i") };
view.cursor.className = "cursor";

function host() {
  return `root@dayone-${view.hire}:~#`;
}

function write(text, className) {
  const atBottom = screen.scrollHeight - screen.scrollTop - screen.clientHeight < BOTTOM_SLACK_PX;
  for (const line of text.replace(/\n$/, "").split("\n")) {
    const span = document.createElement("span");
    span.className = className ?? (ERROR_LINE.test(line) ? "error" : "");
    span.textContent = line || " ";
    screen.insertBefore(span, view.cursor);
  }
  while (screen.childElementCount > MAX_LINES) screen.firstElementChild.remove();
  if (atBottom) screen.scrollTop = screen.scrollHeight;
}

function writePrompt(command) {
  const span = document.createElement("span");
  span.className = "prompt";
  const typed = document.createElement("b");
  typed.textContent = command;
  span.append(`${host()} `, typed);
  screen.insertBefore(span, view.cursor);
}

function setState(running) {
  const state = document.getElementById("state");
  state.textContent = running ? "live" : "idle";
  state.classList.toggle("live", running);
  view.cursor.hidden = !running;
}

function elapsed(ts) {
  const seconds = Math.round((new Date(ts) - new Date(view.startedAt)) / 1000);
  return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
}

function title(stepId) {
  return STEP_TITLES[stepId] ?? stepId;
}

function memoryLine(data) {
  const parts = [`${SOURCE_WORDS[data.source] ?? data.source} from ${data.source_hire ?? "memory"}`];
  if (data.similarity != null) parts.push(`similarity ${Number(data.similarity).toFixed(2)}`);
  if (data.adapted_from) parts.push(`adapted from ${data.adapted_from}`);
  return `# memory (Atlas): ${parts.join(", ")} → ${data.commands.join(" && ")}`;
}

function apply({ type, step_id: stepId, ts, data }) {
  switch (type) {
    case "run_started":
      view.startedAt = ts;
      write(`# Day One started onboarding ${view.hire}`, "note");
      break;
    case "run_resumed":
      view.startedAt ??= ts;
      write("# resumed: re-checking every finished step before running anything", "reasoning");
      break;
    case "step_pending":
      write(`── ${title(stepId)} ──`, "heading");
      break;
    case "terminal_output":
      // A step that raised reports its traceback under a "<step>.<phase>()" pseudo-command.
      if (data.command !== view.command && !data.command.endsWith("()")) writePrompt(data.command);
      view.command = data.command;
      write(data.chunk);
      break;
    case "memory_hit":
      write(memoryLine(data), "memory");
      break;
    case "memory_miss":
      write("# no fix in memory: reasoning from the error", "reasoning");
      break;
    case "fix_attempt":
      if (data.source === "reasoning") write(`# reasoned fix → ${data.commands.join(" && ")}`, "reasoning");
      break;
    case "experience_saved":
      write("# fix verified and saved to Atlas memory", "memory");
      break;
    case "step_status":
      if (data.reconciled) write(`✓ ${title(stepId)}: already done, not re-run`, "note");
      else if (data.status === "failed") write(`✗ ${title(stepId)}: ${data.evidence}`, "error");
      else write(`✓ ${title(stepId)}: ${data.evidence}`, "done");
      break;
    case "drift_detected":
      write(data.from ? `# remote HEAD moved ${data.from} → ${data.to}: re-verifying` : `# drift: ${data.reason}`, "error");
      break;
    case "readme_issue_opened":
      write(`# opened README issue: ${data.title ?? data.url}`, "memory");
      break;
    case "run_finished":
      write(`# onboarding complete in ${elapsed(ts)}`, "done");
      break;
    case "run_failed":
      write(`# run failed after ${elapsed(ts)}`, "error");
      break;
  }
}

function watch(summary) {
  view.source?.close();
  Object.assign(view, { hire: summary.hire, command: null, startedAt: null });
  document.title = `${summary.hire}'s laptop`;
  document.getElementById("title").textContent = `${host()} (${summary.machine ?? "laptop"})`;
  screen.replaceChildren(view.cursor);
  // EventSource resends Last-Event-ID on reconnect, so a dropped stream resumes where it stopped.
  view.source = new EventSource(`/api/hires/${summary.hire}/events`);
  view.source.addEventListener("dayone", (message) => apply(JSON.parse(message.data)));
  view.source.addEventListener("process", (message) => setState(JSON.parse(message.data).running));
}

// The hire to show: the pinned one, else the one running now, else the one that started last.
function pick(hires) {
  if (PINNED_HIRE) return hires.find((summary) => summary.hire === PINNED_HIRE) ?? { hire: PINNED_HIRE };
  return hires.findLast((summary) => summary.running) ?? hires.at(-1);
}

async function follow() {
  try {
    const response = await fetch("/api/hires");
    if (!response.ok) throw new Error(`/api/hires answered ${response.status}`);
    const next = pick(await response.json());
    if (next && next.hire !== view.hire) watch(next);
  } catch (error) {
    document.getElementById("state").textContent = "offline";
    console.error(error);
  }
}

setState(false);
follow();
setInterval(follow, FOLLOW_INTERVAL_MS);

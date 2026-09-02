"use strict";
/* TrajWeave UI - vanilla SPA over the read-only JSON API.
   No build step, no dependencies. Hash routing. */

const PAGE = 50;
let META = null;

/* ---------- tiny helpers ---------------------------------------- */
function el(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2), v);
      } else node.setAttribute(k, v === true ? "" : String(v));
    }
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    node.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

async function api(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  let body = null;
  try {
    body = await res.json();
  } catch (_) {
    /* fall through */
  }
  if (!res.ok) {
    const msg = (body && body.error) || `${res.status} ${res.statusText}`;
    throw new Error(msg);
  }
  return body;
}

function esc(s) {
  return String(s == null ? "" : s);
}

function fmtDate(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  return d.toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

function fmtClock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  return d.toISOString().slice(11, 19);
}

function fmtDuration(sec) {
  if (sec == null) return "-";
  sec = Math.round(sec);
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor((sec % 3600) / 60)}m`;
}

function relTime(iso) {
  if (!iso) return "never";
  const then = new Date(iso).getTime();
  if (isNaN(then)) return esc(iso);
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} hours ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function statusPill(status) {
  const s = (status || "unknown").toLowerCase();
  return el("span", { class: `pill st-${s}`, text: s.toUpperCase() });
}

function truncate(s, n) {
  s = esc(s);
  return s.length > n ? s.slice(0, n) + "…" : s;
}

/* ---------- event taxonomy ------------------------------------- */
const EVENT_META = {
  user_prompt: ["Prompt", "cat-prompt"],
  assistant_message: ["Assistant", "cat-assistant"],
  file_read: ["File read", "cat-file"],
  file_create: ["File created", "cat-file"],
  file_edit: ["File edited", "cat-file"],
  file_delete: ["File deleted", "cat-file"],
  command: ["Command", "cat-command"],
  tool_call: ["Tool call", "cat-tool"],
  test_run: ["Test run", "cat-command"],
  test_pass: ["Test passed", "cat-pass"],
  test_fail: ["Test failed", "cat-fail"],
  lint_run: ["Lint run", "cat-command"],
  lint_pass: ["Lint passed", "cat-pass"],
  lint_fail: ["Lint failed", "cat-fail"],
  build_run: ["Build run", "cat-command"],
  build_pass: ["Build passed", "cat-pass"],
  build_fail: ["Build failed", "cat-fail"],
  error: ["Error", "cat-fail"],
  retry: ["Retry", "cat-correction"],
  human_correction: ["Human correction", "cat-correction"],
  completion: ["Completion", "cat-done"],
  unknown: ["Unknown", "cat-unknown"],
};
function eventMeta(type) {
  return EVENT_META[type] || [type, "cat-unknown"];
}
const FAIL_TYPES = new Set([
  "test_fail",
  "lint_fail",
  "build_fail",
  "error",
  "retry",
  "human_correction",
]);
function isErrorEvent(e) {
  if (FAIL_TYPES.has(e.type)) return true;
  return e.type === "command" && e.exit_code != null && e.exit_code !== 0;
}

/* ---------- routing ------------------------------------------- */
function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, qs] = raw.split("?");
  return { path: path || "/", params: new URLSearchParams(qs || "") };
}

function navSetActive(name) {
  document.querySelectorAll(".nav a").forEach((a) => {
    a.classList.toggle("active", a.dataset.nav === name);
  });
}

async function render() {
  const { path, params } = parseHash();
  const view = document.getElementById("view");
  view.replaceChildren(el("p", { class: "muted", text: "Loading…" }));

  try {
    if (path === "/" || path === "/projects") {
      navSetActive("projects");
      await viewProjects(view);
    } else if (path === "/sessions") {
      navSetActive("sessions");
      await viewSessions(view, params);
    } else if (path.startsWith("/session/")) {
      navSetActive("sessions");
      await viewSession(view, decodeURIComponent(path.slice("/session/".length)));
    } else if (path === "/debug") {
      navSetActive(null);
      await viewDebug(view, params);
    } else {
      view.replaceChildren(el("div", { class: "empty" }, "Page not found."));
    }
  } catch (err) {
    view.replaceChildren(
      el("div", { class: "banner", role: "alert" }, `Could not load: ${err.message}`)
    );
  }
}

/* ---------- projects ---------------------------------------- */
async function viewProjects(view) {
  const data = await api("/api/projects");
  const projects = data.projects || [];
  const frag = [el("div", { class: "page-head" }, el("h1", { text: "Projects" }))];

  if (!projects.length) {
    frag.push(
      el(
        "div",
        { class: "empty" },
        el("p", { text: "No TrajWeave projects found." }),
        el("p", {}, "Run ", el("code", { text: "trajweave init" }), " inside a repository, then "),
        el("p", {}, el("code", { text: "trajweave import --all" }))
      )
    );
    view.replaceChildren(...frag);
    return;
  }

  const cards = el("div", { class: "cards" });
  for (const p of projects) {
    const sub = `${p.total_sessions} session${p.total_sessions === 1 ? "" : "s"}` +
      ` · Codex ${p.codex_count} · Claude ${p.claude_count}`;
    const card = el(
      "a",
      { class: "card", href: `#/sessions?project=${encodeURIComponent(p.id)}` },
      el(
        "div",
        { class: "card-title" },
        p.name,
        p.status && p.status !== "active"
          ? el("span", { class: "tag", text: p.status })
          : null
      ),
      el("div", { class: "card-sub", text: sub }),
      el("div", {
        class: "card-sub",
        text: `Last activity: ${p.last_activity ? relTime(p.last_activity) : "no imported sessions"}`,
      }),
      el("div", { class: "card-path mono", text: p.root })
    );
    cards.append(card);
  }
  frag.push(cards);
  view.replaceChildren(...frag);
}

/* ---------- sessions list --------------------------------- */
async function viewSessions(view, params) {
  const qp = new URLSearchParams();
  ["project", "agent", "status", "q"].forEach((k) => {
    if (params.get(k)) qp.set(k, params.get(k));
  });
  const offset = Math.max(0, parseInt(params.get("offset") || "0", 10) || 0);
  qp.set("limit", String(PAGE));
  qp.set("offset", String(offset));

  const data = await api("/api/sessions?" + qp.toString());
  const rows = data.sessions || [];
  const total = data.total || 0;

  const filters = META && META.filters ? META.filters : { agents: [], statuses: [] };

  const setParam = (k, v) => {
    const next = new URLSearchParams(params.toString());
    if (v) next.set(k, v);
    else next.delete(k);
    next.delete("offset");
    location.hash = "#/sessions?" + next.toString();
  };

  const agentSel = el(
    "select",
    { "aria-label": "Filter by agent", onchange: (e) => setParam("agent", e.target.value) },
    el("option", { value: "", text: "All agents" }),
    ...filters.agents.map((a) =>
      el("option", { value: a, text: a, selected: params.get("agent") === a })
    )
  );
  const statusSel = el(
    "select",
    { "aria-label": "Filter by status", onchange: (e) => setParam("status", e.target.value) },
    el("option", { value: "", text: "All statuses" }),
    ...filters.statuses.map((s) =>
      el("option", { value: s, text: s, selected: params.get("status") === s })
    )
  );
  const search = el("input", {
    type: "search",
    placeholder: "Search task text or TW-id",
    value: params.get("q") || "",
    "aria-label": "Search sessions",
  });
  let t;
  search.addEventListener("input", (e) => {
    clearTimeout(t);
    const v = e.target.value.trim();
    t = setTimeout(() => setParam("q", v), 300);
  });

  const frag = [
    el("div", { class: "page-head" }, el("h1", { text: "Sessions" })),
  ];

  if (params.get("project")) {
    const projName = rows.length ? rows[0].project_name : null;
    frag.push(
      el(
        "p",
        { class: "crumbs" },
        el("a", { href: "#/" }, "Projects"),
        " / ",
        projName || params.get("project"),
        "  ",
        el("a", { href: "#/sessions", class: "muted" }, "(clear filter)")
      )
    );
  }

  frag.push(el("div", { class: "filters" }, agentSel, statusSel, search));

  if (!rows.length) {
    frag.push(
      el(
        "div",
        { class: "empty" },
        total === 0 && !hasAnyFilter(params)
          ? el(
              "div",
              {},
              el("p", { text: "No sessions found." }),
              el("p", {}, "Run ", el("code", { text: "trajweave import --all" }))
            )
          : el("p", { text: "No sessions match these filters." })
      )
    );
    view.replaceChildren(...frag);
    return;
  }

  const tbody = el("tbody");
  for (const s of rows) {
    tbody.append(
      el(
        "tr",
        {},
        el("td", { class: "id-cell" }, el("a", { href: `#/session/${s.id}` }, s.id)),
        el(
          "td",
          {},
          s.project_name || el("span", { class: "muted", text: "-" })
        ),
        el(
          "td",
          {},
          s.agent,
          s.is_subagent ? el("span", { class: "tag", text: "subagent" }) : null
        ),
        el("td", { class: "task", text: truncate(s.task || "(no task extracted)", 120) }),
        el("td", {}, statusPill(s.final_status)),
        el("td", { class: "mono", text: fmtDate(s.started_at) }),
        el("td", { text: fmtDuration(s.duration_seconds) })
      )
    );
  }

  const table = el(
    "table",
    { class: "tbl" },
    el(
      "thead",
      {},
      el(
        "tr",
        {},
        ...["Session", "Project", "Agent", "Task", "Status", "Started (UTC)", "Duration"].map(
          (h) => el("th", { text: h })
        )
      )
    ),
    tbody
  );
  frag.push(table);

  const shown = `${offset + 1}–${offset + rows.length} of ${total}`;
  const prev = el("button", {
    text: "← Prev",
    disabled: offset === 0,
    onclick: () => gotoOffset(params, Math.max(0, offset - PAGE)),
  });
  const next = el("button", {
    text: "Next →",
    disabled: offset + rows.length >= total,
    onclick: () => gotoOffset(params, offset + PAGE),
  });
  frag.push(el("div", { class: "pager" }, prev, next, el("span", { class: "muted", text: shown })));

  view.replaceChildren(...frag);
}

function hasAnyFilter(params) {
  return ["project", "agent", "status", "q"].some((k) => params.get(k));
}
function gotoOffset(params, offset) {
  const next = new URLSearchParams(params.toString());
  next.set("offset", String(offset));
  location.hash = "#/sessions?" + next.toString();
}

/* ---------- session detail ------------------------------- */
async function viewSession(view, id) {
  const data = await api("/api/trajectories/" + encodeURIComponent(id));
  const t = data.trajectory;
  const events = data.events || [];
  const files = data.files || [];

  const head = el(
    "div",
    { class: "detail-head" },
    el(
      "div",
      {},
      el("span", { class: "id-cell mono", text: t.id }),
      t.is_subagent ? el("span", { class: "tag", text: "subagent" }) : null,
      "  ",
      statusPill(t.final_status)
    ),
    el("div", { class: "dh-title", text: t.task || "(no task extracted)" }),
    el(
      "dl",
      { class: "kv" },
      kv("Project", t.project_name || "-"),
      kv("Agent", t.agent),
      kv("Model", t.model || "-"),
      kv("Started", fmtDate(t.started_at)),
      kv("Ended", fmtDate(t.ended_at)),
      kv("Duration", fmtDuration(t.duration_seconds)),
      kv("Events", String(t.event_count)),
      t.final_status_reason ? kv("Status basis", t.final_status_reason) : null,
      kv("Task source", t.task_source || "none")
    )
  );

  const errorEvents = events.filter(isErrorEvent);
  const commandEvents = events.filter((e) => e.type === "command");

  const tabs = [
    ["Timeline", () => renderTimeline(events)],
    ["Files", () => renderFiles(files)],
    ["Commands", () => renderCommands(commandEvents)],
    ["Errors", () => renderErrors(errorEvents)],
    ["Raw Metadata", () => renderMetadata(t)],
  ];
  const counts = [null, files.length, commandEvents.length, errorEvents.length, null];

  const panel = el("div", { id: "tabpanel" });
  const tabBar = el("div", { class: "tabs", role: "tablist" });
  tabs.forEach(([label, build], i) => {
    const btn = el(
      "button",
      {
        role: "tab",
        onclick: () => {
          tabBar.querySelectorAll("button").forEach((b) => b.classList.remove("active"));
          btn.classList.add("active");
          panel.replaceChildren(build());
        },
      },
      label,
      counts[i] != null ? el("span", { class: "count", text: ` (${counts[i]})` }) : null
    );
    if (i === 0) btn.classList.add("active");
    tabBar.append(btn);
  });
  panel.replaceChildren(tabs[0][1]());

  view.replaceChildren(
    el("p", { class: "crumbs" }, el("a", { href: "#/sessions" }, "Sessions"), " / ", t.id),
    head,
    tabBar,
    panel
  );
}

function kv(k, v) {
  return el("div", {}, el("dt", { text: k }), el("dd", { text: v }));
}

/* ---- timeline ---- */
function renderTimeline(events) {
  if (!events.length) return el("div", { class: "empty", text: "This session has no recorded events." });

  const repairAt = detectRepairs(events);
  const wrap = el("div", { class: "timeline" });

  events.forEach((e, idx) => {
    if (repairAt.has(idx)) wrap.append(el("div", { class: "repair-note", text: repairAt.get(idx) }));
    wrap.append(timelineRow(e));
  });
  return wrap;
}

function timelineRow(e) {
  const [label, cat] = eventMeta(e.type);
  const body = el("div", { class: "tl-body" });

  if (e.path) body.append(el("div", { class: "path", text: e.path }));
  if (e.command) body.append(el("div", { class: "cmd", text: "$ " + e.command }));
  if (e.tool_name && !e.path && !e.command)
    body.append(el("div", { class: "mono", text: e.tool_name }));
  if (e.summary && e.summary !== e.command) {
    const out = el("div", { class: "tl-output", text: e.summary });
    body.append(out);
    if (e.summary.length > 240) {
      const more = el("button", {
        class: "tl-more",
        text: "show more",
        onclick: () => {
          const open = out.classList.toggle("expanded");
          more.textContent = open ? "show less" : "show more";
        },
      });
      body.append(more);
    }
  }

  const kindBits = [];
  const meta = e.metadata || {};
  const kind = meta.command_kind || meta.kind;
  if (kind && e.type === "command") kindBits.push(kind);
  if (meta.lines_added != null || meta.lines_removed != null)
    kindBits.push(`+${meta.lines_added || 0}/-${meta.lines_removed || 0}`);
  if (e.metadata_error) kindBits.push("unparseable metadata");
  if (kindBits.length) body.append(el("div", { class: "tl-meta", text: kindBits.join(" · ") }));

  const headBits = [el("span", { class: "tl-kind", text: label })];
  if (e.exit_code != null)
    headBits.push(
      el("span", {
        class: "pill " + (e.exit_code === 0 ? "st-success" : "st-failure"),
        text: `exit ${e.exit_code}`,
      })
    );
  if (e.redacted) headBits.push(el("span", { class: "tag agent", text: "redacted" }));

  return el(
    "div",
    { class: "tl-row" },
    el("div", { class: "tl-time", text: fmtClock(e.timestamp) }),
    el("div", { class: "tl-rail" }, el("div", { class: "tl-dot" })),
    el("div", { class: `tl-card ${cat}` }, el("div", { class: "tl-head" }, ...headBits), body)
  );
}

function detectRepairs(events) {
  const families = { test_fail: "test_pass", lint_fail: "lint_pass", build_fail: "build_pass" };
  const notes = new Map();
  events.forEach((e, i) => {
    const passType = families[e.type];
    if (!passType) return;
    for (let j = i + 1; j < events.length; j++) {
      if (events[j].type === passType) {
        let edits = 0;
        for (let k = i + 1; k < j; k++) {
          if (["file_edit", "file_create", "file_delete"].includes(events[k].type)) edits++;
        }
        const fam = e.type.split("_")[0];
        notes.set(
          j,
          `Repair sequence: ${fam} failed at step ${e.sequence}, passed at step ${events[j].sequence}` +
            (edits ? ` after ${edits} file change${edits === 1 ? "" : "s"}` : "") +
            " (ordering only, not a proven cause)."
        );
        break;
      }
    }
  });
  return notes;
}

/* ---- files / commands / errors / metadata ---- */
function renderFiles(files) {
  if (!files.length) return el("div", { class: "empty", text: "No files were touched." });
  const list = el("div", { class: "files-list" });
  for (const f of files) {
    const flags =
      (f.was_read ? "r" : "-") +
      (f.was_created ? "c" : "-") +
      (f.was_modified ? "m" : "-") +
      (f.was_deleted ? "d" : "-");
    list.append(
      el("div", {}, el("span", { class: "flag", text: `[${flags}]` }), el("span", { class: "mono", text: f.path }))
    );
  }
  return el("div", {}, el("p", { class: "muted", text: "flags: r read · c created · m modified · d deleted" }), list);
}

function renderCommands(cmds) {
  if (!cmds.length) return el("div", { class: "empty", text: "No shell commands were run." });
  const tbody = el("tbody");
  for (const c of cmds) {
    const meta = c.metadata || {};
    tbody.append(
      el(
        "tr",
        {},
        el("td", { class: "mono", text: fmtClock(c.timestamp) }),
        el("td", { class: "mono", text: truncate(c.command || "", 160) }),
        el(
          "td",
          {},
          c.exit_code == null
            ? el("span", { class: "muted", text: "-" })
            : el("span", {
                class: "pill " + (c.exit_code === 0 ? "st-success" : "st-failure"),
                text: String(c.exit_code),
              })
        ),
        el("td", { text: meta.command_kind || meta.kind || "other" })
      )
    );
  }
  return el(
    "table",
    { class: "tbl" },
    el("thead", {}, el("tr", {}, ...["Time", "Command", "Exit", "Kind"].map((h) => el("th", { text: h })))),
    tbody
  );
}

function renderErrors(errs) {
  if (!errs.length)
    return el("div", { class: "empty", text: "No failures, errors, retries or corrections recorded." });
  const wrap = el("div", { class: "timeline" });
  errs.forEach((e) => wrap.append(timelineRow(e)));
  return wrap;
}

function renderMetadata(t) {
  const scalars = [
    ["Trajectory id", t.id],
    ["Source session id", t.source_session_id],
    ["Source path", t.source_path],
    ["Recorded cwd", t.source_cwd],
    ["Repository root", t.repository_root],
    ["Git branch", t.git_branch],
    ["Git commit", t.git_commit],
    ["Git remote", t.git_remote],
    ["Model", t.model],
    ["CLI version", t.cli_version],
    ["Task source", t.task_source],
    ["Final status", t.final_status],
    ["Status basis", t.final_status_reason],
    ["Created (import)", t.created_at],
  ];
  const dl = el("dl", { class: "kv" });
  for (const [k, v] of scalars) if (v != null && v !== "") dl.append(kv(k, String(v)));

  const blocks = [el("h2", { text: "Fields" }), dl];
  if (t.token_usage) {
    blocks.push(el("h2", { text: "Token usage" }));
    blocks.push(el("pre", { class: "raw", text: JSON.stringify(t.token_usage, null, 2) }));
  }
  if (t.parse_warnings && t.parse_warnings.length) {
    blocks.push(el("h2", { text: `Parse warnings (${t.parse_warnings.length})` }));
    blocks.push(el("pre", { class: "raw", text: t.parse_warnings.join("\n") }));
  }
  return el("div", {}, ...blocks);
}

/* ---------- import ledger (debug) ----------------------- */
async function viewDebug(view, params) {
  const data = await api("/api/debug/sessions");
  const rows = data.sessions || [];
  const frag = [
    el("p", { class: "crumbs" }, el("a", { href: "#/" }, "Projects"), " / Import ledger"),
    el("div", { class: "page-head" }, el("h1", { text: "Import ledger" })),
    el("p", {
      class: "muted",
      text:
        "Every discovered Codex/Claude session file and why it is (or is not) in the dataset. Debugging view - shows absolute paths.",
    }),
  ];
  if (!rows.length) {
    frag.push(el("div", { class: "empty", text: "No source sessions discovered yet." }));
    view.replaceChildren(...frag);
    return;
  }
  const tbody = el("tbody");
  for (const r of rows) {
    tbody.append(
      el(
        "tr",
        {},
        el("td", { text: r.agent }),
        el("td", {}, el("span", { class: "pill st-" + ledgerClass(r.status), text: r.status })),
        el("td", { class: "mono", text: truncate(r.source_path || "", 90) }),
        el("td", { text: truncate(r.detail || "", 80) })
      )
    );
  }
  frag.push(
    el(
      "table",
      { class: "tbl" },
      el("thead", {}, el("tr", {}, ...["Agent", "Status", "Source path", "Detail"].map((h) => el("th", { text: h })))),
      tbody
    )
  );
  view.replaceChildren(...frag);
}
function ledgerClass(status) {
  if (status === "imported") return "success";
  if (status === "failed") return "failure";
  return "unknown";
}

/* ---------- boot -------------------------------------- */
async function boot() {
  try {
    META = await api("/api/meta");
  } catch (_) {
    META = { filters: { agents: [], statuses: [] } };
  }
  const line = document.getElementById("meta-line");
  if (META && META.db_exists) {
    const c = META.counts || {};
    line.textContent =
      `${c.projects || 0} projects · ${c.trajectories || 0} trajectories · ` +
      `${c.events || 0} events · db ${META.db_path}`;
    if (META.schema_ok === false) {
      line.textContent = `database schema not readable · ${META.db_path}`;
    }
  } else {
    line.textContent = META ? `no database yet · ${META.db_path || ""}` : "";
  }
  window.addEventListener("hashchange", render);
  render();
}

boot();

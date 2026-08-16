/* Rain Bird Scheduler panel (plan §31).
 *
 * Dependency-free ES module: no build step, no external imports (works under
 * Home Assistant's CSP). The panel always distinguishes requested start,
 * planned start, base runtime, adjusted exact runtime, and the controller-
 * quantized runtime, and labels the native rain delay in DAYS.
 */

const DOMAIN = "rainbird_scheduler";

const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (ch) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        ch
      ],
  );

const fmtDt = (iso) => {
  if (!iso) return "—";
  const date = new Date(iso);
  return date.toLocaleString([], {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
};

const fmtTime = (iso) => {
  if (!iso) return "—";
  return new Date(iso).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
};

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const RECURRENCE_KINDS = [
  ["weekly", "Selected weekdays"],
  ["odd_days", "Odd calendar days"],
  ["even_days", "Even calendar days"],
  ["interval", "Every N days"],
];

const PROVIDER_KINDS = [
  ["fixed", "Fixed 100%"],
  ["manual_percent", "Manual percentage"],
  ["monthly_curve", "Monthly seasonal curve"],
  ["entity_percent", "External percentage entity"],
  ["entity_runtime", "External runtime entity"],
];

const SENSOR_CUT = [
  ["abort_run", "Abort the run"],
  ["pause_until_dry", "Pause until dry"],
  ["defer_remaining", "Defer remaining zones"],
];

const WINDOW_POLICIES = [
  ["skip_step", "Skip steps outside the window"],
  ["truncate_last", "Truncate the last step"],
  ["defer_occurrence", "Defer the whole occurrence"],
  ["require_intervention", "Mark conflict, require intervention"],
];

const SOILS = ["unknown", "clay", "loam", "sand"];
const SLOPES = ["flat", "moderate", "steep"];
const MIN_RUNTIME = [
  ["", "Controller default"],
  ["skip_with_warning", "Skip with warning"],
  ["clamp_to_one_minute", "Clamp to one minute"],
  ["carry_forward", "Carry forward"],
];

function recurrenceSummary(program) {
  const rec = program.recurrence || {};
  let base;
  if (rec.kind === "weekly") {
    const days = (rec.weekdays || []).slice().sort();
    base =
      days.length === 7
        ? "Every day"
        : days.map((day) => WEEKDAYS[day]).join(", ") || "No days selected";
  } else if (rec.kind === "odd_days") base = "Odd days";
  else if (rec.kind === "even_days") base = "Even days";
  else base = `Every ${rec.interval_days ?? "?"} days`;
  const starts = (program.nominal_start_times || [])
    .map((time) => time.slice(0, 5))
    .join(", ");
  return `${base} at ${starts || "—"}`;
}

class RainBirdSchedulerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._initialized = false;
    this._tab = "overview";
    this._entries = [];
    this._entryId = null;
    this._config = null;
    this._state = null;
    this._timeline = null;
    this._history = null;
    this._diagnostics = null;
    this._draft = null;
    this._draftIsNew = false;
    this._selectedRun = null;
    this._toast = "";
    this._unsub = null;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._initialized && hass) {
      this._initialized = true;
      this._init();
    }
  }

  get hass() {
    return this._hass;
  }

  disconnectedCallback() {
    if (this._unsub) {
      this._unsub.then((unsub) => unsub()).catch(() => {});
      this._unsub = null;
    }
  }

  api(msg) {
    return this._hass.callWS(msg);
  }

  async _init() {
    try {
      this._entries = await this.api({ type: `${DOMAIN}/entries` });
      if (!this._entries.length) {
        this._renderEmpty();
        return;
      }
      this._entryId = this._entries[0].entry_id;
      await this._loadAll();
      this._subscribe();
    } catch (err) {
      this._renderError(err);
    }
  }

  _subscribe() {
    if (this._unsub) this._unsub.then((unsub) => unsub()).catch(() => {});
    this._unsub = this._hass.connection.subscribeMessage(
      (event) => this._onPush(event),
      { type: `${DOMAIN}/subscribe`, entry_id: this._entryId },
    );
  }

  async _onPush(event) {
    if (event.kind === "state") {
      this._state = event.state;
      this._refreshPreviewSoon();
      this.render();
    } else if (event.kind === "config") {
      await this._loadConfig();
      this.render();
    } else if (event.kind === "lifecycle") {
      this._toastMsg(`${event.event}${event.data?.zone_name ? ": " + event.data.zone_name : ""}`);
    }
  }

  _refreshPreviewSoon() {
    clearTimeout(this._previewTimer);
    this._previewTimer = setTimeout(async () => {
      try {
        const result = await this.api({
          type: `${DOMAIN}/plan/preview`,
          entry_id: this._entryId,
        });
        this._timeline = result.timeline;
        this._state = result.state;
        this.render();
      } catch (err) {
        /* transient */
      }
    }, 400);
  }

  async _loadConfig() {
    this._config = await this.api({
      type: `${DOMAIN}/config/get`,
      entry_id: this._entryId,
    });
  }

  async _loadAll() {
    await this._loadConfig();
    const preview = await this.api({
      type: `${DOMAIN}/plan/preview`,
      entry_id: this._entryId,
    });
    this._timeline = preview.timeline;
    this._state = preview.state;
    this.render();
  }

  async _loadHistory() {
    this._history = await this.api({
      type: `${DOMAIN}/history/list`,
      entry_id: this._entryId,
      limit: 100,
    });
  }

  async _loadDiagnostics() {
    this._diagnostics = await this.api({
      type: `${DOMAIN}/diagnostics/get`,
      entry_id: this._entryId,
    });
  }

  _toastMsg(message) {
    this._toast = message;
    this.render();
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => {
      this._toast = "";
      this.render();
    }, 4000);
  }

  async _action(promise, okMessage) {
    try {
      await promise;
      if (okMessage) this._toastMsg(okMessage);
    } catch (err) {
      if (err && err.code === "revision_conflict") {
        await this._loadConfig();
        this._toastMsg("Changed elsewhere — reloaded the latest version.");
        this.render();
      } else {
        this._toastMsg(`Error: ${err.message || err.code || err}`);
      }
    }
  }

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------

  _styles() {
    return `
      :host { display:block; height:100%; overflow:auto;
        background: var(--primary-background-color, #fafafa);
        color: var(--primary-text-color, #212121);
        font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
      .topbar { display:flex; align-items:center; gap:16px; padding:12px 20px;
        background: var(--app-header-background-color, #03a9f4);
        color: var(--app-header-text-color, #fff); position:sticky; top:0; z-index:5;}
      .topbar h1 { font-size:20px; margin:0; font-weight:400;}
      .tabs { display:flex; gap:4px; padding:0 12px; flex-wrap:wrap;
        background: var(--card-background-color, #fff);
        border-bottom:1px solid var(--divider-color, #e0e0e0);
        position:sticky; top:52px; z-index:4;}
      .tabs button { border:none; background:none; padding:12px 16px; cursor:pointer;
        font-size:14px; color: var(--secondary-text-color,#666);
        border-bottom:2px solid transparent; text-transform:uppercase;}
      .tabs button.active { color: var(--primary-color,#03a9f4);
        border-bottom-color: var(--primary-color,#03a9f4);}
      main { padding:16px 20px 60px; max-width:1100px; margin:0 auto;}
      .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:12px;}
      .card { background:var(--card-background-color,#fff); border-radius:10px;
        box-shadow: var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.12)); padding:14px 16px;}
      .card h3 { margin:0 0 6px; font-size:12px; text-transform:uppercase;
        color:var(--secondary-text-color,#666); letter-spacing:.5px;}
      .card .big { font-size:20px; }
      .card .sub { font-size:13px; color:var(--secondary-text-color,#666); margin-top:2px;}
      table { width:100%; border-collapse:collapse; background:var(--card-background-color,#fff);
        border-radius:10px; overflow:hidden; box-shadow:var(--ha-card-box-shadow,0 1px 3px rgba(0,0,0,.12));}
      th, td { text-align:left; padding:8px 12px; font-size:13px;
        border-bottom:1px solid var(--divider-color,#eee);}
      th { background:var(--secondary-background-color,#f5f5f5); font-weight:500;
        text-transform:uppercase; font-size:11px; letter-spacing:.5px;
        color:var(--secondary-text-color,#666);}
      tr:last-child td { border-bottom:none; }
      .btn { border:none; border-radius:6px; padding:8px 14px; cursor:pointer;
        font-size:13px; background:var(--primary-color,#03a9f4); color:#fff; margin:2px;}
      .btn.warn { background: var(--error-color,#db4437); }
      .btn.ghost { background:transparent; color:var(--primary-color,#03a9f4);
        border:1px solid var(--primary-color,#03a9f4);}
      .btn.small { padding:4px 8px; font-size:12px; }
      .chip { display:inline-block; border-radius:10px; padding:2px 10px; font-size:12px;
        background:var(--secondary-background-color,#eee); }
      .chip.on { background:#c8e6c9; color:#1b5e20; }
      .chip.off { background:#eee; color:#666; }
      .chip.bad { background:#ffcdd2; color:#b71c1c; }
      .chip.warn2 { background:#fff9c4; color:#795548; }
      .section { margin:22px 0 10px; font-size:16px; font-weight:500; }
      .row { display:flex; gap:10px; align-items:center; flex-wrap:wrap; margin:6px 0;}
      label.f { display:flex; flex-direction:column; font-size:12px; gap:3px;
        color:var(--secondary-text-color,#666); }
      input, select { padding:6px 8px; border:1px solid var(--divider-color,#ccc);
        border-radius:6px; font-size:13px; background:var(--card-background-color,#fff);
        color:var(--primary-text-color,#212121); }
      input[type=checkbox] { width:auto; }
      .toast { position:fixed; bottom:18px; left:50%; transform:translateX(-50%);
        background:#323232; color:#fff; padding:10px 20px; border-radius:8px;
        font-size:13px; z-index:20; }
      .muted { color:var(--secondary-text-color,#888); }
      .prog { border-left:4px solid var(--primary-color,#03a9f4); }
      .prog.disabled { border-left-color:#bbb; opacity:.75; }
      pre { background:var(--card-background-color,#fff); padding:14px; border-radius:10px;
        overflow:auto; font-size:12px; box-shadow:var(--ha-card-box-shadow,0 1px 3px rgba(0,0,0,.12));}
      .explain { font-size:12px; color:var(--secondary-text-color,#666); margin:2px 0 8px 12px;}
      .grid2 { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
      @media (max-width:800px){ .grid2 { grid-template-columns:1fr; } }
    `;
  }

  _renderEmpty() {
    this.shadowRoot.innerHTML = `<style>${this._styles()}</style>
      <main><div class="card"><h3>Rain Bird Scheduler</h3>
      <p>No scheduler is configured yet. Add one under Settings → Devices &amp;
      Services → Add integration → Rain Bird Scheduler.</p></div></main>`;
  }

  _renderError(err) {
    this.shadowRoot.innerHTML = `<style>${this._styles()}</style>
      <main><div class="card"><h3>Error</h3><p>${esc(err.message || err)}</p></div></main>`;
  }

  render() {
    if (!this._config || !this._state) return;
    const tabs = [
      ["overview", "Overview"],
      ["programs", "Programs"],
      ["zones", "Zones"],
      ["adjustments", "Adjustments"],
      ["history", "History"],
      ["diagnostics", "Diagnostics"],
    ];
    const entryOptions = this._entries
      .map(
        (entry) =>
          `<option value="${esc(entry.entry_id)}" ${
            entry.entry_id === this._entryId ? "selected" : ""
          }>${esc(entry.title)}</option>`,
      )
      .join("");

    let body = "";
    if (this._draft) body = this._renderEditor();
    else if (this._tab === "overview") body = this._renderOverview();
    else if (this._tab === "programs") body = this._renderPrograms();
    else if (this._tab === "zones") body = this._renderZones();
    else if (this._tab === "adjustments") body = this._renderAdjustments();
    else if (this._tab === "history") body = this._renderHistory();
    else if (this._tab === "diagnostics") body = this._renderDiagnostics();

    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <div class="topbar"><h1>Irrigation</h1>
        ${
          this._entries.length > 1
            ? `<select id="entry-select">${entryOptions}</select>`
            : `<span>${esc(this._entries[0]?.title || "")}</span>`
        }
      </div>
      <div class="tabs">${tabs
        .map(
          ([id, label]) =>
            `<button data-tab="${id}" class="${
              this._tab === id && !this._draft ? "active" : ""
            }">${label}</button>`,
        )
        .join("")}</div>
      <main>${body}</main>
      ${this._toast ? `<div class="toast">${esc(this._toast)}</div>` : ""}
    `;
    this._wire();
  }

  _wire() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-tab]").forEach((button) =>
      button.addEventListener("click", async () => {
        this._draft = null;
        this._tab = button.dataset.tab;
        if (this._tab === "history") await this._loadHistory().catch(() => {});
        if (this._tab === "diagnostics")
          await this._loadDiagnostics().catch(() => {});
        this.render();
      }),
    );
    const entrySelect = root.getElementById("entry-select");
    if (entrySelect)
      entrySelect.addEventListener("change", async (event) => {
        this._entryId = event.target.value;
        await this._loadAll();
        this._subscribe();
      });
    root.querySelectorAll("[data-action]").forEach((element) =>
      element.addEventListener("click", (event) =>
        this._handleAction(
          element.dataset.action,
          element.dataset,
          event,
        ),
      ),
    );
    if (this._draft) this._wireEditor();
  }

  // ------------------------------------------------------------------
  // Overview
  // ------------------------------------------------------------------

  _upcomingSteps(limit = 12) {
    const rows = [];
    for (const run of this._timeline?.runs || []) {
      for (const step of run.steps || []) {
        rows.push({ run, step });
      }
    }
    rows.sort((a, b) =>
      a.step.planned_start_utc.localeCompare(b.step.planned_start_utc),
    );
    return rows.slice(0, limit);
  }

  _renderOverview() {
    const state = this._state;
    const controller = this._config.controller;
    const observation = state.observation;
    const active = state.active_run;
    const next = state.next_run;
    const rainDelay = observation?.rain_delay_days;
    const paused = (state.executor_state || "").startsWith("paused");
    const running = ["starting", "watering", "inter_zone_gap", "reconciling"].includes(
      state.executor_state,
    );

    const upcoming = this._upcomingSteps()
      .map(
        ({ run, step }) => `
        <tr>
          <td>${esc(run.program_name)}</td>
          <td>${esc(step.zone_name)}${
            step.cycle_count > 1
              ? ` <span class="muted">cycle ${step.cycle_index}/${step.cycle_count}</span>`
              : ""
          }</td>
          <td>${fmtDt(step.requested_start_utc)}</td>
          <td>${fmtDt(step.planned_start_utc)}</td>
          <td>${fmtTime(step.planned_end_utc)}</td>
          <td>${step.duration_minutes} min
            <span class="muted">(${esc(step.exact_minutes)} exact)</span></td>
        </tr>`,
      )
      .join("");

    const conflicts = (this._timeline?.conflicts || [])
      .map(
        (conflict) =>
          `<div class="chip warn2" title="${esc(conflict.message)}">
            ${esc(conflict.reason)}: ${esc(conflict.message)}</div>`,
      )
      .join(" ");

    const skipped = (this._timeline?.runs || [])
      .flatMap((run) => run.skipped_zones || [])
      .map(
        (zone) =>
          `<div class="chip warn2">${esc(zone.zone_name)}: ${esc(
            zone.reason,
          )} — ${esc(zone.detail)}</div>`,
      )
      .join(" ");

    return `
      <div class="cards">
        <div class="card"><h3>Scheduler</h3>
          <div class="big">${esc(state.executor_state)}</div>
          <div class="sub">${
            controller.enabled ? "enabled" : "disabled"
          } · ${esc(controller.authority_mode)}</div>
          ${state.paused_reason ? `<div class="sub">paused: ${esc(state.paused_reason)}</div>` : ""}
        </div>
        <div class="card"><h3>Active</h3>
          <div class="big">${esc(state.active_zone || "Idle")}</div>
          <div class="sub">${
            active
              ? `${esc(active.program_name)} · expected end ${fmtTime(
                  state.expected_end,
                )}`
              : "No scheduler run active"
          }</div>
        </div>
        <div class="card"><h3>Next compiled occurrence</h3>
          <div class="big">${
            next?.steps?.length ? fmtDt(next.steps[0].planned_start_utc) : "—"
          }</div>
          <div class="sub">${
            next ? `${esc(next.program_name)} · requested ${fmtTime(next.requested_start_utc)}` : "Nothing scheduled"
          }</div>
        </div>
        <div class="card"><h3>Rain</h3>
          <div class="big">${
            observation?.rain_sensor_active == null
              ? "No sensor"
              : observation.rain_sensor_active
                ? "Sensor WET"
                : "Sensor dry"
          }</div>
          <div class="sub">Native Rain Bird rain delay:
            <b>${rainDelay == null ? "unknown" : `${rainDelay} day${rainDelay === 1 ? "" : "s"}`}</b></div>
        </div>
        <div class="card"><h3>Status flags</h3>
          <div>${state.source_available ? '<span class="chip on">source available</span>' : '<span class="chip bad">source unavailable</span>'}</div>
          <div style="margin-top:4px">${
            state.external_watering
              ? '<span class="chip warn2">external watering active</span>'
              : '<span class="chip off">no external watering</span>'
          }</div>
          <div style="margin-top:4px">${
            state.native_schedule_conflict
              ? '<span class="chip bad">native schedule conflict</span>'
              : '<span class="chip off">no native conflict</span>'
          }</div>
        </div>
      </div>

      <div class="row" style="margin-top:14px">
        <button class="btn warn" data-action="stop"
          title="Stops ALL watering on the controller">Stop controller</button>
        ${running ? '<button class="btn ghost" data-action="pause">Pause</button>' : ""}
        ${paused ? '<button class="btn" data-action="resume">Resume</button>' : ""}
        ${running ? '<button class="btn ghost" data-action="skip">Skip current zone</button>' : ""}
        <button class="btn ghost" data-action="recalculate">Recalculate</button>
      </div>

      <div class="section">Upcoming (compiled plan)</div>
      <table>
        <tr><th>Program</th><th>Zone</th><th>Requested</th><th>Planned start</th>
        <th>Planned end</th><th>Runtime</th></tr>
        ${upcoming || '<tr><td colspan="6" class="muted">Nothing scheduled in the next 7 days</td></tr>'}
      </table>
      ${conflicts ? `<div class="section">Conflicts</div>${conflicts}` : ""}
      ${skipped ? `<div class="section">Planned skips</div>${skipped}` : ""}
    `;
  }

  // ------------------------------------------------------------------
  // Programs
  // ------------------------------------------------------------------

  _renderPrograms() {
    const programs = Object.values(this._config.programs || {});
    programs.sort((a, b) => a.name.localeCompare(b.name));
    const cards = programs
      .map((program) => {
        const zones = (program.zone_steps || [])
          .slice()
          .sort((a, b) => a.position - b.position)
          .map((step) => {
            const zone = this._config.zones[step.zone_id];
            return zone ? zone.display_name : step.zone_id;
          })
          .join(" → ");
        return `
        <div class="card prog ${program.enabled ? "" : "disabled"}" style="margin-bottom:10px">
          <div class="row" style="justify-content:space-between">
            <div>
              <b>${esc(program.name)}</b>
              <span class="chip ${program.enabled ? "on" : "off"}">${
                program.enabled ? "enabled" : "disabled"
              }</span>
              <span class="chip">priority ${program.priority}</span>
            </div>
            <div>
              <button class="btn small" data-action="run-program" data-id="${program.id}">Run now</button>
              <button class="btn small ghost" data-action="edit-program" data-id="${program.id}">Edit</button>
              <button class="btn small ghost" data-action="toggle-program" data-id="${program.id}">${
                program.enabled ? "Disable" : "Enable"
              }</button>
              <button class="btn small ghost" data-action="duplicate-program" data-id="${program.id}">Duplicate</button>
              <button class="btn small warn" data-action="delete-program" data-id="${program.id}">Delete</button>
            </div>
          </div>
          <div class="sub">${esc(recurrenceSummary(program))}</div>
          <div class="sub">Zones: ${esc(zones || "none")}</div>
        </div>`;
      })
      .join("");
    return `
      <div class="row" style="justify-content:flex-end">
        <button class="btn" data-action="new-program">New program</button>
      </div>
      ${cards || '<div class="card">No programs yet. Create one to start scheduling.</div>'}
    `;
  }

  // ------------------------------------------------------------------
  // Program editor
  // ------------------------------------------------------------------

  _newDraft() {
    return {
      name: "New program",
      enabled: true,
      priority: 100,
      recurrence: {
        kind: "weekly",
        weekdays: [0, 2, 4],
        interval_days: null,
        anchor_date: null,
        start_date: null,
        end_date: null,
        months: null,
        dst_nonexistent_policy: "shift_forward",
      },
      nominal_start_times: ["06:00:00"],
      zone_steps: [],
      adjustment_provider: {
        kind: "fixed",
        percent: "100",
        monthly_percents: null,
        entity_id: null,
      },
      rain_policy: {
        honor_native_delay: true,
        skip_when_sensor_wet: true,
        sensor_cut_behavior: "abort_run",
      },
      missed_run_policy: "run_late",
      external_interruption_policy: "pause",
      watering_window: null,
    };
  }

  _renderEditor() {
    const draft = this._draft;
    const zones = this._config.zones;
    const zoneOptions = (selected) =>
      Object.values(zones)
        .map(
          (zone) =>
            `<option value="${zone.id}" ${zone.id === selected ? "selected" : ""}>
              ${esc(zone.display_name)} (station ${zone.reference.station_number})
            </option>`,
        )
        .join("");

    const stepRows = draft.zone_steps
      .map(
        (step, index) => `
      <tr>
        <td>
          <button class="btn small ghost" data-action="step-up" data-index="${index}">↑</button>
          <button class="btn small ghost" data-action="step-down" data-index="${index}">↓</button>
        </td>
        <td><select data-step="${index}" data-field="zone_id">${zoneOptions(step.zone_id)}</select></td>
        <td><input type="checkbox" data-step="${index}" data-field="enabled" ${step.enabled ? "checked" : ""}></td>
        <td><input type="number" style="width:70px" min="0" step="0.5" placeholder="zone default"
          data-step="${index}" data-field="base_runtime_override_minutes"
          value="${step.base_runtime_override_minutes ?? ""}"></td>
        <td><input type="number" style="width:70px" min="0" step="1"
          data-step="${index}" data-field="requested_offset_seconds"
          value="${step.requested_offset_seconds || 0}"></td>
        <td><input type="number" style="width:60px" min="0" step="1" placeholder="—"
          data-step="${index}" data-field="max_cycle_minutes_override"
          value="${step.max_cycle_minutes_override ?? ""}"></td>
        <td><input type="number" style="width:60px" min="0" step="1" placeholder="—"
          data-step="${index}" data-field="minimum_soak_minutes_override"
          value="${step.minimum_soak_minutes_override ?? ""}"></td>
        <td><button class="btn small warn" data-action="step-remove" data-index="${index}">✕</button></td>
      </tr>`,
      )
      .join("");

    const startInputs = draft.nominal_start_times
      .map(
        (time, index) => `
        <span class="row" style="display:inline-flex">
          <input type="time" data-start-index="${index}" value="${time.slice(0, 5)}">
          <button class="btn small warn" data-action="start-remove" data-index="${index}">✕</button>
        </span>`,
      )
      .join("");

    const rec = draft.recurrence;
    const provider = draft.adjustment_provider;
    const window_ = draft.watering_window;

    return `
      <div class="card">
        <div class="row" style="justify-content:space-between">
          <b>${this._draftIsNew ? "New program" : `Edit: ${esc(draft.name)}`}</b>
          <div>
            <button class="btn" data-action="save-program">Save</button>
            <button class="btn ghost" data-action="cancel-edit">Cancel</button>
          </div>
        </div>

        <div class="section">Basics</div>
        <div class="row">
          <label class="f">Name <input id="f-name" value="${esc(draft.name)}"></label>
          <label class="f">Enabled <input id="f-enabled" type="checkbox" ${draft.enabled ? "checked" : ""}></label>
          <label class="f">Priority (lower runs first)
            <input id="f-priority" type="number" min="1" max="1000" value="${draft.priority}"></label>
        </div>

        <div class="section">Recurrence</div>
        <div class="row">
          <label class="f">Kind
            <select id="f-rec-kind">${RECURRENCE_KINDS.map(
              ([value, label]) =>
                `<option value="${value}" ${rec.kind === value ? "selected" : ""}>${label}</option>`,
            ).join("")}</select></label>
          <span id="f-weekdays" style="${rec.kind === "weekly" ? "" : "display:none"}">
            ${WEEKDAYS.map(
              (day, index) =>
                `<label style="margin-right:6px"><input type="checkbox" data-weekday="${index}"
                  ${rec.weekdays?.includes(index) ? "checked" : ""}> ${day}</label>`,
            ).join("")}
          </span>
          <label class="f" id="f-interval-wrap" style="${rec.kind === "interval" ? "" : "display:none"}">
            Every N days <input id="f-interval" type="number" min="1" value="${rec.interval_days ?? 2}"></label>
          <label class="f" id="f-anchor-wrap" style="${rec.kind === "interval" ? "" : "display:none"}">
            Anchor date <input id="f-anchor" type="date" value="${rec.anchor_date ?? ""}"></label>
        </div>
        <div class="row">
          <label class="f">Start date (optional) <input id="f-startdate" type="date" value="${rec.start_date ?? ""}"></label>
          <label class="f">End date (optional) <input id="f-enddate" type="date" value="${rec.end_date ?? ""}"></label>
          <label class="f">Nonexistent DST time
            <select id="f-dst">
              <option value="shift_forward" ${rec.dst_nonexistent_policy === "shift_forward" ? "selected" : ""}>Shift to first valid instant</option>
              <option value="skip" ${rec.dst_nonexistent_policy === "skip" ? "selected" : ""}>Skip that start</option>
            </select></label>
        </div>

        <div class="section">Start times (local)</div>
        <div class="row">${startInputs}
          <button class="btn small ghost" data-action="start-add">+ Add start time</button></div>

        <div class="section">Zones (run order; shared requested start unless offset)</div>
        <table>
          <tr><th>Order</th><th>Zone</th><th>On</th><th>Runtime override (min)</th>
            <th>Offset (s)</th><th>Max cycle</th><th>Min soak</th><th></th></tr>
          ${stepRows || '<tr><td colspan="8" class="muted">No zones yet</td></tr>'}
        </table>
        <button class="btn small ghost" style="margin-top:6px" data-action="step-add">+ Add zone</button>

        <div class="section">Runtime adjustment</div>
        <div class="row">
          <label class="f">Provider
            <select id="f-provider">${PROVIDER_KINDS.map(
              ([value, label]) =>
                `<option value="${value}" ${provider.kind === value ? "selected" : ""}>${label}</option>`,
            ).join("")}</select></label>
          <label class="f" id="f-percent-wrap" style="${provider.kind === "manual_percent" ? "" : "display:none"}">
            Percent <input id="f-percent" type="number" min="0" max="300" value="${provider.percent ?? 100}"></label>
          <label class="f" id="f-entity-wrap" style="${provider.kind.startsWith("entity") ? "" : "display:none"}">
            Source entity <input id="f-entity" placeholder="sensor.example" value="${esc(provider.entity_id ?? "")}"></label>
        </div>
        <div id="f-monthly-wrap" style="${provider.kind === "monthly_curve" ? "" : "display:none"}">
          <div class="row">${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
            .map(
              (month, index) =>
                `<label class="f">${month}<input type="number" style="width:60px" min="0" max="300"
                  data-month="${index}" value="${provider.monthly_percents?.[index] ?? 100}"></label>`,
            )
            .join("")}</div>
        </div>

        <div class="section">Rain policy</div>
        <div class="row">
          <label><input id="f-honor-delay" type="checkbox" ${draft.rain_policy.honor_native_delay ? "checked" : ""}>
            Honor native rain delay (days)</label>
          <label><input id="f-skip-wet" type="checkbox" ${draft.rain_policy.skip_when_sensor_wet ? "checked" : ""}>
            Skip when rain sensor is wet</label>
          <label class="f">On sensor cut
            <select id="f-sensor-cut">${SENSOR_CUT.map(
              ([value, label]) =>
                `<option value="${value}" ${draft.rain_policy.sensor_cut_behavior === value ? "selected" : ""}>${label}</option>`,
            ).join("")}</select></label>
        </div>

        <div class="section">Policies</div>
        <div class="row">
          <label class="f">Missed run
            <select id="f-missed">
              <option value="run_late" ${draft.missed_run_policy === "run_late" ? "selected" : ""}>Run late (within tolerance)</option>
              <option value="skip" ${draft.missed_run_policy === "skip" ? "selected" : ""}>Skip</option>
            </select></label>
          <label class="f">External interruption
            <select id="f-interrupt">
              <option value="pause" ${draft.external_interruption_policy === "pause" ? "selected" : ""}>Pause and resume</option>
              <option value="abort" ${draft.external_interruption_policy === "abort" ? "selected" : ""}>Abort the run</option>
            </select></label>
        </div>

        <div class="section">Watering window</div>
        <div class="row">
          <label><input id="f-window-on" type="checkbox" ${window_ ? "checked" : ""}> Restrict starts to a window</label>
          <label class="f">From <input id="f-window-start" type="time" value="${window_ ? window_.start_local.slice(0, 5) : "05:00"}"></label>
          <label class="f">To <input id="f-window-end" type="time" value="${window_ ? window_.end_local.slice(0, 5) : "10:00"}"></label>
          <label class="f">Policy
            <select id="f-window-policy">${WINDOW_POLICIES.map(
              ([value, label]) =>
                `<option value="${value}" ${window_?.policy === value ? "selected" : ""}>${label}</option>`,
            ).join("")}</select></label>
        </div>
      </div>
    `;
  }

  _wireEditor() {
    const root = this.shadowRoot;
    root.querySelectorAll("[data-step]").forEach((input) =>
      input.addEventListener("change", () => {
        const step = this._draft.zone_steps[Number(input.dataset.step)];
        const field = input.dataset.field;
        if (input.type === "checkbox") step[field] = input.checked;
        else if (input.value === "") step[field] = null;
        else if (field === "zone_id") step[field] = input.value;
        else if (field === "base_runtime_override_minutes")
          step[field] = String(input.value);
        else step[field] = Number(input.value);
      }),
    );
    root.querySelectorAll("[data-start-index]").forEach((input) =>
      input.addEventListener("change", () => {
        this._draft.nominal_start_times[Number(input.dataset.startIndex)] =
          `${input.value}:00`;
      }),
    );
    root.querySelectorAll("[data-weekday]").forEach((input) =>
      input.addEventListener("change", () => {
        const day = Number(input.dataset.weekday);
        const set = new Set(this._draft.recurrence.weekdays || []);
        if (input.checked) set.add(day);
        else set.delete(day);
        this._draft.recurrence.weekdays = [...set].sort();
      }),
    );
    root.querySelectorAll("[data-month]").forEach((input) =>
      input.addEventListener("change", () => {
        const curve =
          this._draft.adjustment_provider.monthly_percents ||
          Array(12).fill("100");
        curve[Number(input.dataset.month)] = String(input.value);
        this._draft.adjustment_provider.monthly_percents = curve;
      }),
    );
    const rewire = (id, handler) => {
      const element = root.getElementById(id);
      if (element) element.addEventListener("change", handler);
    };
    rewire("f-rec-kind", (event) => {
      this._draft.recurrence.kind = event.target.value;
      this._collectDraft();
      this.render();
    });
    rewire("f-provider", (event) => {
      this._draft.adjustment_provider.kind = event.target.value;
      this._collectDraft();
      this.render();
    });
    rewire("f-window-on", () => {
      this._collectDraft();
      this.render();
    });
  }

  _collectDraft() {
    const root = this.shadowRoot;
    const value = (id) => root.getElementById(id)?.value;
    const checked = (id) => !!root.getElementById(id)?.checked;
    const draft = this._draft;
    if (!root.getElementById("f-name")) return;
    draft.name = value("f-name") || draft.name;
    draft.enabled = checked("f-enabled");
    draft.priority = Number(value("f-priority") || 100);
    draft.recurrence.kind = value("f-rec-kind");
    draft.recurrence.interval_days =
      draft.recurrence.kind === "interval"
        ? Number(value("f-interval") || 2)
        : null;
    draft.recurrence.anchor_date =
      draft.recurrence.kind === "interval" ? value("f-anchor") || null : null;
    draft.recurrence.start_date = value("f-startdate") || null;
    draft.recurrence.end_date = value("f-enddate") || null;
    draft.recurrence.dst_nonexistent_policy = value("f-dst");
    draft.adjustment_provider.kind = value("f-provider");
    draft.adjustment_provider.percent = String(value("f-percent") || "100");
    draft.adjustment_provider.entity_id = value("f-entity") || null;
    if (draft.adjustment_provider.kind !== "monthly_curve")
      draft.adjustment_provider.monthly_percents = null;
    else if (!draft.adjustment_provider.monthly_percents)
      draft.adjustment_provider.monthly_percents = Array(12).fill("100");
    draft.rain_policy.honor_native_delay = checked("f-honor-delay");
    draft.rain_policy.skip_when_sensor_wet = checked("f-skip-wet");
    draft.rain_policy.sensor_cut_behavior = value("f-sensor-cut");
    draft.missed_run_policy = value("f-missed");
    draft.external_interruption_policy = value("f-interrupt");
    draft.watering_window = checked("f-window-on")
      ? {
          start_local: `${value("f-window-start") || "05:00"}:00`,
          end_local: `${value("f-window-end") || "10:00"}:00`,
          policy: value("f-window-policy") || "skip_step",
        }
      : null;
  }

  async _saveProgram() {
    this._collectDraft();
    const draft = JSON.parse(JSON.stringify(this._draft));
    draft.zone_steps.forEach((step, index) => (step.position = index));
    if (!draft.zone_steps.length) {
      this._toastMsg("Add at least one zone.");
      return;
    }
    if (this._draftIsNew) {
      await this._action(
        this.api({
          type: `${DOMAIN}/program/create`,
          entry_id: this._entryId,
          program: draft,
        }),
        "Program created",
      );
    } else {
      const { id, revision, ...patch } = draft;
      await this._action(
        this.api({
          type: `${DOMAIN}/program/update`,
          entry_id: this._entryId,
          program_id: id,
          expected_revision: revision,
          patch,
        }),
        "Program saved",
      );
    }
    this._draft = null;
    await this._loadAll();
  }

  // ------------------------------------------------------------------
  // Zones
  // ------------------------------------------------------------------

  _renderZones() {
    const zones = Object.values(this._config.zones || {});
    zones.sort(
      (a, b) => a.reference.station_number - b.reference.station_number,
    );
    const rows = zones
      .map(
        (zone) => `
      <tr data-zone-row="${zone.id}">
        <td>${zone.reference.station_number}</td>
        <td><input data-z="${zone.id}" data-f="display_name" value="${esc(zone.display_name)}"></td>
        <td class="muted">${esc(zone.reference.last_known_entity_id)}</td>
        <td><input type="checkbox" data-z="${zone.id}" data-f="enabled" ${zone.enabled ? "checked" : ""}></td>
        <td><input type="number" style="width:70px" step="0.5" min="0"
          data-z="${zone.id}" data-f="base_runtime_minutes" value="${zone.base_runtime_minutes}"></td>
        <td><select data-z="${zone.id}" data-f="soil_type">${SOILS.map(
          (soil) =>
            `<option value="${soil}" ${zone.soil_type === soil ? "selected" : ""}>${soil}</option>`,
        ).join("")}</select></td>
        <td><select data-z="${zone.id}" data-f="slope_class">${SLOPES.map(
          (slope) =>
            `<option value="${slope}" ${zone.slope_class === slope ? "selected" : ""}>${slope}</option>`,
        ).join("")}</select></td>
        <td><input type="number" style="width:60px" min="0" placeholder="—"
          data-z="${zone.id}" data-f="max_cycle_minutes" value="${zone.max_cycle_minutes ?? ""}"></td>
        <td><input type="number" style="width:60px" min="0" placeholder="—"
          data-z="${zone.id}" data-f="minimum_soak_minutes" value="${zone.minimum_soak_minutes ?? ""}"></td>
        <td><select data-z="${zone.id}" data-f="minimum_runtime_policy">${MIN_RUNTIME.map(
          ([value, label]) =>
            `<option value="${value}" ${(zone.minimum_runtime_policy ?? "") === value ? "selected" : ""}>${label}</option>`,
        ).join("")}</select></td>
        <td><button class="btn small" data-action="save-zone" data-id="${zone.id}">Save</button></td>
      </tr>`,
      )
      .join("");
    return `
      <div class="sub" style="margin-bottom:8px">Soil and slope populate editable
        Cycle+Soak suggestions; they never change behavior invisibly. Runtimes are
        quantized once to whole minutes (round half up) when a plan is compiled.</div>
      <table>
        <tr><th>Station</th><th>Name</th><th>Source entity</th><th>On</th>
          <th>Base (min)</th><th>Soil</th><th>Slope</th><th>Max cycle</th>
          <th>Min soak</th><th>Sub-minute policy</th><th></th></tr>
        ${rows || '<tr><td colspan="11" class="muted">No zones discovered</td></tr>'}
      </table>`;
  }

  async _saveZone(zoneId) {
    const zone = this._config.zones[zoneId];
    const root = this.shadowRoot;
    const patch = {};
    root
      .querySelectorAll(`[data-z="${zoneId}"]`)
      .forEach((input) => {
        const field = input.dataset.f;
        if (input.type === "checkbox") patch[field] = input.checked;
        else if (input.value === "" || input.value == null)
          patch[field] = null;
        else if (
          ["max_cycle_minutes", "minimum_soak_minutes"].includes(field)
        )
          patch[field] = Number(input.value);
        else patch[field] = String(input.value);
      });
    if (patch.minimum_runtime_policy === "") patch.minimum_runtime_policy = null;
    await this._action(
      this.api({
        type: `${DOMAIN}/zone/update`,
        entry_id: this._entryId,
        zone_id: zoneId,
        expected_revision: zone.revision,
        patch,
      }),
      "Zone saved",
    );
    await this._loadAll();
  }

  // ------------------------------------------------------------------
  // Adjustments
  // ------------------------------------------------------------------

  _renderAdjustments() {
    const runs = this._timeline?.runs || [];
    if (!runs.length)
      return '<div class="card muted">No upcoming runs to explain.</div>';
    const blocks = runs.slice(0, 6).map((run) => {
      const snapshot = run.adjustment_snapshot || {};
      const zones = Object.entries(snapshot.per_zone || {})
        .map(([zoneId, result]) => {
          const zone = this._config.zones[zoneId];
          const stale = (result.stale_inputs || []).length
            ? `<span class="chip bad">stale: ${esc(result.stale_inputs.join(", "))}</span>`
            : "";
          return `
          <tr><td>${esc(zone?.display_name || zoneId)}</td>
            <td>${esc(result.base_runtime_minutes)} min</td>
            <td>${esc(result.seasonal_factor)}%</td>
            <td>${esc(result.exact_adjusted_minutes)} min</td>
            <td><b>${result.quantized_minutes} min</b></td>
            <td>${stale}</td></tr>
          <tr><td colspan="6" class="explain">${(result.explanation || [])
            .map(esc)
            .join("<br>")}</td></tr>`;
        })
        .join("");
      return `
        <div class="section">${esc(run.program_name)} — ${fmtDt(run.requested_start_utc)}
          <span class="chip">${esc(snapshot.provider_kind || "fixed")}</span></div>
        <table><tr><th>Zone</th><th>Base</th><th>Factor</th><th>Exact</th>
          <th>Commanded</th><th>Inputs</th></tr>${zones}</table>`;
    });
    return blocks.join("");
  }

  // ------------------------------------------------------------------
  // History
  // ------------------------------------------------------------------

  _renderHistory() {
    if (!this._history)
      return '<div class="card muted">Loading history…</div>';
    const outcomeChip = (outcome) => {
      const good = ["completed", "completed_with_skips"].includes(outcome);
      const bad = ["failed", "aborted_power_loss"].includes(outcome);
      return `<span class="chip ${good ? "on" : bad ? "bad" : "warn2"}">${esc(outcome)}</span>`;
    };
    const rows = (this._history.runs || [])
      .map(
        (run) => `
      <tr data-action="select-run" data-id="${run.run_id}" style="cursor:pointer">
        <td>${fmtDt(run.requested_start_utc)}</td>
        <td>${fmtDt(run.actual_start_utc)}</td>
        <td>${esc(run.program_name)}${run.manual ? ' <span class="chip">manual</span>' : ""}</td>
        <td>${outcomeChip(run.outcome)}</td>
        <td>${esc(run.reason ?? "")}</td>
        <td>${run.retries}${run.uncertain_commands ? ` <span class="chip warn2">${run.uncertain_commands} uncertain</span>` : ""}</td>
      </tr>`,
      )
      .join("");
    let detail = "";
    if (this._selectedRun) {
      const records = (this._history.zone_records || []).filter(
        (record) => record.run_id === this._selectedRun,
      );
      detail = `
        <div class="section">Zone detail</div>
        <table><tr><th>Zone</th><th>Cycle</th><th>Planned</th><th>Actual start</th>
          <th>Actual end</th><th>Commanded</th><th>Status</th><th>Reason</th></tr>
        ${records
          .map(
            (record) => `<tr>
            <td>${esc(record.zone_name)}</td>
            <td>${record.cycle_index}/${record.cycle_count}</td>
            <td>${fmtTime(record.planned_start_utc)}–${fmtTime(record.planned_end_utc)}</td>
            <td>${fmtTime(record.actual_start_utc)}</td>
            <td>${fmtTime(record.actual_end_utc)}</td>
            <td>${record.commanded_minutes} min <span class="muted">(${esc(record.exact_minutes)} exact)</span></td>
            <td>${esc(record.status)}</td>
            <td>${esc(record.reason ?? "")}</td></tr>`,
          )
          .join("")}</table>`;
    }
    const interventions = (this._history.interventions || [])
      .slice()
      .reverse()
      .slice(0, 20)
      .map(
        (item) =>
          `<tr><td>${fmtDt(item.recorded_at_utc)}</td><td>${esc(item.kind)}</td>
           <td>${esc(item.message)}</td></tr>`,
      )
      .join("");
    return `
      <div class="section">Runs (authoritative bounded history)</div>
      <table><tr><th>Requested</th><th>Actual start</th><th>Program</th>
        <th>Outcome</th><th>Reason</th><th>Retries</th></tr>${
          rows || '<tr><td colspan="6" class="muted">No runs recorded yet</td></tr>'
        }</table>
      ${detail}
      ${
        interventions
          ? `<div class="section">Failures &amp; interventions</div>
             <table><tr><th>When</th><th>Kind</th><th>Message</th></tr>${interventions}</table>`
          : ""
      }`;
  }

  _renderDiagnostics() {
    if (!this._diagnostics)
      return '<div class="card muted">Loading diagnostics…</div>';
    return `
      <div class="sub" style="margin-bottom:8px">${(
        this._diagnostics.notes || []
      )
        .map(esc)
        .join("<br>")}</div>
      <pre>${esc(JSON.stringify(this._diagnostics, null, 2))}</pre>`;
  }

  // ------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------

  async _handleAction(action, data) {
    const entry_id = this._entryId;
    const programs = this._config?.programs || {};
    switch (action) {
      case "stop":
        if (
          !confirm(
            "Stop the controller? This stops ALL watering on the Rain Bird " +
              "controller, not just the current zone.",
          )
        )
          return;
        await this._action(
          this.api({ type: `${DOMAIN}/run/stop`, entry_id }),
          "Controller stopped",
        );
        break;
      case "pause":
        await this._action(
          this.api({ type: `${DOMAIN}/run/pause`, entry_id }),
          "Paused (controller stopped, run resumable)",
        );
        break;
      case "resume":
        await this._action(
          this.api({ type: `${DOMAIN}/run/resume`, entry_id }),
        );
        break;
      case "skip":
        await this._action(
          this.api({ type: `${DOMAIN}/run/skip_current`, entry_id }),
        );
        break;
      case "recalculate":
        await this._loadAll();
        this._toastMsg("Plan recalculated");
        break;
      case "run-program":
        await this._action(
          this.api({
            type: `${DOMAIN}/run/start`,
            entry_id,
            program_id: data.id,
          }),
          "Run started",
        );
        break;
      case "toggle-program": {
        const program = programs[data.id];
        await this._action(
          this.api({
            type: `${DOMAIN}/program/update`,
            entry_id,
            program_id: data.id,
            expected_revision: program.revision,
            patch: { enabled: !program.enabled },
          }),
        );
        await this._loadAll();
        break;
      }
      case "duplicate-program":
        await this._action(
          this.api({
            type: `${DOMAIN}/program/duplicate`,
            entry_id,
            program_id: data.id,
          }),
          "Duplicated (disabled until you enable it)",
        );
        await this._loadAll();
        break;
      case "delete-program": {
        const program = programs[data.id];
        if (!confirm(`Delete program "${program?.name}"?`)) return;
        await this._action(
          this.api({
            type: `${DOMAIN}/program/delete`,
            entry_id,
            program_id: data.id,
          }),
          "Program deleted",
        );
        await this._loadAll();
        break;
      }
      case "new-program":
        this._draft = this._newDraft();
        this._draftIsNew = true;
        this.render();
        break;
      case "edit-program":
        this._draft = JSON.parse(JSON.stringify(programs[data.id]));
        this._draftIsNew = false;
        this.render();
        break;
      case "cancel-edit":
        this._draft = null;
        this.render();
        break;
      case "save-program":
        await this._saveProgram();
        break;
      case "step-add":
        this._collectDraft();
        {
          const first = Object.keys(this._config.zones)[0];
          if (!first) {
            this._toastMsg("No zones discovered yet.");
            return;
          }
          this._draft.zone_steps.push({
            zone_id: first,
            position: this._draft.zone_steps.length,
            enabled: true,
            requested_offset_seconds: 0,
            base_runtime_override_minutes: null,
            max_cycle_minutes_override: null,
            minimum_soak_minutes_override: null,
          });
        }
        this.render();
        break;
      case "step-remove":
        this._collectDraft();
        this._draft.zone_steps.splice(Number(data.index), 1);
        this.render();
        break;
      case "step-up":
      case "step-down": {
        this._collectDraft();
        const index = Number(data.index);
        const target = action === "step-up" ? index - 1 : index + 1;
        const steps = this._draft.zone_steps;
        if (target < 0 || target >= steps.length) return;
        [steps[index], steps[target]] = [steps[target], steps[index]];
        this.render();
        break;
      }
      case "start-add":
        this._collectDraft();
        this._draft.nominal_start_times.push("06:00:00");
        this.render();
        break;
      case "start-remove":
        this._collectDraft();
        this._draft.nominal_start_times.splice(Number(data.index), 1);
        this.render();
        break;
      case "save-zone":
        await this._saveZone(data.id);
        break;
      case "select-run":
        this._selectedRun = data.id;
        this.render();
        break;
      default:
        break;
    }
  }
}

customElements.define("rainbird-scheduler-panel", RainBirdSchedulerPanel);

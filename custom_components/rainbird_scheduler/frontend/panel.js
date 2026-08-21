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
  ["fixed", "No adjustment — always run base minutes"],
  ["seasonal_auto", "Automatic seasonal (nearest US city)"],
  ["manual_percent", "Fixed percentage I type in (e.g. 50%)"],
  ["monthly_curve", "Percentage per month I type in"],
  ["entity_percent", "Percentage read from a sensor entity"],
  ["entity_runtime", "Total minutes read from a sensor entity"],
];

const SENSOR_CUT = [
  ["abort_run", "Abort the run"],
  ["pause_until_dry", "Pause until dry"],
  ["defer_remaining", "Defer remaining zones"],
];

// Same behaviors as SENSOR_CUT; "pause until dry" reads oddly for a freeze.
const FREEZE_CUT = [
  ["abort_run", "Abort the run"],
  ["pause_until_dry", "Pause until clear"],
  ["defer_remaining", "Defer remaining zones"],
];

const WHEN_UNAVAILABLE = [
  ["allow_watering", "Water normally (note it as unknown)"],
  ["block_watering", "Skip watering until temperature is known"],
];

const TEMP_UNITS = ["°C", "°F"];

const WINDOW_POLICIES = [
  ["skip_step", "Skip steps outside the window"],
  ["truncate_last", "Truncate the last step"],
  ["defer_occurrence", "Defer the whole occurrence"],
  ["require_intervention", "Mark conflict, require intervention"],
];

const SOILS = ["unknown", "clay", "loam", "sand"];
const SLOPES = ["flat", "moderate", "steep"];

/* Editable Cycle+Soak starting points, keyed "soil|slope" (plan §16: soil and
 * slope populate SUGGESTIONS, never invisible behavior). Values follow the
 * Texas A&M AgriLife Extension runoff guide (AGEN-PU-217): spray-head basis,
 * clay = short cycles + ≥60 min soak, lighter soils = longer cycles + 30–40
 * min soak, steeper slope = shorter cycle. Sand on flat ground rarely needs
 * cycling at all, so it deliberately has no suggestion. */
const CYCLE_SOAK_SUGGESTIONS = {
  "clay|flat": { cycle: 6, soak: 60 },
  "clay|moderate": { cycle: 4, soak: 60 },
  "clay|steep": { cycle: 3, soak: 60 },
  "loam|flat": { cycle: 12, soak: 40 },
  "loam|moderate": { cycle: 9, soak: 40 },
  "loam|steep": { cycle: 7, soak: 40 },
  "sand|moderate": { cycle: 15, soak: 30 },
  "sand|steep": { cycle: 10, soak: 30 },
};
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
    .map((start) => fmtStartTime(start))
    .join(", ");
  return `${base} at ${starts || "—"}`;
}

/* One program start time: legacy "HH:MM:SS" strings and the current
 * {kind, at, offset_minutes} objects both occur in the wild. */
function normalizeStart(start) {
  if (typeof start === "string") {
    return { kind: "clock", at: start, offset_minutes: 0 };
  }
  return {
    kind: start?.kind || "clock",
    at: start?.at || null,
    offset_minutes: Number(start?.offset_minutes || 0),
  };
}

function fmtStartTime(start) {
  const s = normalizeStart(start);
  if (s.kind === "clock") return (s.at || "??:??").slice(0, 5);
  const base = s.kind === "sunrise" ? "sunrise" : "sunset";
  if (!s.offset_minutes) return base;
  const sign = s.offset_minutes < 0 ? "−" : "+";
  return `${base} ${sign}${Math.abs(s.offset_minutes)}m`;
}

/* Per-tab explainers. Every page opens with a concrete scenario, then defines
 * each control on that page. Static authored HTML — no user data inside. */
const HELP = {
  overview: `
    <p class="scen">Say Jonny opens this page at 8:55 AM. His "Morning Lawn"
    program is about to fire: the cards tell him what the scheduler is doing
    right now, and the table below shows exactly what it intends to do next.</p>
    <p><b class="k">Scheduler card</b> — the executor state machine:
    <i>idle</i> (nothing to do), <i>waiting</i> (a run is compiled and waiting
    for its start time), <i>starting/watering/inter_zone_gap</i> (actively
    driving the controller), <i>paused_*</i> (holding, with the reason shown),
    <i>reconciling</i> (checking what the controller actually did after a
    restart or an uncertain command).</p>
    <p><b class="k">Requested vs planned</b> — "Requested" is the start time
    you asked for. "Planned" is what the compiler produced after serializing
    zones one-at-a-time, adding inter-zone gaps, and applying policies. If
    three zones all request 9:00, only the first is planned at 9:00 — the
    others are planned to follow it. The same rule holds between whole
    programs: a run whose requested start lands while another run still owns
    the controller (soak waits included — the controller stays reserved
    through them) is planned after that run's block ends. The Planned column
    is the "you have to wait N minutes" answer, shown before it happens;
    whether a long wait runs late or skips is that program's <i>Missed
    run</i> policy. The panel never blurs the two.</p>
    <p><b class="k">Schedule timeline</b> — one row per day for the coming
    week, on a shared hour axis. Each colored bar is one zone cycle (color =
    zone, see the legend); hover any bar for the program, zone, exact window
    and runtime. A dashed line marks "now" on today's row, and a ⚠️ next to a
    day means a program that day will not water at all. The exact
    requested-vs-planned times live in the collapsible table underneath.</p>
    <p><b class="k">Runtime figures</b> — the whole-minute value actually sent
    to the controller, with the exact pre-quantization figure beside it
    (e.g. "14 min (14.4 exact)").</p>
    <p><b class="k">Buttons</b> — <i>Stop controller</i> halts ALL watering
    (even a run started from the Rain Bird app). <i>Pause</i> stops the
    controller but keeps the run resumable; <i>Resume</i> continues from the
    interrupted zone with remaining minutes recomputed. <i>Skip current zone</i>
    ends the active zone and moves to the next planned step.
    <i>Recalculate</i> recompiles the preview from current inputs.</p>
    <p><b class="k">Status flags</b> — <i>external watering</i> means the
    controller reports a zone on that this scheduler did not start (someone
    used the app or the dial). <i>native schedule conflict</i> means the
    controller's own onboard program is also active — two schedulers fighting
    over one valve stack. <i>source unavailable</i> means the core rainbird
    integration's entities are unavailable, so the scheduler cannot observe or
    command anything.</p>`,

  programs: `
    <p class="scen">Say Jonny wants the lawn watered Monday, Wednesday and
    Saturday at 6 AM, but the vegetable garden every 2nd day at 7 PM. That is
    two programs: each card here is one program — its own days, start times,
    zone list and policies.</p>
    <p><b class="k">Priority</b> — when two programs' plans collide on the
    clock, the lower number runs first and the other is shifted to follow it.</p>
    <p><b class="k">Run now</b> — starts a manual occurrence of the program
    immediately (it still respects rain policies and shows up in History
    tagged "manual"). <b class="k">Disable</b> keeps the program but stops
    scheduling it. <b class="k">Duplicate</b> copies it disabled, so you can
    experiment safely. <b class="k">Delete</b> is permanent.</p>
    <p>The order zones water in comes from the program's own zone list — edit
    a program to change order, per-zone runtimes and policies.</p>`,

  editor: `
    <p class="scen">Say Jonny is building "Morning Lawn": Mon/Wed/Sat, one
    6:00 AM start, three zones at 12, 8 and 15 minutes. Everything on this
    page describes <i>intent</i> — the compiler turns it into a strict
    one-zone-at-a-time plan you can preview on the Overview tab.</p>
    <p><b class="k">Priority</b> — tie-breaker against other programs; lower
    runs first when plans collide.</p>
    <p><b class="k">Recurrence</b> — <i>Selected weekdays</i>,
    <i>odd/even calendar days</i> (classic water-restriction schedules), or
    <i>every N days</i> anchored to a date so the rhythm survives restarts.
    Optional start/end dates bound the season. The DST rule decides what
    happens if a start time lands in a nonexistent hour on spring-forward
    night: shift to the first valid instant, or skip that one start.</p>
    <p><b class="k">Start times</b> — the whole zone list runs once per start
    time. Two starts = the full program twice that day. Each start is either
    a fixed <i>clock</i> time or <i>sunrise/sunset</i> plus an offset in
    minutes (negative = before, e.g. sunrise −45 finishes watering before the
    sun is up). Solar starts follow the season automatically using Home
    Assistant's home location; the exact resolved time for each day is shown
    on the Overview schedule. Sunrise/sunset are instants, so the DST rule
    never applies to them.</p>
    <p><b class="k">Zone rows</b> — <i>Order</i> is the watering sequence
    (one zone at a time, always). <i>Runtime override</i> replaces the zone's
    default base minutes just for this program. <i>Offset</i> shifts this
    zone's requested start by N seconds relative to the program's start time.
    <i>Max cycle / Min soak</i> override the zone's Cycle+Soak defaults from
    the Zones tab (blank = use the zone's own values).</p>
    <p><b class="k">Runtime adjustment</b> — scales every zone's base minutes
    before compiling. <i>No adjustment</i> always runs the base minutes.
    <i>Automatic seasonal</i> picks a per-month percent-of-peak curve from the
    major US city nearest your Home Assistant home location — zero
    configuration. <i>Fixed percentage I type in</i> runs every zone at that
    percent (50 → half the minutes, every run). <i>Percentage per month</i> is
    twelve percentages you edit yourself. The two <i>read from a sensor
    entity</i> options take an <b>entity ID</b> (like
    <code>sensor.watering_percent</code>) — the sensor's live state supplies
    the percentage or total minutes before each run; a plain number typed
    there will not work. Every run's math is shown, input by input, on the
    Adjustments tab.</p>
    <p><b class="k">Rain policy</b> — <i>Honor native rain delay</i>: if the
    controller's own rain delay (in DAYS) is set, the scheduler skips —
    important because Rain Bird controllers ignore their own delay for
    manual/app runs, and this scheduler's runs are exactly that kind.
    <i>Skip when sensor wet</i>: don't start while the rain sensor reports
    wet. <i>On sensor cut</i> (sensor goes wet mid-run): <i>Abort</i> ends the
    run, <i>Pause until dry</i> holds and resumes when the sensor dries,
    <i>Defer remaining</i> abandons the rest but records it as deferred.</p>
    <p><b class="k">Missed run</b> — if HA was down at start time:
    <i>Run late</i> starts anyway if still within the tolerance window
    (30 min by default), <i>Skip</i> records a skip.</p>
    <p><b class="k">External interruption</b> — someone stops the controller
    or starts an app run mid-plan: <i>Pause and resume</i> waits out the
    conflict and continues; <i>Abort</i> ends the run with that outcome.
    A run that has already begun watering resumes as long as the pause
    itself stayed within the missed-run tolerance; only a run that never
    started is held to "requested start + tolerance".</p>
    <p><b class="k">Watering window</b> — hard bounds on when steps may START.
    If a compiled step would fall outside: <i>Skip that step</i>,
    <i>Truncate the last step</i> to fit, <i>Defer the whole occurrence</i>,
    or <i>Require intervention</i> (mark a conflict and wait for you).</p>`,

  zones: `
    <p class="scen">Say Jonny's "West Yard" sits on a steep clay bank. It
    needs 24 minutes of water, but after ~3 minutes of spray the clay stops
    absorbing and water sheets downhill. The fix is Cycle+Soak: water in
    short bursts, rest between them — and that is what most of this table
    configures.</p>
    <p><b class="k">Base (min)</b> — minutes this zone needs per run in
    ideal conditions, before any adjustment provider scales it. Decimals are
    fine here; the total is quantized to whole minutes (round half up) once,
    when a plan is compiled.</p>
    <p><b class="k">Soil / Slope</b> — picking a combination fills
    <b class="k">Max cycle</b> and <b class="k">Min soak</b> with published
    starting points (Texas A&amp;M AgriLife Extension runoff guidance,
    AGEN-PU-217): clay = short cycles with a 60-minute soak, loam ≈ 9–12 min
    cycles with 40-minute soaks, sand barely needs cycling; steeper slope
    always shortens the cycle. The numbers land in the editable boxes and
    save ONLY when you press <i>Save changes</i> — nothing changes
    invisibly. Edited rows are highlighted until then, and <i>Discard</i>
    puts every row back. Those figures
    assume spray heads; low-precipitation rotor zones can run roughly 3×
    longer before runoff, so raise them if that is what the zone uses.</p>
    <p><b class="k">Max cycle</b> — longest single burst allowed. Jonny's 24
    minutes with a 3-minute max compiles to 8 bursts of 3. <b class="k">Min
    soak</b> — the mandatory rest after each burst before the SAME zone may
    water again. Other zones are free to run inside that gap; the planner
    interleaves them. Blank both for one continuous run.</p>
    <p><b class="k">Sub-minute policy</b> — what to do when adjustment shrinks
    a runtime below the controller's 1-minute resolution (e.g. 1 min × 40% =
    0.4): <i>Skip with warning</i>, <i>Clamp to one minute</i> (slightly
    overwater instead of skipping), or <i>Carry forward</i> (bank the deficit
    and add it to the next run). <i>Controller default</i> defers to the
    global setting.</p>
    <p><b class="k">On</b> — an off zone is excluded from every program with
    a structured "zone disabled" skip reason, but keeps its configuration.</p>`,

  adjustments: `
    <p class="scen">Say Jonny set a monthly curve that waters 60% in October.
    West Yard's base is 24 min, so the math is 24 × 60% = 14.4 exact →
    commanded 14. This page shows that arithmetic for every zone of every
    upcoming run — no black box, every input timestamped.</p>
    <p><b class="k">Base</b> — the zone's configured minutes (or the
    program's override). <b class="k">Factor</b> — the percentage the
    provider produced for this run. <b class="k">Exact</b> — base × factor
    before rounding. <b class="k">Commanded</b> — the whole minutes actually
    sent (quantized once, round half up; Cycle+Soak splits happen AFTER this
    total is fixed, so bursts always sum exactly to it).</p>
    <p><b class="k">Inputs / stale</b> — entity-driven providers record which
    entities they read and when. If an input hadn't updated recently, it is
    flagged <i>stale</i> here and the provider falls back per its
    configuration rather than silently trusting old data.</p>
    <p>The explanation lines under each zone are the provider's own working —
    the same provenance is stored with the run in History.</p>
    <p>This page is deliberately <b class="k">read-only</b>: it shows the
    arithmetic, it doesn't change it. To change how runtimes are adjusted,
    edit the program (Programs tab → Edit → Runtime adjustment). Pick
    <i>Automatic seasonal</i> there to have runtimes track the season using
    the curve for the nearest major US city — no numbers to maintain.</p>`,

  settings: `
    <p class="scen">Say Jonny wants watering to stop when it is near freezing.
    His Rain Bird WR2 sensor already cuts on freeze, but at a dial on the
    receiver he cannot see, and it reports rain and freeze on the same wire —
    so the scheduler cannot tell them apart. This page adds a software freeze
    guard with a threshold he sets and can see.</p>
    <p><b class="k">Why a temperature source</b> — the Rain Bird LNK module
    exposes only one "sensor active" boolean and a rain delay in days. It has
    no temperature reading and no adjustable threshold. So the freeze guard
    reads a temperature <i>entity</i> you choose — a <code>weather.*</code>
    entity from any weather integration, or a <code>sensor.*</code>
    temperature. No extra hardware needed if you already have a weather
    integration.</p>
    <p><b class="k">What the guard adds over the WR2</b> — a threshold you can
    see and change; <i>pre-emptive</i> skips (a run scheduled while it is
    already below threshold is skipped before it starts, which a real-time
    hardware trip cannot do); and correct rain-vs-freeze labeling, since with a
    temperature reading the panel can tell which one tripped the shared
    sensor.</p>
    <p><b class="k">Skip below</b> — the threshold, in your chosen unit.
    <b class="k">When temperature is unknown</b> — if the source is
    unavailable or hasn't updated in an hour, either water normally (the
    default; a missed freeze wastes water, a false block can kill a lawn) or
    block until a fresh reading returns. A paused run resumes only once the
    temperature climbs a full degree Celsius back above the threshold, so it
    does not flap around the setpoint.</p>
    <p><b class="k">Rain sensor</b> — the Rain Bird sensor is discovered
    automatically. Override it only to point rain skips at a different binary
    sensor; your override is kept even when the integration re-scans its
    entities. Per-program behavior (skip, pause, abort) lives on each
    program's <i>Rain policy</i> and <i>Freeze policy</i> sections.</p>`,

  history: `
    <p class="scen">Say the 6 AM run looked short this morning. Click the run
    row: the zone detail shows planned vs actual start and end for every
    burst, what was commanded, and a classified reason for anything odd.</p>
    <p><b class="k">Outcomes</b> — <i>completed</i>;
    <i>completed_with_skips</i> (finished, but some zones were skipped —
    reasons in the detail); <i>aborted_*</i> (rain sensor cut, external stop,
    your Stop button, power loss); <i>failed</i> (commands did not take).</p>
    <p><b class="k">Requested vs actual start</b> — the gap between them is
    normal serialization (zones queue one at a time) or a policy delay; large
    gaps carry a reason.</p>
    <p><b class="k">Retries / uncertain</b> — each controller command retries
    with backoff; "uncertain" counts commands whose delivery could not be
    confirmed (the connection dropped mid-command). The executor treats those
    carefully on restart — it observes before re-commanding, so a zone is
    never started twice.</p>
    <p><b class="k">Failures &amp; interventions</b> — the bounded log of
    everything that needed (or may need) a human: repeated command failures,
    conflicts left in "require intervention" state, and similar.</p>
    <p><b class="k">Reading collision failures</b> — three kinds point at a
    second schedule fighting this one. <i>external_watering</i>: zones turned
    on while no scheduler run was active (with timestamps — compare them to
    the controller's native program start times). <i>controller_overrun</i>:
    a zone this scheduler started stayed on past its commanded end, freshly
    re-confirmed twice, so the controller was stopped and the run failed.
    <i>external_stop</i>: a zone this scheduler started turned off early and
    the stop was confirmed by a fresh read. All three usually mean the
    controller's own internal programs (or the Rain Bird app, or another
    automation) are still running — check the banner on Overview.</p>
    <p>This table is the authoritative record (a bounded store independent of
    HA's recorder). The lifecycle <i>event entity</i> mirrors it for
    automations and Logbook, but this page is the source of truth.</p>`,

  diagnostics: `
    <p class="scen">Filing a bug or checking why something planned oddly?
    This JSON is the whole picture: config, compiled plan, executor state,
    journal, and recent decisions — with secrets and tokens redacted, so it
    is safe to paste into a GitHub issue.</p>
    <p>The same payload is attached when you download diagnostics from the
    integration page (Settings → Devices &amp; Services → Rain Bird
    Scheduler). Notes at the top flag known oddities the integration itself
    detected (stale sources, clock skew, journal recoveries).</p>`,
};

const helpBlock = (key, title) => `
  <details class="help"><summary>How this page works — ${title}</summary>
    <div>${HELP[key]}</div></details>`;

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
        font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
        /* Categorical zone palette (validated, fixed slot order) + chart ink. */
        --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100;
        --s5:#e87ba4; --s6:#008300; --s7:#4a3aa7; --s8:#e34948;
        --tl-grid:#e1e0d9; --tl-muted:#898781; --tl-ink2:#52514e; }
      :host([dark]) {
        --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500;
        --s5:#d55181; --s6:#008300; --s7:#9085e9; --s8:#e66767;
        --tl-grid:#2c2c2a; --tl-ink2:#c3c2b7; }
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
      .banner.warn { background:#fff3e0; border-left:4px solid #ef6c00;
        color:#5d4037; border-radius:8px; padding:10px 14px; margin:0 0 14px;
        line-height:1.5; font-size:13px; }
      .banner.warn b { color:#e65100; display:block; margin-bottom:4px; }
      .tl-card { position:relative; padding:14px 16px 10px; }
      .tl-grid { stroke:var(--tl-grid); stroke-width:1; }
      .tl-tick { fill:var(--tl-muted); font-size:11px; }
      .tl-day { fill:var(--tl-ink2); font-size:12px; }
      .tl-day.today { fill:var(--primary-text-color,#212121); font-weight:600; }
      .tl-badge { font-size:11px; cursor:help; }
      .tl-empty { fill:var(--tl-muted); font-size:11px; font-style:italic; }
      .tl-bar { stroke:var(--card-background-color,#fff); stroke-width:1; }
      .tl-bar:hover { filter:brightness(1.15); }
      /* Already-elapsed cycles keep their zone hue but read as dimmer/duller
       * so the eye lands on what is still to come. */
      .tl-bar.past { opacity:.3; }
      .tl-bar.past:hover { opacity:.5; filter:none; }
      .tl-now { stroke:var(--tl-muted); stroke-width:1; stroke-dasharray:3 3; }
      .tl-nowlabel { fill:var(--tl-muted); font-size:10px; }
      .tl-legend { display:flex; flex-wrap:wrap; gap:4px 16px; padding:8px 4px 2px;
        font-size:12px; color:var(--tl-ink2); }
      .tl-key { display:inline-flex; align-items:center; gap:6px; }
      .tl-swatch { width:12px; height:12px; border-radius:3px; display:inline-block; }
      .tl-tip { position:fixed; z-index:20; display:none; pointer-events:none;
        background:var(--card-background-color,#fff); color:var(--primary-text-color,#212121);
        border:1px solid var(--divider-color,#e0e0e0); border-radius:8px;
        box-shadow:0 2px 10px rgba(0,0,0,.18); padding:6px 10px; font-size:12px;
        line-height:1.5; white-space:pre-line; max-width:320px; }
      .tview { margin-top:10px; }
      .tview summary { cursor:pointer; font-size:13px; padding:6px 2px;
        color:var(--secondary-text-color,#666); user-select:none; }
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
      .help { background:var(--card-background-color,#fff); border-radius:10px;
        box-shadow:var(--ha-card-box-shadow,0 1px 3px rgba(0,0,0,.12));
        margin:0 0 14px; font-size:13px; }
      .help summary { cursor:pointer; padding:10px 14px; font-weight:500;
        color:var(--primary-color,#03a9f4); user-select:none; }
      .help > div { padding:0 16px 12px; line-height:1.55; max-width:760px; }
      .help p { margin:7px 0; }
      .help b.k { color:var(--primary-color,#0288d1); }
      .help .scen { font-style:italic; color:var(--secondary-text-color,#666); }
      .suggest-hint { font-size:12px; margin-top:8px; padding:8px 12px;
        border-left:3px solid var(--primary-color,#03a9f4); border-radius:4px;
        background:var(--secondary-background-color,#f0f7fa);
        color:var(--primary-text-color,#212121); }
      .suggest-hint:empty { display:none; }
      .btn:disabled { opacity:.45; cursor:default; }
      tr.dirty td { background: rgba(3,169,244,.08); }
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
    this.toggleAttribute("dark", !!this._hass?.themes?.darkMode);
    const tabs = [
      ["overview", "Overview"],
      ["programs", "Programs"],
      ["zones", "Zones"],
      ["adjustments", "Adjustments"],
      ["settings", "Settings"],
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
    else if (this._tab === "settings") body = this._renderSettings();
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
        this._zoneEdits = {};
        this._suggestHint = "";
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
    root.querySelectorAll("[data-z]").forEach((input) =>
      input.addEventListener("change", () => this._recordZoneEdit(input)),
    );
    this._wireTimelineTooltip();
    if (this._draft) this._wireEditor();
  }

  _wireTimelineTooltip() {
    const root = this.shadowRoot;
    const svg = root.getElementById("tl-svg");
    const tip = root.getElementById("tl-tip");
    if (!svg || !tip) return;
    svg.addEventListener("mousemove", (event) => {
      const bar = event.target.closest("[data-tip]");
      if (!bar) {
        tip.style.display = "none";
        return;
      }
      tip.textContent = bar.dataset.tip;
      tip.style.display = "block";
      const pad = 12;
      const box = tip.getBoundingClientRect();
      let left = event.clientX + pad;
      let top = event.clientY + pad;
      if (left + box.width > window.innerWidth - 8)
        left = event.clientX - box.width - pad;
      if (top + box.height > window.innerHeight - 8)
        top = event.clientY - box.height - pad;
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
    });
    svg.addEventListener("mouseleave", () => {
      tip.style.display = "none";
    });
  }

  /* Zones-tab edits accumulate in _zoneEdits (zone_id → {field: raw value})
   * so live state pushes can re-render without eating unsaved work. A field
   * set back to its stored value un-dirties itself. */
  _setZoneEdit(zoneId, field, value) {
    const zone = this._config.zones[zoneId];
    if (!zone) return;
    const edits = ((this._zoneEdits ??= {})[zoneId] ??= {});
    const original =
      typeof value === "boolean"
        ? !!zone[field]
        : String(zone[field] ?? "");
    if (value === original) delete edits[field];
    else edits[field] = value;
    if (!Object.keys(edits).length) delete this._zoneEdits[zoneId];
  }

  _recordZoneEdit(input) {
    const zoneId = input.dataset.z;
    const field = input.dataset.f;
    this._setZoneEdit(
      zoneId,
      field,
      input.type === "checkbox" ? input.checked : input.value,
    );
    if (field === "soil_type" || field === "slope_class")
      this._suggestCycleSoak(zoneId);
    this.render();
  }

  /* Put the published Cycle+Soak starting point for the zone's soil+slope
   * into the edit buffer (plan §16: suggestions, never invisible behavior —
   * the values appear in the boxes and persist only via Save). */
  _suggestCycleSoak(zoneId) {
    const zone = this._config.zones[zoneId];
    if (!zone) return;
    const edits = (this._zoneEdits ?? {})[zoneId] || {};
    const soil = edits.soil_type ?? zone.soil_type;
    const slope = edits.slope_class ?? zone.slope_class;
    const name = zone.display_name || `station ${zoneId}`;
    if (!soil || soil === "unknown") {
      this._suggestHint = `${name}: pick a soil type to get a Cycle+Soak starting point.`;
      return;
    }
    const suggestion = CYCLE_SOAK_SUGGESTIONS[`${soil}|${slope}`];
    if (!suggestion) {
      this._suggestHint =
        `${name}: sand on flat ground usually absorbs water as fast as ` +
        `sprinklers apply it — Cycle+Soak is rarely needed. Leave Max ` +
        `cycle and Min soak blank unless you actually see runoff.`;
      return;
    }
    this._setZoneEdit(zoneId, "max_cycle_minutes", String(suggestion.cycle));
    this._setZoneEdit(zoneId, "minimum_soak_minutes", String(suggestion.soak));
    this._suggestHint =
      `${name}: suggested ${suggestion.cycle} min max cycle / ` +
      `${suggestion.soak} min soak for ${soil} + ${slope} slope ` +
      `(Texas A&M AgriLife spray-head guidance; rotor zones can run ~3× ` +
      `longer). Adjust freely, then press Save changes.`;
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

  /* Zone → categorical color slot, fixed by station order so a zone keeps
   * its color across renders, filters, and program edits (color follows the
   * entity, never its position in today's plan). 8 slots, then wrap. */
  _zoneSlots() {
    const zones = Object.values(this._config?.zones || {})
      .slice()
      .sort(
        (a, b) => a.reference.station_number - b.reference.station_number,
      );
    const slots = {};
    zones.forEach((zone, index) => (slots[zone.id] = index % 8));
    return slots;
  }

  /* Gantt-style schedule: one row per day for the 7-day horizon, bars are
   * compiled zone cycles, colored per zone, on a shared hour axis so the
   * daily watering rhythm reads at a glance. SVG built as strings — no
   * dependencies, CSP-safe, theme-aware via CSS custom properties. */
  _renderTimeline() {
    const runs = this._timeline?.runs || [];
    const slots = this._zoneSlots();
    const items = [];
    for (const run of runs) {
      for (const step of run.steps || []) {
        items.push({
          run,
          step,
          start: new Date(step.planned_start_utc),
          end: new Date(step.planned_end_utc),
        });
      }
    }
    if (!items.length)
      return '<div class="card muted">Nothing scheduled in the next 7 days</div>';

    const now = new Date();
    const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const days = Array.from({ length: 7 }, (_, index) => {
      const date = new Date(day0);
      date.setDate(day0.getDate() + index);
      return date;
    });
    const dayKey = (date) =>
      `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
    const byDay = new Map(days.map((date) => [dayKey(date), []]));
    for (const item of items) {
      const key = dayKey(item.start);
      if (byDay.has(key)) byDay.get(key).push(item);
    }

    // Fully-skipped occurrences (zero steps) become a day-row warning badge.
    const badges = new Map();
    for (const run of runs) {
      if ((run.steps || []).length || !(run.skipped_zones || []).length)
        continue;
      const key = dayKey(new Date(run.requested_start_utc));
      badges.set(
        key,
        [...(badges.get(key) || []), `${run.program_name}: will not water`],
      );
    }

    // Shared hour domain across all rows so days align vertically.
    const sameDay = (a, b) => dayKey(a) === dayKey(b);
    const hourOf = (date) =>
      date.getHours() + date.getMinutes() / 60 + date.getSeconds() / 3600;
    let lo = 24;
    let hi = 0;
    for (const { start, end } of items) {
      lo = Math.min(lo, hourOf(start));
      hi = Math.max(hi, sameDay(start, end) ? hourOf(end) : 24);
    }
    lo = Math.max(0, Math.floor(lo) - 1);
    hi = Math.min(24, Math.ceil(hi) + 1);
    while (hi - lo < 4) {
      if (lo > 0) lo -= 1;
      if (hi < 24 && hi - lo < 4) hi += 1;
      if (lo === 0 && hi === 24) break;
    }
    const span = hi - lo;
    const tickStep = span <= 8 ? 1 : span <= 14 ? 2 : 3;

    const W = 980;
    const GL = 100;
    const GR = 14;
    const AX = 22;
    const ROW = 30;
    const BAR = 16;
    const H = AX + days.length * ROW + 6;
    const xAt = (hour) => GL + ((hour - lo) / span) * (W - GL - GR);
    const fmtHour = (hour) =>
      new Date(2000, 0, 1, hour).toLocaleTimeString([], { hour: "numeric" });
    const fmtShort = (date) =>
      date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

    let grid = "";
    for (let hour = Math.ceil(lo); hour <= hi; hour += tickStep) {
      const gx = xAt(hour);
      grid += `<line x1="${gx}" y1="${AX - 4}" x2="${gx}" y2="${H - 4}" class="tl-grid"/>
        <text x="${gx}" y="${AX - 9}" class="tl-tick" text-anchor="middle">${esc(fmtHour(hour))}</text>`;
    }

    let rows = "";
    days.forEach((date, index) => {
      const y = AX + index * ROW;
      const key = dayKey(date);
      const dayItems = byDay.get(key) || [];
      const isToday = index === 0;
      const label = date.toLocaleDateString([], {
        weekday: "short",
        month: "short",
        day: "numeric",
      });
      rows += `<text x="${GL - 10}" y="${y + ROW / 2 + 4}" text-anchor="end"
        class="tl-day ${isToday ? "today" : ""}">${esc(label)}</text>`;
      if (badges.has(key)) {
        rows += `<text x="${GL - 10}" y="${y + ROW - 2}" text-anchor="end" class="tl-badge">
          <title>${esc(badges.get(key).join("\n"))}</title>⚠️</text>`;
      }
      if (!dayItems.length) {
        rows += `<text x="${GL + 8}" y="${y + ROW / 2 + 4}" class="tl-empty">no watering</text>`;
        return;
      }
      for (const { run, step, start, end } of dayItems) {
        const x1 = xAt(Math.max(lo, hourOf(start)));
        const x2 = sameDay(start, end)
          ? xAt(Math.min(hi, hourOf(end)))
          : xAt(hi);
        const width = Math.max(5, x2 - x1);
        const slot = (slots[step.zone_id] ?? 0) + 1;
        const cycle =
          step.cycle_count > 1
            ? ` · cycle ${step.cycle_index}/${step.cycle_count}`
            : "";
        const past = end < now;
        const tip =
          `${run.program_name} — ${step.zone_name}${cycle}\n` +
          `${fmtShort(start)}–${fmtShort(end)} · ${step.duration_minutes} min` +
          ` (${step.exact_minutes} exact)` +
          (past ? "\nAlready ran" : "");
        rows += `<rect x="${x1}" y="${y + (ROW - BAR) / 2}" width="${width}" height="${BAR}"
          rx="2" class="tl-bar${past ? " past" : ""}" style="fill:var(--s${slot})" data-tip="${esc(tip)}">
          <title>${esc(tip)}</title></rect>`;
      }
    });

    let nowLine = "";
    const nowHour = hourOf(now);
    if (nowHour >= lo && nowHour <= hi) {
      const nx = xAt(nowHour);
      nowLine = `<line x1="${nx}" y1="${AX}" x2="${nx}" y2="${AX + ROW}" class="tl-now"/>
        <text x="${nx}" y="${AX + ROW + 9}" class="tl-nowlabel" text-anchor="middle">now</text>`;
    }

    // Legend: zones that appear in the plan, in slot (station) order.
    const seen = new Map();
    for (const { step } of items)
      if (!seen.has(step.zone_id)) seen.set(step.zone_id, step.zone_name);
    const legend = [...seen.entries()]
      .sort((a, b) => (slots[a[0]] ?? 0) - (slots[b[0]] ?? 0))
      .map(
        ([zoneId, name]) =>
          `<span class="tl-key"><span class="tl-swatch"
            style="background:var(--s${(slots[zoneId] ?? 0) + 1})"></span>${esc(name)}</span>`,
      )
      .join("");

    return `
      <div class="card tl-card">
        <svg id="tl-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet"
          style="width:100%;height:auto;display:block" role="img"
          aria-label="Watering schedule for the next 7 days">
          ${grid}${nowLine}${rows}
        </svg>
        <div class="tl-legend">${legend}</div>
        <div id="tl-tip" class="tl-tip"></div>
      </div>`;
  }

  /* current_temperature_c arrives as a Decimal string in Celsius; render it
   * in the guard's configured unit. */
  _fmtTemp(celsius, unit) {
    const c = Number(celsius);
    if (Number.isNaN(c)) return "—";
    const value = unit === "°F" ? c * 1.8 + 32 : c;
    return `${value.toFixed(1)} ${unit}`;
  }

  _fmtThreshold(guard) {
    const t = Number(guard.threshold ?? 0);
    return `${t.toFixed(1)} ${guard.unit || "°C"}`;
  }

  /* One label per distinct cause: "unknown" alone hides whether there is
   * no entity to read, the source is down, or it just hasn't reported. */
  _temperatureLabel(tempC, observation, guard) {
    if (tempC != null) return this._fmtTemp(tempC, guard.unit || "°C");
    switch (observation?.temperature_status) {
      case "no_entity":
        // Nothing configured: a quiet dash unless the guard needs one.
        return guard.enabled ? "no temperature source" : "—";
      case "unavailable":
        return "source unavailable";
      case "no_value":
        return "no reading";
      case "invalid":
        return "unreadable value";
      case "stale":
        return "stale (ignored)";
      default:
        // Pre-upgrade payloads: keep the old wording.
        return observation?.temperature_stale
          ? "unknown (stale)"
          : guard.enabled
            ? "unknown"
            : "—";
    }
  }

  _rainDelayLabel(days, status) {
    if (days != null) return `${days} day${days === 1 ? "" : "s"}`;
    switch (status) {
      case "no_entity":
        return "no delay entity found";
      case "unavailable":
        return "source unavailable";
      case "not_yet_read":
        return "not read yet";
      case "invalid":
        return "unreadable value";
      default:
        return "unknown"; // pre-upgrade observation payloads
    }
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

    const upcoming = this._upcomingSteps(100)
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

    const cancelled = (this._timeline?.warnings || [])
      .map((warning) => `<div>⚠️ ${esc(warning.message)}</div>`)
      .join("");

    const guard = controller.freeze_guard || {};
    const tempC = observation?.current_temperature_c;
    const tempLabel = this._temperatureLabel(tempC, observation, guard);
    // The Rain Bird boolean covers rain AND freeze; name which one when we can.
    const sensorLabel =
      observation?.rain_sensor_active == null
        ? "No sensor"
        : observation.rain_sensor_active
          ? state.freeze_active
            ? "Likely freeze"
            : "Sensor WET"
          : "Sensor dry";

    return `
      ${helpBlock("overview", "live state and the compiled plan")}
      ${
        state.native_schedule_conflict
          ? `<div class="banner warn">
              <b>The controller's own schedule is still active</b>
              <div>The Rain Bird controller has internal programs of its own
                (its native calendar shows scheduled events) while this
                scheduler owns automatic watering. Two schedules driving the
                same valves causes exactly the failures this page reports:
                zones staying on past their commanded end
                (<i>controller_overrun</i>), zones stopping early
                (<i>external_stop</i>), and runs pausing or skipping for
                "external activity".</div>
              <div class="sub">Fix: clear or disable the programs on the
                controller itself (or in the Rain Bird app), or switch the
                authority mode in Settings if you want the controller to stay
                in charge. Watch History → Failures &amp; interventions for
                "external watering" entries to confirm when the native
                schedule runs.</div>
            </div>`
          : ""
      }
      ${
        state.external_watering
          ? `<div class="banner warn">
              <b>External watering is active right now</b>
              <div>A zone is on that this scheduler did not start — a native
                Rain Bird program, the app, or another automation is driving
                the controller.</div>
            </div>`
          : ""
      }
      ${
        cancelled
          ? `<div class="banner warn">
              <b>Scheduled watering will not happen</b>
              ${cancelled}
              <div class="sub">Skip reasons are listed under "Planned skips"
                below; the per-zone math is on the Adjustments tab.</div>
            </div>`
          : ""
      }
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
        <div class="card"><h3>Weather</h3>
          <div class="big">${esc(sensorLabel)}${
            state.freeze_active
              ? ' <span class="chip bad">freeze</span>'
              : ""
          }</div>
          <div class="sub">Temperature: <b>${esc(tempLabel)}</b>${
            guard.enabled
              ? ` · freeze threshold ${esc(this._fmtThreshold(guard))}`
              : ""
          }</div>
          <div class="sub">Native Rain Bird rain delay:
            <b>${esc(this._rainDelayLabel(rainDelay, observation?.rain_delay_status))}</b></div>
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

      <div class="section">Schedule (next 7 days)</div>
      ${this._renderTimeline()}
      <details class="tview">
        <summary>Table view (exact requested vs planned times)</summary>
        <table>
          <tr><th>Program</th><th>Zone</th><th>Requested</th><th>Planned start</th>
          <th>Planned end</th><th>Runtime</th></tr>
          ${upcoming || '<tr><td colspan="6" class="muted">Nothing scheduled in the next 7 days</td></tr>'}
        </table>
      </details>
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
      ${helpBlock("programs", "programs are watering intent")}
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
      nominal_start_times: [
        { kind: "clock", at: "06:00:00", offset_minutes: 0 },
      ],
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
      freeze_policy: {
        skip_when_freezing: true,
        freeze_cut_behavior: "abort_run",
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
      .map((raw, index) => {
        const start = normalizeStart(raw);
        const kindSelect = `
          <select data-start-kind="${index}">
            <option value="clock" ${start.kind === "clock" ? "selected" : ""}>Clock</option>
            <option value="sunrise" ${start.kind === "sunrise" ? "selected" : ""}>Sunrise</option>
            <option value="sunset" ${start.kind === "sunset" ? "selected" : ""}>Sunset</option>
          </select>`;
        const valueInput =
          start.kind === "clock"
            ? `<input type="time" data-start-index="${index}"
                value="${(start.at || "06:00:00").slice(0, 5)}">`
            : `<input type="number" data-start-offset="${index}"
                value="${start.offset_minutes}" min="-600" max="600" step="5"
                style="width:72px" title="Minutes relative to ${start.kind};
                negative starts before it"><span class="muted">min</span>`;
        return `
        <span class="row" style="display:inline-flex;align-items:center;gap:4px">
          ${kindSelect}
          ${valueInput}
          <button class="btn small warn" data-action="start-remove" data-index="${index}">✕</button>
        </span>`;
      })
      .join("");

    const rec = draft.recurrence;
    const provider = draft.adjustment_provider;
    const window_ = draft.watering_window;

    return `
      ${helpBlock("editor", "every field on this form")}
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
            Percent of base runtime
            <input id="f-percent" type="number" min="0" max="300" value="${provider.percent ?? 100}"></label>
          <label class="f" id="f-entity-wrap" style="${provider.kind.startsWith("entity") ? "" : "display:none"}">
            Entity ID (not a number)
            <input id="f-entity" placeholder="sensor.example" value="${esc(provider.entity_id ?? "")}"></label>
        </div>
        <div class="sub" id="f-percent-hint" style="${provider.kind === "manual_percent" ? "" : "display:none"}">
          Every zone runs at this percent of its base minutes on every run —
          e.g. 50 turns a 10-minute zone into 5 minutes.
        </div>
        <div class="sub" id="f-entity-hint" style="${provider.kind.startsWith("entity") ? "" : "display:none"}">
          This is the ID of a Home Assistant entity (like
          <code>sensor.watering_percent</code>) whose live state supplies the
          ${provider.kind === "entity_runtime" ? "total minutes" : "percentage"}
          before each run. To use one fixed percentage instead, choose
          "Fixed percentage I type in" above.
        </div>
        <div class="sub" id="f-seasonal-hint" style="${provider.kind === "seasonal_auto" ? "" : "display:none"}">
          Scales base runtimes by month using a published percent-of-peak
          curve for the major US city nearest your Home Assistant home
          location. Base runtime = peak-season minutes. The chosen city and
          each month's percentage appear on the Adjustments tab.
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

        <div class="section">Freeze policy</div>
        <div class="explain">Uses the temperature source and threshold set on
          the Settings tab; here you choose whether this program obeys it.</div>
        <div class="row">
          <label><input id="f-skip-freeze" type="checkbox" ${draft.freeze_policy?.skip_when_freezing ? "checked" : ""}>
            Skip when below the freeze threshold</label>
          <label class="f">On freeze during a run
            <select id="f-freeze-cut">${FREEZE_CUT.map(
              ([value, label]) =>
                `<option value="${value}" ${draft.freeze_policy?.freeze_cut_behavior === value ? "selected" : ""}>${label}</option>`,
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
        // Override fields accept null (meaning "no override"); the rest
        // are non-nullable ints on the backend, so a cleared input
        // reverts to the current value instead of storing null.
        const nullable = [
          "base_runtime_override_minutes",
          "max_cycle_minutes_override",
          "minimum_soak_minutes_override",
        ];
        if (input.type === "checkbox") step[field] = input.checked;
        else if (input.value === "") {
          if (nullable.includes(field)) step[field] = null;
          else input.value = step[field];
        } else if (
          nullable.includes(field) &&
          field !== "minimum_soak_minutes_override" &&
          Number(input.value) === 0
        ) {
          // Spinner misclick: a 0-minute override waters nothing, and a 0
          // max cycle means "no cycling" — both are really "no override".
          step[field] = null;
          input.value = "";
          this._toastMsg(
            "A 0 override would water nothing — cleared. Untick On to skip a zone.",
          );
        } else if (field === "zone_id") step[field] = input.value;
        else if (field === "base_runtime_override_minutes")
          step[field] = String(input.value);
        else step[field] = Number(input.value);
      }),
    );
    root.querySelectorAll("[data-start-index]").forEach((input) =>
      input.addEventListener("change", () => {
        this._draft.nominal_start_times[Number(input.dataset.startIndex)] = {
          kind: "clock",
          at: `${input.value}:00`,
          offset_minutes: 0,
        };
      }),
    );
    root.querySelectorAll("[data-start-kind]").forEach((select) =>
      select.addEventListener("change", () => {
        const index = Number(select.dataset.startKind);
        const current = normalizeStart(this._draft.nominal_start_times[index]);
        this._draft.nominal_start_times[index] =
          select.value === "clock"
            ? { kind: "clock", at: current.at || "06:00:00", offset_minutes: 0 }
            : { kind: select.value, at: null, offset_minutes: 0 };
        // The value input switches between a time and an offset field.
        this.render();
      }),
    );
    root.querySelectorAll("[data-start-offset]").forEach((input) =>
      input.addEventListener("change", () => {
        const index = Number(input.dataset.startOffset);
        const current = normalizeStart(this._draft.nominal_start_times[index]);
        const clamped = Math.max(-600, Math.min(600, Number(input.value) || 0));
        this._draft.nominal_start_times[index] = {
          kind: current.kind,
          at: null,
          offset_minutes: clamped,
        };
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
    draft.freeze_policy = {
      skip_when_freezing: checked("f-skip-freeze"),
      freeze_cut_behavior: value("f-freeze-cut") || "abort_run",
    };
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
    const provider = draft.adjustment_provider;
    if (provider.kind?.startsWith("entity")) {
      const entity = provider.entity_id || "";
      if (!/^[a-z_]+\.[a-z0-9_]+$/i.test(entity)) {
        this._toastMsg(
          /^\d+(\.\d+)?$/.test(entity)
            ? `"${entity}" is a number, but this provider reads a sensor — ` +
              `it needs an entity ID like sensor.watering_percent. ` +
              `For a fixed ${entity}%, pick "Fixed percentage I type in".`
            : "This provider reads a sensor: enter an entity ID like " +
              "sensor.watering_percent (or pick a different provider).",
        );
        return;
      }
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
    const allEdits = this._zoneEdits ?? {};
    const rows = zones
      .map((zone) => {
        const edits = allEdits[zone.id] || {};
        const val = (field) => esc(edits[field] ?? zone[field] ?? "");
        const on = edits.enabled ?? zone.enabled;
        const soilNow = edits.soil_type ?? zone.soil_type;
        const slopeNow = edits.slope_class ?? zone.slope_class;
        const policyNow =
          edits.minimum_runtime_policy ?? zone.minimum_runtime_policy ?? "";
        return `
      <tr data-zone-row="${zone.id}" class="${Object.keys(edits).length ? "dirty" : ""}">
        <td>${zone.reference.station_number}</td>
        <td><input data-z="${zone.id}" data-f="display_name" value="${val("display_name")}"></td>
        <td class="muted">${esc(zone.reference.last_known_entity_id)}</td>
        <td><input type="checkbox" data-z="${zone.id}" data-f="enabled" ${on ? "checked" : ""}></td>
        <td><input type="number" style="width:70px" step="0.5" min="0"
          data-z="${zone.id}" data-f="base_runtime_minutes" value="${val("base_runtime_minutes")}"></td>
        <td><select data-z="${zone.id}" data-f="soil_type">${SOILS.map(
          (soil) =>
            `<option value="${soil}" ${soilNow === soil ? "selected" : ""}>${soil}</option>`,
        ).join("")}</select></td>
        <td><select data-z="${zone.id}" data-f="slope_class">${SLOPES.map(
          (slope) =>
            `<option value="${slope}" ${slopeNow === slope ? "selected" : ""}>${slope}</option>`,
        ).join("")}</select></td>
        <td><input type="number" style="width:60px" min="0" placeholder="—"
          data-z="${zone.id}" data-f="max_cycle_minutes" value="${val("max_cycle_minutes")}"></td>
        <td><input type="number" style="width:60px" min="0" placeholder="—"
          data-z="${zone.id}" data-f="minimum_soak_minutes" value="${val("minimum_soak_minutes")}"></td>
        <td><select data-z="${zone.id}" data-f="minimum_runtime_policy">${MIN_RUNTIME.map(
          ([value, label]) =>
            `<option value="${value}" ${policyNow === value ? "selected" : ""}>${label}</option>`,
        ).join("")}</select></td>
      </tr>`;
      })
      .join("");
    const dirty = Object.keys(allEdits).length;
    return `
      ${helpBlock("zones", "runtimes, soil and Cycle+Soak")}
      <div class="row" style="justify-content:space-between">
        <div class="sub" style="flex:1">Changing Soil or Slope fills Max cycle /
          Min soak with published starting points (Texas A&amp;M AgriLife runoff
          guidance) — visible in the boxes, editable, and applied only when you
          save. Runtimes are quantized once to whole minutes (round half up)
          when a plan is compiled.</div>
        <div>
          ${dirty ? '<button class="btn ghost" data-action="discard-zone-edits">Discard</button>' : ""}
          <button class="btn" data-action="save-zones" ${dirty ? "" : "disabled"}>
            Save changes${dirty ? ` (${dirty} zone${dirty > 1 ? "s" : ""})` : ""}</button>
        </div>
      </div>
      <table>
        <tr><th>Station</th><th>Name</th><th>Source entity</th><th>On</th>
          <th>Base (min)</th><th>Soil</th><th>Slope</th><th>Max cycle</th>
          <th>Min soak</th><th>Sub-minute policy</th></tr>
        ${rows || '<tr><td colspan="10" class="muted">No zones discovered</td></tr>'}
      </table>
      ${this._suggestHint ? `<div class="suggest-hint">${esc(this._suggestHint)}</div>` : ""}`;
  }

  async _saveZones() {
    // Only these zone fields accept null on the backend; clearing any
    // other input must not persist null (it would poison recalculation).
    const nullable = [
      "max_cycle_minutes",
      "minimum_soak_minutes",
      "minimum_runtime_policy",
    ];
    const patches = [];
    for (const [zoneId, edits] of Object.entries(this._zoneEdits ?? {})) {
      const zone = this._config.zones[zoneId];
      if (!zone) continue;
      const patch = {};
      for (const [field, raw] of Object.entries(edits)) {
        if (typeof raw === "boolean") {
          patch[field] = raw;
        } else if (raw === "" || raw == null) {
          if (!nullable.includes(field)) {
            this._toastMsg(
              `${zone.display_name}: ${field.replace(/_/g, " ")} cannot be empty.`,
            );
            return;
          }
          patch[field] = null;
        } else if (
          ["max_cycle_minutes", "minimum_soak_minutes"].includes(field)
        )
          patch[field] = Number(raw);
        else patch[field] = String(raw);
      }
      if (Object.keys(patch).length) patches.push([zoneId, patch]);
    }
    if (!patches.length) return;
    let saved = 0;
    let failed = 0;
    for (const [zoneId, patch] of patches) {
      try {
        await this.api({
          type: `${DOMAIN}/zone/update`,
          entry_id: this._entryId,
          zone_id: zoneId,
          expected_revision: this._config.zones[zoneId].revision,
          patch,
        });
        delete this._zoneEdits[zoneId];
        saved += 1;
      } catch (err) {
        failed += 1;
        if (err && err.code === "revision_conflict") await this._loadConfig();
        this._toastMsg(
          `${this._config.zones[zoneId]?.display_name || zoneId}: ` +
            `${err.message || err.code || err}`,
        );
      }
    }
    if (saved && !failed)
      this._toastMsg(`Saved ${saved} zone${saved > 1 ? "s" : ""}.`);
    await this._loadAll();
  }

  // ------------------------------------------------------------------
  // Adjustments
  // ------------------------------------------------------------------

  _renderAdjustments() {
    const runs = this._timeline?.runs || [];
    if (!runs.length)
      return `${helpBlock("adjustments", "the runtime math, shown in full")}
        <div class="card muted">No upcoming runs to explain.</div>`;
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
    return helpBlock("adjustments", "the runtime math, shown in full") + blocks.join("");
  }

  // ------------------------------------------------------------------
  // Settings (controller-level configuration)
  // ------------------------------------------------------------------

  /* <option> list for a datalist, from the live entity registry. */
  _entityOptions(predicate) {
    const states = this._hass?.states || {};
    return Object.keys(states)
      .filter(predicate)
      .sort()
      .map((id) => `<option value="${esc(id)}"></option>`)
      .join("");
  }

  _renderSettings() {
    const controller = this._config.controller;
    const guard = controller.freeze_guard || {};
    const states = this._hass?.states || {};
    const isTemp = (id) =>
      id.startsWith("weather.") ||
      (id.startsWith("sensor.") &&
        (states[id]?.attributes?.device_class === "temperature" ||
          ["°C", "°F", "K"].includes(
            states[id]?.attributes?.unit_of_measurement,
          )));
    const tempOptions = this._entityOptions(isTemp);
    const rainOptions = this._entityOptions((id) =>
      id.startsWith("binary_sensor."),
    );
    const discovered =
      controller.rain_sensor_reference?.last_known_entity_id || "—";

    return `
      ${helpBlock("settings", "connecting the rain sensor and freeze guard")}
      <div class="card">
        <div class="section" style="margin-top:0">Weather protection</div>
        <div class="explain">A software low-temperature guard. Your Rain Bird
          sensor reports only one on/off signal (rain or freeze), so an
          adjustable freeze threshold reads from a temperature entity you
          choose — a weather integration works, no extra hardware.</div>
        <div class="row">
          <label><input id="s-freeze-enabled" type="checkbox"
            ${guard.enabled ? "checked" : ""}> Enable freeze guard</label>
        </div>
        <div class="row">
          <label class="f">Temperature source (entity id)
            <input id="s-temp-entity" list="s-temp-list"
              placeholder="weather.home or sensor.outdoor_temp"
              value="${esc(guard.temperature_entity_id ?? "")}">
            <datalist id="s-temp-list">${tempOptions}</datalist></label>
          <label class="f">Skip below
            <input id="s-threshold" type="number" step="0.5"
              style="width:80px" value="${esc(guard.threshold ?? "1")}"></label>
          <label class="f">Unit
            <select id="s-unit">${TEMP_UNITS.map(
              (u) =>
                `<option value="${u}" ${(guard.unit || "°C") === u ? "selected" : ""}>${u}</option>`,
            ).join("")}</select></label>
        </div>
        <div class="row">
          <label class="f">When temperature is unknown
            <select id="s-when-unavailable">${WHEN_UNAVAILABLE.map(
              ([v, label]) =>
                `<option value="${v}" ${(guard.when_unavailable || "allow_watering") === v ? "selected" : ""}>${label}</option>`,
            ).join("")}</select></label>
        </div>

        <div class="section">Rain sensor</div>
        <div class="explain">Auto-discovered from the Rain Bird integration.
          Override only if you want a different binary sensor (e.g. a separate
          soil-moisture sensor) to drive rain skips.</div>
        <div class="row">
          <label class="f">Discovered
            <input value="${esc(discovered)}" readonly
              style="background:var(--secondary-background-color,#f0f0f0)"></label>
          <label class="f">Override (entity id, optional)
            <input id="s-rain-override" list="s-rain-list"
              placeholder="leave blank to use discovered"
              value="${esc(controller.rain_sensor_override_entity_id ?? "")}">
            <datalist id="s-rain-list">${rainOptions}</datalist></label>
        </div>

        <div class="row" style="margin-top:10px">
          <button class="btn" data-action="save-settings">Save settings</button>
        </div>
      </div>`;
  }

  async _saveSettings() {
    const root = this.shadowRoot;
    const value = (id) => root.getElementById(id)?.value;
    const checked = (id) => !!root.getElementById(id)?.checked;
    const controller = this._config.controller;
    const override = (value("s-rain-override") || "").trim();
    const tempEntity = (value("s-temp-entity") || "").trim();
    const patch = {
      freeze_guard: {
        enabled: checked("s-freeze-enabled"),
        temperature_entity_id: tempEntity || null,
        threshold: String(value("s-threshold") || "1"),
        unit: value("s-unit") || "°C",
        when_unavailable: value("s-when-unavailable") || "allow_watering",
      },
      rain_sensor_override_entity_id: override || null,
    };
    await this._action(
      this.api({
        type: `${DOMAIN}/config/update`,
        entry_id: this._entryId,
        expected_revision: controller.revision,
        patch,
      }),
      "Settings saved",
    );
    await this._loadConfig();
    this.render();
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
      ${helpBlock("history", "what actually happened, and why")}
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
      ${helpBlock("diagnostics", "the full redacted state dump")}
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
      case "save-settings":
        await this._saveSettings();
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
        this._draft.nominal_start_times.push({
          kind: "clock",
          at: "06:00:00",
          offset_minutes: 0,
        });
        this.render();
        break;
      case "start-remove":
        this._collectDraft();
        this._draft.nominal_start_times.splice(Number(data.index), 1);
        this.render();
        break;
      case "save-zones":
        await this._saveZones();
        break;
      case "discard-zone-edits":
        this._zoneEdits = {};
        this._suggestHint = "";
        this.render();
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

/* A page that survives a cache-busted reload (HA restart with the panel open)
 * would otherwise hit "name already used with this registry". */
if (!customElements.get("rainbird-scheduler-panel"))
  customElements.define("rainbird-scheduler-panel", RainBirdSchedulerPanel);

# Rain Bird Scheduler for Home Assistant

A companion **helper integration** (`rainbird_scheduler`) that adds a real irrigation scheduler on top of the core Home Assistant [`rainbird`](https://www.home-assistant.io/integrations/rainbird) integration.

> **Status: planning.** No code yet. The complete design is in [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md).

## The idea

The core `rainbird` integration can start one zone, stop the controller, and report state — but it has no scheduling. Rain Bird LNK controllers accept one request at a time and water one zone at a time. This project lets the user describe irrigation *intent*:

```text
Program: Morning Lawn
Days: Monday, Wednesday, Saturday
Requested start: 9:00 AM
Front lawn       12 min
Side lawn         8 min
Back lawn        15 min
```

All three zones may share the same requested start. The scheduler compiles a valid, deterministic, one-zone-at-a-time execution plan:

```text
Front lawn     9:00:00–9:12:00
Side lawn      9:12:05–9:20:05
Back lawn      9:20:10–9:35:10
```

The UI always distinguishes requested start, planned start, observed start, base runtime, adjusted runtime, controller-quantized runtime, and the reason for any delay, skip, or adjustment.

## What it adds

- **Programs and recurrence** — weekdays, odd/even days, every-N-days, multiple start times, watering windows, explicit DST rules.
- **Automatic serialization** — overlapping requested starts are compiled into a non-overlapping plan with deliberate inter-zone gaps.
- **Runtime adjustment** — transparent seasonal/weather providers with full calculation provenance; no proprietary black box.
- **Cycle+Soak** — quantized cycle allocation with soak-interval interleaving across zones.
- **Journaled execution** — a persisted state machine that survives Home Assistant restarts without duplicating a zone start.
- **Rain handling** — native rain delay is enforced by the scheduler (Rain Bird controllers ignore it for manual runs); rain-sensor cuts get their own classified outcome.
- **Conflict awareness** — app/manual activity detection, single-flight runs per controller, external-stop classification.
- **History and UI** — bounded authoritative run history, a lifecycle event entity, calendar preview, and a full-screen program editor panel.

## Architecture in one paragraph

The scheduler never talks to the controller. It drives irrigation exclusively through the core integration's public surface — `rainbird.start_irrigation` for exactly one zone at a time, `switch.turn_off` to stop the controller, and entity states for observation. It never opens a second `pyrainbird` connection, never stores the controller password, and never imports the core integration's runtime data. A driver interface ships in v1 so a future native-queue backend can slot in once `pyrainbird` and Home Assistant core expose stable APIs for stacked runs — with no planner or executor refactor.

## Implementation sequence

| PR | Scope |
|----|-------|
| 1 | Integration contract: config flow, zone discovery, HA-entity driver, device linkage |
| 2 | Recurrence, DST rules, pure planner, runtime quantization |
| 3 | Journaled executor, restart recovery, single-flight, event entity |
| 4 | Full-screen scheduler panel and WebSocket CRUD |
| 5 | Rain policies and adjustment providers |
| 6 | Cycle+Soak and soil profiles |
| 7 | Release hardening: repairs, diagnostics, migrations, HACS |
| 8 | Upstream native queue support (`pyrainbird` + core), capability-gated driver |
| 9 | Native schedule writing (separate, model-specific, verified page writes) |

See [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) for the full plan: domain model, executor state machine, storage and journaling design, planner invariants, WebSocket API, and the hardware test matrix.

## Planned requirements

- Home Assistant 2026.8 or newer
- The core `rainbird` integration configured with a supported LNK/LNK2 controller
- Zero runtime Python dependencies beyond Home Assistant itself

---

*This project is not affiliated with or endorsed by Rain Bird Corporation.*

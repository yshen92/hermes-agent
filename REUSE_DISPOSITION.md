# Phase 10.2A-local reuse disposition

Baseline inspected: official `v2026.8.3` / package `0.20.0` /
`3c27eb6234bf91b8ceee9e9071591b31e9b148cb`.

## Already available on v0.20

- `kanban_db.complete_task()` and `kanban_db.block_task()` are the canonical
  finalizers. They already preserve scratch artifacts, close runs, emit events,
  reset/account for failures, recompute ready tasks, clean workspaces, and fire
  lifecycle hooks. The restricted-worker path must call them, not duplicate
  them.
- Claims already bind task, run, profile, claim lock/expiry and worker PID.
  Dispatcher supervision already handles live-PID claim extension, crash,
  timeout, reclaim and retry.
- Delegated-child DB/CLI/tool mutation guards and worker own-task checks already
  exist as defense in depth. They do not protect a native same-UID worker from
  raw SQLite access.
- The existing `HERMES_BIN` dispatcher seam supports the separately reviewed
  Phase 10.1 fixed restricted Unix principal. That OS identity is the hard
  board-DB boundary; this patch does not create an account, service or sandbox.

## Selectively adapted

- PR #68029: adapt its worker/orchestrator graph-mutation separation only for
  the new restricted-worker mode. Restricted workers receive no graph mutation
  tools. The generic non-restricted policy/config switch is outside this local
  seam; OS denial of the board DB remains decisive.
- PR #63297: reuse its useful validation direction (current running task/run,
  profile, canonical workspace, live claim, non-stale ownership), but validate
  against dispatcher-owned spawn state and then enter the existing canonical
  finalizers.

## Rejected

- PR #63297's `terminal_handoffs` persistence and parallel completion/block
  SQL are rejected: they add a second persistent authority/receipt store and
  bypass artifact preservation, redaction parity, hooks and cleanup.
- Tool hiding, environment-string trust, a same-UID result file, a network
  listener, broker daemon, capability registry, second DB or per-task Unix
  accounts are rejected. None is needed when the existing dispatcher process,
  credential-authenticated child control socket and fixed restricted UID are
  combined.

## Genuinely missing

- A bounded restricted-worker result carried on a dispatcher-created Unix
  datagram socket with kernel PID/UID credentials, then applied only after
  claim-bound current-state validation.
- Restricted-worker prompt/tool behavior that returns complete/block through
  that parent seam and treats process supervision as liveness, without opening
  the board DB.

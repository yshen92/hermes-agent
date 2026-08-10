# Phase 10.2A-local upstream extraction map

## A. Generic Hermes Kanban behavior suitable for upstream

1. **Worker/orchestrator graph separation**
   - Hide create/link/comment/attachment/board-selection tools from a
     restricted task worker.
   - Keep tool/DB guards as defense in depth while documenting that only an OS
     filesystem boundary prevents raw SQLite bypass.

2. **Own-run claim-bound lifecycle handoff**
   - Emit one identity-free, size-bounded complete/block result on a
     dispatcher-owned Unix child socket authenticated with kernel PID/UID
     credentials.
   - Bind authority exclusively to dispatcher spawn state and current DB state:
     task, run, assignee/profile, exact and canonical workspace, claim lock and
     expiry, active run, and worker PID.
   - Enter the existing `complete_task` / `block_task` finalizers through an
     atomic ownership CAS. Do not add a receipt table or parallel finalizer.

3. **Dispatcher-owned liveness**
   - Extend expired claims for a live bound child PID.
   - Exempt a live restricted binding from DB-heartbeat-only false-stale
     reclaim, while retaining crash detection and per-task max-runtime bounds.

4. **Generic security/lifecycle tests**
   - Foreign/stale/forged task/run/profile/workspace/claim/PID refusal.
   - Multiple, oversized and generic mutation result refusal.
   - Direct import/CLI guard plus a deployment-level raw SQLite denial test
     under the actual restricted OS identity.
   - Canonical artifact preservation, metadata redaction, hooks, cleanup,
     typed block routing, crash/timeout/retry and R0 regressions.

## B. Local-only v2 behavior that must not be upstreamed

- The development-framework-v2 graph and controller-owned batch-state schema.
- Lane A / Lane B policy and controller `next_action` decisions.
- Review/remediation workflow and Phase 9 / 9.5 framework logic.
- VPS-specific identities, source/state paths, ACLs, sudoers rules, launcher
  bytes, profile names and operator authorization bindings.
- Pilot/cutover/rollback procedures from the Phase 10.1 deployment addendum.

The generic runtime must expose only the opt-in restricted-worker seam. A
separately reviewed local binding remains responsible for proving that its
fixed worker identity cannot traverse or write the native board database.

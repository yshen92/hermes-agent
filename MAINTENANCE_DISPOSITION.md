# Phase 10.2A-local maintenance and deletion disposition

- Keep `hermes_cli/kanban_lifecycle.py` as the small transport/format boundary.
  It has no DB or scheduler responsibility.
- Keep dispatcher bindings process-local and bounded by live child PIDs and a
  mandatory per-task maximum runtime. Do not persist, migrate or replicate
  them. The separately reviewed launcher must terminate children on dispatcher
  death; restart recovery then uses the existing crash/reclaim path.
- Keep canonical lifecycle behavior in `kanban_db.complete_task` and
  `kanban_db.block_task`; future changes to artifacts, hooks, cleanup, events or
  typed routing automatically apply to restricted workers.
- Delete the local restricted-worker seam if Hermes later gains an upstream
  equivalent with the same OS-boundary and canonical-finalizer guarantees.
  Migration is code replacement only: there is no table, receipt, token,
  daemon, socket or persistent state to migrate or delete.
- Do not upstream or retain the Phase 10.1 VPS launcher/ACL/profile/path values
  in Hermes Agent. They belong to the local deployment binding and require a
  separate reviewed revision before activation.

# Phase 10.2A-local adversarial review memo

## Security claim

The Python marker, hidden tools and DB-layer guard are not the hard boundary.
The security claim is valid only when `kanban.restricted_workers.enabled` is
paired with the separately reviewed fixed restricted-UID `HERMES_BIN` launcher
and filesystem binding in Phase 10.1 (or an equivalently reviewed OS binding).
The dispatcher fails closed without an absolute launcher. Activation must also
prove the child identity cannot traverse/write the selected board DB and its
WAL/SHM paths. This PR does not create that identity or mutate a host.

## Attacks considered

- **Raw sqlite/import/CLI bypass:** worker receives no board path or claim
  credential; public DB mutators fail under the restricted marker; the OS UID
  must deny raw SQLite even if the worker discovers the path.
- **Foreign/sibling mutation:** result schema has no identity or generic verb.
  Linux `SCM_CREDENTIALS` must match the spawned PID and configured restricted
  UID, so a sibling or terminal subprocess cannot inject a result even if it
  obtains another worker's file descriptor. The dispatcher then atomically
  verifies task/run/profile/workspace/claim/expiry/PID plus active run state.
- **Stale/replayed result:** each attempt receives a fresh, dispatcher-owned
  datagram socket; at most one result is applied, and current-run/claim/PID CAS
  rejects stale data.
  Bindings are process-local and removed after exit; dispatcher restart loses
  the binding and safely falls back to crash/reclaim rather than replay.
- **Result injection:** a worker may send only the already-authorized
  complete/block choice for its bound run. Unsupported
  fields/actions, multiple records, non-object metadata, created-card claims
  and oversized results are refused. Trusted-side redaction runs again.
- **Lifecycle semantic bypass:** finalization calls canonical completion/block
  functions, preserving artifacts, events/runs, ready recomputation, failure
  accounting, cleanup and hooks. Goal-mode complete/block gates are retained.
- **False liveness:** restricted workers do not touch the DB for heartbeat.
  Existing live-PID claim extension and restricted binding supervision prevent
  false reclaim; crash detection and max-runtime remain effective.
- **Graph/tool bypass:** restricted workers receive only complete, block and
  liveness tools. Tool hiding is defense in depth; DB denial is decisive.

## Residual risks and activation gates

- The retained Phase 10.1 launcher currently preserves/requires the old board
  environment. Its separate deployment revision must preserve
  `HERMES_KANBAN_RESTRICTED_WORKER` and `HERMES_KANBAN_CONTEXT`, remove the four
  board/claim variables from its required set, and keep the same closed argv
  and fixed-UID checks. This PR must not be deployed against the old launcher.
- The revised launcher contract must switch to the configured UID before any
  Hermes Python runs, then `exec` Hermes (so the credential PID is the Popen
  PID), install a parent-death kill mechanism after the UID switch, and deny
  the board DB plus WAL/SHM paths. Its canonical path and every ancestor must
  be root-owned and non-group/world-writable.
- The Unix control socket is an integrity channel, not a confidentiality store.
  Handoff fields become durable board data and must not contain secrets.
  Trusted-side redaction is retained.
- A gateway restart intentionally abandons in-memory result bindings. Every
  restricted task is required to have a positive max runtime, so a restarted
  dispatcher retains a finite cleanup bound; the launcher must additionally
  kill an orphan immediately on parent death. No persistent receipt/authority
  store is added.
- The deterministic suite proves runtime logic. Actual raw-SQLite denial needs
  the authorized VPS binding and is therefore a later deployment preflight,
  not something this source-only session may mutate or claim to have run.

## Verdict

The Phase 10.2A source design and exact-R0 candidate are ready for review
subject to final tests. Activation remains separately blocked on a reviewed
Phase 10.1 binding revision and its real restricted-UID raw-SQL/CLI/import
integration proof. No live deployment is authorized.

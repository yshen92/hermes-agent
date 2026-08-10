# Phase 10.2A-local runtime provenance

`R0_PROVENANCE: EXACT_COMMIT_RECOVERED`

```yaml
upstream_base: 3c27eb6234bf91b8ceee9e9071591b31e9b148cb
exact_r0_commit: 01edcadbd194f81bd7eceb9ca267737830ce24c0
exact_r0_remote:
  repository: yshen92/hermes-agent
  ref: refs/heads/r0-evidence/v2026.8.3-01edcad
final_phase10_2a_runtime_candidate: 819fe5c1fe0f442674c3b00f24c6d6858faa0359
```

The final runtime candidate has the exact immutable ancestry:

1. official Hermes `v2026.8.3` / package `0.20.0` at `3c27eb623...`;
2. recovered R0 child `01edcadbd...` with that exact parent;
3. Phase 10.2A-local implementation commit `b5e3ab783...`;
4. deterministic CI correction `819fe5c1f...`, which makes the POSIX identity
   guard Windows-safe and compares contributor attribution to the actual PR
   base without changing the runtime authority model.

The R0 commit was fetched from the preserved evidence ref and was not
reconstructed, amended, squashed or rebased. No behaviorally equivalent R0
substitute remains in the final ancestry. The evidence ref
`r0-evidence/v2026.8.3-01edcad` was read and verified but never modified.

The preserved private backup bundle was independently reported with SHA-256
`63a61edc01024357ace13a9256cd15ba2b573153a7629d7c2f3481f624ae6f7a`;
this session did not need or alter that private backup.

`RUNTIME_PROVENANCE.md` is a provenance-only follow-up to the runtime commit,
so the PR head that contains this record is intentionally distinct from the
runtime candidate named above. The exact PR head is recorded in the PR and
final delivery result after this file is committed.

No VPS mutation, service change, runtime activation or deployment occurred.

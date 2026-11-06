> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](../../portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Failure and recovery drills

Bounded reference experiments. Record run ID, revision and audit events before
and after each step. All EC2 commands run through SSM; Windows commands are
marked. Do not describe in-process object recreation as a process restart.

## 1. Process crash with an awaiting-approval run

EC2:

```bash
/usr/local/lib/delivery-recovery/service-control.sh status
docker kill --signal=KILL delivery-recovery
/usr/local/lib/delivery-recovery/service-control.sh replace
/usr/local/lib/delivery-recovery/service-control.sh verify
curl -fsS http://127.0.0.1:8000/readyz
```

`replace` removes the killed container before starting; a stopped container
still reserves the name. `verify` refuses to start if the data mount is missing.

Verify:

- same run ID/thread ID remains `awaiting_approval`;
- audit still has `RUN_CREATED`, `RUN_WAITING`, no decision;
- no duplicate revision or approval event.

## 2. Normal service restart

```bash
systemctl restart delivery-recovery
```

The unit runs `ExecStartPre` mount verification and will not start the app
against the root-directory fallback. Check `/readyz` and the pending run again.

## 3. Post-commit / pre-checkpoint crash window

Local tested control:

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase3_restart_drill.py `
  prepare --mode crash-window --data C:\tmp\p4-crash-window
.\.venv-focused\Scripts\python.exe scripts\phase3_restart_drill.py `
  recover --mode crash-window --data C:\tmp\p4-crash-window
```

Cloud observation is separate. If the process dies after the decision
transaction before checkpoint completion, restart must find the prior decision
and advance without a second revision or audit event. Record both the local
control and any cloud observation; do not claim the cloud drill before it runs.

## 4. Provider degradation

The scope uses `LocalDemoProvider`, a deterministic fake. A live-provider outage
cannot be injected here. Keep deterministic readiness/serving and use the
existing offline provider failure tests. Label any controlled error
`fake_provider`/`injected`; do not claim a live outage recovery.

## 5. SSM session loss

Close the port-forwarding session. The app must remain running because SSM is
transport only. Reconnect and verify readiness and the same run. If SSM cannot
reconnect, check instance state, outbound 443/53, SSM agent status, instance
role and managed-instance inventory.

## 6. Faulty release

Use an intentionally bad digest or stop the service and verify readiness fails.
Record the failing signal, then follow `rollback.md`; rollback is operator-led,
not automatic.

## Evidence

- before/after run JSON and audit events;
- UTC timestamps and SSM command IDs;
- CloudWatch excerpts around the failure;
- container status/restart count;
- mount verification result;
- explicit statement of what was injected and what remained unchanged.

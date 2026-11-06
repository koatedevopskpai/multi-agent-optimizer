> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](/docs/portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Smoke test

Run after deploy or recreation. The smoke script uses the real HTTP API, real
worker and persisted stores. It prints no tokens.

## Health

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
Invoke-RestMethod http://127.0.0.1:8000/readyz
```

Expected: `/healthz` 200; `/readyz` status `ready`, schema version 2, storage
`ok`, worker `running`, model `local-demo`. A model outage does not remove
deterministic readiness.

On EC2, also verify the data mount through the same helper used before start:

```bash
/usr/local/lib/delivery-recovery/service-control.sh verify
/usr/local/lib/delivery-recovery/service-control.sh status
```

## In-instance business smoke

```powershell
$Instance = terraform -chdir=infra/terraform output -raw instance_id
aws ssm send-command --instance-ids $Instance `
  --document-name AWS-RunShellScript `
  --parameters 'commands=["docker exec delivery-recovery python scripts/phase4_container_smoke.py --base-url http://127.0.0.1:8000 --identity-file /data/demo-identities.json --out /tmp/smoke.json","cat /tmp/smoke.json"]' `
  --query 'Command.CommandId' --output text
```

Then retrieve the command result:

```powershell
aws ssm get-command-invocation --command-id <command-id> --instance-id $Instance
```

Expected JSON facts:

- awaiting status `awaiting_approval`, finish day 15, GBP 1,800;
- completed status `completed`, revision 1, decision approved;
- audit events `RUN_CREATED`, `RUN_WAITING`, `DECISION_APPROVED`,
  `RUN_COMPLETED`;
- exactly one revision and plan version 2.

The project ID is unique per smoke run (`UTILITY-SMOKE-<UTC time>`), so repeated
smoke runs do not collide with an existing authoritative project version.

## Local smoke against a forwarded port

`aws ssm start-session ... AWS-StartPortForwardingSession` maps instance port
8000 to local 8000. To obtain the application tokens, read the identity file in
an interactive SSM shell; do not save a transcript and do not put tokens in a
URL, screenshot or evidence file.

## CloudWatch correlation

```powershell
aws logs describe-log-streams `
  --log-group-name /aws/delivery-recovery/app `
  --order-by LastEventTime --descending --max-items 5
aws logs filter-log-events `
  --log-group-name /aws/delivery-recovery/app `
  --filter-pattern "<run-id>"
```

Record the run ID, request ID, source commit and image digest in the evidence
manifest. Screenshots supplement logs; they do not replace them.

## CP-06 target facts

- ECR repository image digest exactly matches the deployed container.
- Deployed source commit and readiness payload.
- Business smoke run ID/status/revision.
- CloudWatch log stream with the same run ID.
- No public application or SSH port exposed.

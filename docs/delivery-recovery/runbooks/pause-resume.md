> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](../../portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Pause and resume

All EC2 commands run through SSM. Stopping the instance stops compute and
releases the auto-assigned public IPv4. EBS, ECR images, logs and any snapshot
remain billable. No Elastic IP is allocated by this design.

## Pause

```bash
# EC2: stop the service through the verified control path first.
/usr/local/lib/delivery-recovery/service-control.sh stop
```

```powershell
$Instance = terraform -chdir=infra/terraform output -raw instance_id
aws ec2 stop-instances --instance-ids $Instance --region eu-west-2
aws ec2 wait instance-stopped --instance-ids $Instance --region eu-west-2
```

Record UTC stop time, instance state, EBS volume IDs/sizes/encryption, ECR image
digest/size, log retention/bytes, snapshot IDs/sizes, and confirm there is no
project Elastic IP. Projected daily residual cost is about £0.085/day before
snapshots, separate from the compute/address hourly rate.

## Resume

```powershell
aws ec2 start-instances --instance-ids $Instance --region eu-west-2
aws ec2 wait instance-running --instance-ids $Instance --region eu-west-2
```

Then through SSM:

```bash
/usr/local/lib/delivery-recovery/service-control.sh verify
systemctl start delivery-recovery
curl -fsS http://127.0.0.1:8000/readyz
```

The instance receives a new public IPv4. Verify SSM managed-instance inventory,
Docker/Compose state, schema version 2, worker running, preserved run IDs and
unchanged revisions/audit. No inbound application or SSH rule is opened.

## Spend control

- Operational stop threshold: **£12 estimated gross**.
- Approved total ceiling: **£20 including VAT**.
- T4g CPU credit mode is **Standard**. There is no Unlimited surplus-credit
  charge; sustained bursts may be throttled. Record CPU credit metrics if a
  drill saturates the instance.
- The threshold is not an AWS-enforced cap; billing delay, tax and cleanup can
  arrive later.
- During the waiting period remove project infrastructure rather than leaving
  stopped resources. The endorsed lifecycle is teardown after proof and
  recreation for the interview.
- If instance size, session length, retention, NAT, load balancer, endpoints or
  snapshots change, stop and revise the packet first.

> Historical engineering reference, adapted for publication. Execution packets and approval states below belong to the original development sequence; they are not current authorization or a turnkey deployment guide. Start with [the current portfolio guide](../../portfolio/START_HERE.md). Personal paths and identifiers have been replaced; original byte-preserved records remain in the private audit archive.

# Backup and restore

Executed cloud result (Codex, `DR-CLOUD-BACKUP-20261104T194615Z`): the bounded
window verified a 15,226-byte archive
(`9189d4d5...e1d45f0c`), stopped the instance with no public IPv4, and the actual
archive restored in an isolated local Python process with **1 run, 1 approved
revision and 4 audit events** before and after restart. Docker was unavailable,
so that run was not a container restore. The raw legacy archive needed the
explicit normalization step in section 3; the strict importer was not weakened.

The recovery unit is a consistent copy of both SQLite stores plus a SHA-256
manifest and inventory. Two independent SQLite backups are not a cross-database
atomic snapshot while writes continue, so the service **must** be stopped before
copying and the stop must be confirmed. `backup-export.sh` aborts if stop or
mount verification fails.

All EC2 commands run through SSM; Windows commands are marked separately.

## 1. Create backup and export on EC2

```bash
/usr/local/lib/delivery-recovery/backup-export.sh <run-id>
```

This helper:

1. reads `/etc/delivery-recovery/service.env`;
2. stops the service if running and **aborts before copying** if stop fails;
3. verifies that `/srv/delivery-recovery/data` is the approved EBS mount;
4. runs the backup container as UID 10001 with `--include-identity`;
5. exports `/srv/delivery-recovery/backups/<run-id>.tgz`;
6. restarts the service if it was running.

The backup manifest contains file hashes, `PRAGMA integrity_check`, run IDs,
statuses, revision count and audit count. The identity file is sensitive and
must not enter Git or public evidence.

## 2. Download to Windows over SSM

```bash
# EC2: temporary loopback-only HTTP listener, self-expiring
nohup timeout 900 python3 -m http.server 18080 --bind 127.0.0.1 \
  --directory /srv/delivery-recovery/backups \
  >/run/backup-http.log 2>&1 &
echo $! > /run/backup-http.pid
```

```powershell
# Windows: forward and download
$Instance = terraform -chdir=infra/terraform output -raw instance_id
aws ssm start-session --target $Instance `
  --document-name AWS-StartPortForwardingSession `
  --parameters '{"portNumber":["18080"],"localPortNumber":["18080"]}'
# In a second PowerShell window:
New-Item -ItemType Directory -Force var\phase4-recovery\<run-id> | Out-Null
Invoke-WebRequest http://127.0.0.1:18080/<run-id>.tgz `
  -OutFile var\phase4-recovery\<run-id>\<run-id>.tgz
Get-FileHash var\phase4-recovery\<run-id>\<run-id>.tgz -Algorithm SHA256
```

Compare the local hash with the export output. The listener is one-shot and
expires after 900 seconds; do not depend on a post-stop SSM cleanup command. If
the instance is stopped, any remaining listener dies with it.

No S3 bucket is created. This route uses the operator SSM permission already in
the packet. The automated window in `scripts/phase4_backup_window.py` uses a
tracked transfer subprocess killed at the operation deadline and sends no SSM
cleanup after stop.

## 3. Import and restore on Windows (fresh restore proof)

First try the strict importer. If it reports
`BACKUP_ARCHIVE_UNEXPECTED_MEMBER` for the four documented legacy sidecars
(`application.db-shm`, `application.db-wal`, `checkpoints.db-shm`,
`checkpoints.db-wal`), run the **explicit** compatibility step. Never discard
archive members by hand, never use a nonempty WAL, and never change the strict
importer or cloud image.

```powershell
# Fails unless: all members are safe regular files; manifest/files/counts are valid;
# every listed hash matches; only the four documented sidecars are extra; both WALs
# are 0 bytes; the output does not already exist.
.\.venv-focused\Scripts\python.exe scripts\phase4_backup_restore.py normalize-legacy `
  --archive var\phase4-recovery\<run-id>\<run-id>.tgz `
  --out var\phase4-recovery\<run-id>\<run-id>-manifest-only.tgz `
  --receipt var\phase4-recovery\<run-id>\legacy-normalization.json
if ($LASTEXITCODE -ne 0) { throw 'legacy normalization rejected; preserve the original' }
```

The original archive is never modified. The receipt records original/derived
hashes, excluded members and WAL sizes. Unknown extras, nonempty WALs, unsafe or
duplicate members, manifest-listed sidecars, corrupt input and missing files are
rejected. The source, derived output, receipt and staging paths must be distinct
(including resolved/symlink/hardlink aliases); an existing receipt or output is
never overwritten, and a receipt is only written once the derivative exists. The
outer gzip hash depends on local gzip metadata; use the member set/hashes and
restored inventory as the equivalence check. The derivative is the
strict-compatible archive for the next step; ordinary `import` still rejects the
raw legacy archive, so normalization is always explicit. Both archives contain
demo bearer identities and stay private.

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_backup_restore.py import `
  --archive var\phase4-recovery\<run-id>\<run-id>-manifest-only.tgz `
  --target var\phase4-recovery\<run-id>\imported
.\.venv-focused\Scripts\python.exe scripts\phase4_backup_restore.py restore `
  --backup var\phase4-recovery\<run-id>\imported `
  --target var\phase4-recovery\<run-id>\restored
```

Start a fresh local container against `restored` and verify the preserved run
states before deleting any cloud data. If Docker is unavailable, use an isolated
local Python process with credentials/tracing removed and label it a process
restore, not a container restore. The preparation evidence contains a
fresh-container restore of four synthetic runs (1 revision, 10 audit events); the
actual cloud archive was process-restored with 1 run, 1 revision and 4 audit
events.

## 4. Restore a transferred archive onto EC2

**Baseline-read lesson from `DR-EC2-RESTORE-20261104T223838Z`:** a cleanly
stopped WAL-mode database may have no `-shm`; `sqlite3` `mode=ro` on a read-only
mount then fails with `unable to open database file`. Read the logical state by
copying the DB and any existing `-wal`/`-shm` sidecars to writable tmpfs and
opening the disposable copy with `mode=rw`; keep the live source `:ro`. Do not
use `immutable=1`, discard WAL, broaden IAM, change the image or make the live
source writable. Do not reuse the failed run/release ID; use fresh destinations.

Use `scripts/phase4_ssm_upload.py` from Windows to send the archive over SSM
without S3 (use the manifest-only derivative if the original was legacy):

```powershell
.\.venv-focused\Scripts\python.exe scripts\phase4_ssm_upload.py `
  --instance-id $Instance `
  --archive var\phase4-recovery\<run-id>\<run-id>-manifest-only.tgz `
  --remote-path /srv/delivery-recovery/incoming/<run-id>.tgz
```

Then on EC2, import and restore in one one-off container with a persistent target:

```bash
install -d -o 10001 -g 10001 /srv/delivery-recovery/incoming /srv/delivery-recovery/data/restore/<run-id>
docker run --rm --read-only --tmpfs /tmp:size=64m,mode=1777 \
  --user 10001:10001 \
  -v /srv/delivery-recovery/incoming:/backup \
  -v /srv/delivery-recovery/data/restore/<run-id>:/target \
  <pinned-image> sh -lc '
    python scripts/phase4_backup_restore.py import \
      --archive /backup/<run-id>.tgz --target /target/imported &&
    python scripts/phase4_backup_restore.py restore \
      --backup /target/imported --target /target/restored'
```

Start a temporary container against `/srv/delivery-recovery/data/restore/<run-id>/restored`
on `127.0.0.1:18001`, verify `/readyz` and the expected run IDs, then remove it:

```bash
docker run -d --name restore-verify --read-only --tmpfs /tmp:size=64m,mode=1777 \
  --security-opt no-new-privileges:true --cap-drop ALL \
  -p 127.0.0.1:18001:8000 \
  -v /srv/delivery-recovery/data/restore/<run-id>/restored:/data \
  <pinned-image>
curl -fsS http://127.0.0.1:18001/readyz
docker rm -f restore-verify
```

## 5. Stopped-service switch on the stable mount

The data root `/srv/delivery-recovery/data` is the mounted EBS filesystem. Never
rename that mountpoint. The container bind path is
`/srv/delivery-recovery/data/current`; releases live in `.../versions/`.

```bash
/usr/local/lib/delivery-recovery/restore-switch.sh switch \
  /srv/delivery-recovery/data/restore/<run-id>/restored <release-id>
curl -fsS http://127.0.0.1:8000/readyz
```

The helper verifies the exact mountpoint, requires restored data on the same
filesystem, stops and confirms the service is not running, moves `current` to
`versions/<release-id>`, moves the verified restored set into `current`, sets
UID/GID 10001 ownership, verifies the mount and starts the service. If start
fails it restores the previous `current`.

Rollback:

```bash
/usr/local/lib/delivery-recovery/restore-switch.sh rollback <release-id>
```

This keeps the mountpoint stable and preserves the failed set under
`versions/failed-*`. A plain-directory copy is not mount-behaviour evidence; the
local test used a disposable tmpfs mount.

## Rules

- Never normalize implicitly: the strict importer stays strict; legacy
  normalization is a separate, explicit command with its own receipt.
- Never discard a nonempty WAL or an arbitrary extra member.
- A stopped container still holds its name; use `service-control.sh` instead of
  ad-hoc `docker stop`/`docker run`.
- Never copy a live SQLite store while the worker may be writing.
- Treat Docker inspect/Compose discovery failures as errors; do not infer
  stopped from failed discovery.
- A snapshot or volume left on the instance is not an off-instance backup.
- Prove a fresh-environment restore before deleting the instance/volume.
- Do not claim cloud restore execution until it has actually been run.
- Do not describe an isolated Python process restore as a container restore or
  host/volume-loss recovery.

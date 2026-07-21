# Risk Alert Service

A synchronous FastAPI service that reads monthly account status from Parquet,
finds accounts that are continuously At Risk, and routes alerts to regional
Slack channels. It supports local files, GCS, and S3, and uses SQLite to make
reruns safe. See `docs/architecture.md` for the component and sequence diagrams.

## Quickstart

1. Create an environment and install dependencies:

   ```bash
   python -m venv .venv
   # Windows: .venv\Scripts\activate
   # macOS/Linux: source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Start the included mock Slack service:

   ```bash
   uvicorn mock_slack.server:app --host 0.0.0.0 --port 9000
   ```

3. Configure and start the API in another terminal:

   ```bash
   export SLACK_WEBHOOK_BASE_URL="http://localhost:9000/slack/webhook"
   export REGION_CHANNEL_MAP='{"AMER":"amer-risk-alerts","EMEA":"emea-risk-alerts","APAC":"apac-risk-alerts"}'
   export ARR_THRESHOLD=10000
   uvicorn app.main:app --reload --port 8000
   ```

   In PowerShell, use `$env:NAME="value"` instead of `export`.

4. Exercise the API:

   ```bash
   curl http://localhost:8000/health

   curl -X POST http://localhost:8000/preview \
     -H "Content-Type: application/json" \
     -d '{"source_uri":"file:///absolute/path/monthly_account_status.parquet","month":"2026-01-01"}'

   curl -X POST http://localhost:8000/runs \
     -H "Content-Type: application/json" \
     -d '{"source_uri":"file:///absolute/path/monthly_account_status.parquet","month":"2026-01-01","dry_run":false}'

   curl http://localhost:8000/runs/<run_id>
   ```

Captured response examples, including a replay, are in `docs/examples/`.

## Configuration

Settings come from environment variables, optionally layered over a JSON file
specified by `CONFIG_FILE`. Environment variables take precedence.

| Variable | Purpose | Default |
|---|---|---|
| `ARR_THRESHOLD` | Minimum ARR eligible for an alert | `0` |
| `SLACK_WEBHOOK_BASE_URL` | POST to `{base}/{channel}`; takes precedence over the single URL | unset |
| `SLACK_WEBHOOK_URL` | Optional single Slack webhook | unset |
| `DETAILS_BASE_URL` | Account-details URL prefix | `https://app.yourcompany.com/accounts` |
| `REGION_CHANNEL_MAP` | JSON mapping from region to channel | `{}` |
| `SQLITE_PATH` | SQLite database path | `./risk_alerts.db` |
| `SUPPORT_EMAIL` | Unknown-region digest recipient | `support@quadsci.ai` |
| `SUPPORT_NOTIFICATION_LOG` | Local digest stub output | `./support_notifications.jsonl` |
| `RETRY_MAX_ATTEMPTS` | Total Slack attempts | `4` |
| `RETRY_BASE_DELAY_SECONDS` | Initial retry delay | `0.5` |
| `RETRY_BACKOFF_FACTOR` | Delay multiplier | `2.0` |
| `RETRY_MAX_DELAY_SECONDS` | Maximum retry delay | `30` |
| `HTTP_TIMEOUT_SECONDS` | Slack request timeout | `10` |
| `CLAIM_TIMEOUT_SECONDS` | Age at which interrupted delivery work can be retried | `300` |

Example routing configuration:

```json
{"AMER":"amer-risk-alerts","EMEA":"emea-risk-alerts","APAC":"apac-risk-alerts"}
```

There is no fallback channel. Missing, null, or unmapped regions are recorded
as `failed` with reason `unknown_region` and are never sent to Slack.

### ARR threshold

The default is `0` because the exercise provides no customer policy from which
to choose a meaningful commercial cutoff. This avoids silently suppressing
valid risk alerts. Deployments should set the threshold to their own account
segmentation policy. A null ARR is treated as `0`.

### Cloud authentication

`gs://` and `s3://` use `pyarrow.fs.GcsFileSystem` and `S3FileSystem`. GCS uses
Application Default Credentials: set `GOOGLE_APPLICATION_CREDENTIALS` locally,
or use Workload Identity on GCP. AWS credentials follow the standard AWS
credential chain used by PyArrow.

## Processing behavior

### Scale-aware loading and deduplication

The service performs two filtered Parquet reads:

1. Read the target month, deduplicate by `(account_id, month)` using the latest
   `updated_at`, then apply status and ARR filters.
2. Read prior history only for the resulting account IDs.

Status filtering occurs after deduplication because a stale duplicate may have
a different status. `duplicates_found` counts extra rows discarded from the
rows used by the run. Missing required columns and null required values produce
explicit validation errors; the optional `account_owner` column may be absent.

### Risk duration

For every eligible target-month account, duration walks backward one calendar
month at a time while status remains `At Risk`. A changed status or missing
month ends the streak. `risk_start_month` is the earliest month in that streak.

### Replay safety

SQLite enforces one current outcome per `(account_id, month, alert_type)`:

- An existing `sent` outcome is not delivered again and is counted as
  `skipped_replay`.
- A `failed` outcome is retried on the next run.
- Each run retains its own result records for API reporting.

A short `pending` claim prevents two concurrent runs from normally sending the
same alert. Old claims can be reclaimed after `CLAIM_TIMEOUT_SECONDS` so an
interrupted process does not block delivery permanently. This is a practical
safeguard, not an exactly-once guarantee: a process could still stop after
Slack accepts a message but before SQLite records success.

The unique-constrained-table check-then-upsert above is what the exercise
asks for; the claim/timeout layer is deliberate, beyond-spec hardening for
scheduler double-fires and crash recovery — see `docs/architecture.md` for
why I kept it.

### Unknown regions

After a run, unknown-region failures produce one aggregate notification for
`support@quadsci.ai`. The included implementation logs a warning and appends a
JSON record to `SUPPORT_NOTIFICATION_LOG`. In production, the same interface
could call SES, SendGrid, or another email provider. Preview and dry-run requests
do not write this notification.

### Slack delivery

Messages are sent as `{"text":"..."}` to either
`{SLACK_WEBHOOK_BASE_URL}/{channel}` or `SLACK_WEBHOOK_URL`. Network errors,
HTTP 429, and all 5xx responses are retried with capped exponential backoff.
`Retry-After` is honored when present. A failed account does not stop the rest
of the run.

## API

- `GET /health` — process health.
- `POST /preview` — computes and returns alerts without sending Slack or
  persisting alert outcomes; a run audit row is retained.
- `POST /runs` — synchronously processes a month, optionally as a dry run, and
  returns `{"run_id":"..."}`.
- `GET /runs/{run_id}` — returns status, counts, and sample alerts/errors.

For dry runs, `alerts_sent` represents alerts that would be sent, not actual
network deliveries.

## Tests

```bash
pytest -q
```

The suite covers storage dispatch, filtered loading, schema validation,
deduplication, ARR and duration logic, formatting and routing, retries,
idempotency, interrupted claims, and end-to-end API behavior.

## Docker

```bash
docker build -t risk-alert-service .
docker run --rm -p 8000:8000 \
  -e SLACK_WEBHOOK_BASE_URL=http://host.docker.internal:9000/slack/webhook \
  -e REGION_CHANNEL_MAP='{"AMER":"amer-risk-alerts","EMEA":"emea-risk-alerts","APAC":"apac-risk-alerts"}' \
  -v risk-data:/data \
  -e SQLITE_PATH=/data/risk_alerts.db \
  risk-alert-service
```

SQLite must be stored on a mounted volume if run history should survive
container replacement.

# Architecture

## Components

```mermaid
flowchart LR
    subgraph API["FastAPI (app/main.py)"]
        H["GET /health"]
        P["POST /preview"]
        R["POST /runs"]
        G["GET /runs/{run_id}"]
    end

    Storage["app/storage.py\nopen_uri()\npyarrow.fs: Local / GCS / S3"]
    Data["app/data.py\ntwo-pass filtered read\ndedup by latest updated_at"]
    Risk["app/risk_logic.py\nduration_months\nrisk_start_month"]
    Slack["app/slack_client.py\nformat + route + retry"]
    Notif["app/notifications.py\nunknown_region digest"]
    DB[("SQLite\nruns / alert_outcomes / run_alert_results")]

    Source[("Parquet\nfile:// / gs:// / s3://")]
    SlackAPI["Slack webhook\n(base URL mode or single URL)"]
    Support["support@quadsci.ai\n(logged/stubbed)"]

    R --> Storage --> Source
    P --> Storage
    Storage --> Data --> Risk
    R --> DB
    P -.->|persist run audit row; no alert outcomes| DB
    Risk --> Slack --> SlackAPI
    Slack --> DB
    Risk --> Notif --> Support
    G --> DB
```

## Request sequence: `POST /runs`

```mermaid
sequenceDiagram
    participant Client
    participant API as FastAPI (main.py)
    participant Store as storage.py
    participant Data as data.py
    participant Risk as risk_logic.py
    participant DB as SQLite (db.py)
    participant Slack as slack_client.py
    participant Webhook as Slack webhook
    participant Notif as notifications.py

    Client->>API: POST /runs {source_uri, month, dry_run}
    API->>DB: create_run(status="running")
    API->>Store: open_uri(source_uri)
    Store-->>API: pyarrow Dataset (lazy)
    API->>Data: load_at_risk_candidates(dataset, month, arr_threshold)
    Note over Data: Pass 1: scan target-month rows,<br/>dedup by latest updated_at,<br/>then filter status==At Risk & arr>=threshold
    Note over Data: Pass 2: scan prior-month history<br/>for candidate accounts only
    Data-->>API: candidates + history + rows_scanned/duplicates_found
    API->>Risk: compute_alerts(load_result, month)
    Risk-->>API: [RiskAlert(duration_months, risk_start_month, ...)]

    loop each alert
        API->>DB: get_existing_outcome + atomic claim_alert
        alt already sent
            API->>API: tally skipped_replay
        else region unmapped
            API->>DB: upsert_alert_outcome(status="failed", reason="unknown_region")
            API->>API: tally failed_deliveries, queue for digest
        else deliver
            API->>Slack: format_alert_message + send_alert(channel, payload)
            Slack->>Webhook: POST {base_url}/{channel}
            Webhook-->>Slack: 200 / 429(Retry-After) / 5xx
            Note over Slack: retry with exponential backoff<br/>on 429/5xx, honoring Retry-After
            Slack-->>API: DeliveryResult(sent|failed)
            API->>DB: upsert_alert_outcome(status)
            API->>API: tally alerts_sent / failed_deliveries
        end
    end

    opt any unknown_region failures
        API->>Notif: send_unknown_region_summary(...)
        Notif->>Support: one aggregated notice (logged + JSONL)
    end

    API->>DB: finalize_run(status, counts)
    API-->>Client: {"run_id": ...}

    Client->>API: GET /runs/{run_id}
    API->>DB: get_run + list_alert_outcomes
    API-->>Client: status, counts, sample_alerts, sample_errors
```

## Key design choices

**Storage.** `open_uri()` dispatches on URI scheme to a `pyarrow.fs`
filesystem (local, GCS, S3) and returns a `pyarrow.dataset.Dataset`. All
three schemes share one code path, and callers always project columns and
push filters down at read time — nothing is fully materialized.

**Two-pass read.** Pass 1 scans only the target month, across all statuses,
and deduplicates *before* filtering to `At Risk`: the winning row (latest
`updated_at`) can carry a different status than the duplicate it beats, so
status has to be decided after dedup, not before. Pass 2 then scans only
the prior-month history of the accounts that survived pass 1. No unrelated
account or month is ever read. (See the README's "Replay safety" section
for the delivery-side mechanics — `sent` short-circuits, `failed` retries.)

**Why replay safety goes further than the spec asks.** The spec's minimum
bar is one unique-constrained table: check the existing outcome, skip if
`sent`, retry if `failed`. That's implemented, but two things go beyond it,
on purpose:

- `run_alert_results` is a separate, append-only audit table, distinct from
  the canonical `alert_outcomes` state. Without it, a later retry that
  flips a key from `failed` to `sent` would silently overwrite what an
  *earlier*, already-completed run reported for that account — so
  `GET /runs/{run_id}` for an old run could start lying about its own
  history the moment a later run touches the same key. This one is really
  a spec requirement in disguise: `GET /runs/{run_id}` promises sample
  alerts/errors per run, and that only stays true if each run's record is
  immutable.
- Delivery reserves a `pending` claim before calling Slack, and a stale
  claim can be reclaimed after `CLAIM_TIMEOUT_SECONDS`. This one is
  genuinely beyond what's asked — `/runs` is a single synchronous request,
  and the exercise doesn't require concurrent-run safety. I added it
  because a scheduler-triggered batch job like this realistically does
  double-fire (a timeout-and-retry from the scheduler, a manual re-run
  overlapping a scheduled one, two replicas racing), and a plain
  check-then-upsert has a real gap between the check and the send where
  that would double-post to Slack. The timeout exists so a process that
  dies mid-delivery doesn't strand that one account permanently — I hit
  exactly that failure mode while building this and fixed it (see
  `test_delivery_exception_after_claim_does_not_strand_the_alert`). It's
  more machinery than the exercise asks for; I kept it because the failure
  mode it closes is one I'd expect for real, not a hypothetical I invented
  to pad the design.

**No default Slack channel.** Unmapped or missing `account_region` never
reaches `slack_client` — it's recorded as `failed`/`unknown_region` and
rolled into one end-of-run notice to `support@quadsci.ai`.

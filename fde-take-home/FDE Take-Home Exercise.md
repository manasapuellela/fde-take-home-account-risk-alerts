# **Take-Home Exercise**

## **Monthly Account Risk Alerts → Slack Integration (Cloud-Ready)**

### **Scenario**

You receive monthly account health status data in Parquet. Build a cloud-deployable Python service that:

* Identifies accounts that are **“At Risk”** for a given month  
* Computes how long they’ve been continuously at risk  
* Posts Slack alerts to region-specific channels  
* Safely supports re-running the same month (no duplicate alerts)  
* Is designed to run in a customer environment (AWS or GCP)

This simulates a production batch integration deployed alongside customer data.

### **What We Will Evaluate**

This exercise focuses on how you approach a realistic data integration problem. We are primarily evaluating:

* **Correctness of business logic** (duration calculation, duplicate handling, idempotency)  
* **Code organization and separation of concerns**  
* **Handling of external integrations and failures** (Slack retries, routing errors)  
* **Production awareness** (configuration, portability, clear README)

We are not evaluating advanced infrastructure, CI/CD, or complex cloud setup. Please focus on clarity, correctness, and thoughtful design rather than over-engineering.

---

# **Design Requirements**

## **Cloud Storage**

Your service must support a `source_uri`:

* `file://...` (local)  
* `gs://bucket/path/file.parquet` (**required, exercised**)  
* `s3://bucket/path/file.parquet` (design should support; implementation optional)

Abstract storage access behind a small interface (e.g., `open_uri(source_uri)`).

## **Scale Awareness**

Assume the Parquet file may be large. Your design should:

* Minimize scanning and memory use  
* Only materialize rows needed for:  
  * the target month  
  * required history to compute duration  
* Prefer Parquet-friendly access patterns (e.g., filtered scanning, record batches)  
* Be runnable as a containerized batch job

## **Configuration**

Cloud access, Slack routing, base URL, and secrets must be configurable via environment variables and/or config file.

---

# **Data**

**Parquet file** `gs://fde-take-home/monthly_account_status.parquet` containing multiple months of account history. Each row represents one account for one month.

### **Columns**

* `account_id` (string, required)  
* `account_name` (string, required)  
* `account_region` (string, nullable)  
  `month` (date; first day of month)  
* `status` (string; e.g., Healthy / At Risk)  
* `renewal_date` (date, nullable)  
* `account_owner` (string, optional)  
* `arr` (int64, nullable)  
* `updated_at` (timestamp; used for duplicate resolution)

If multiple rows exist for `(account_id, month)`, select the row with the latest `updated_at`.  
Report how many duplicates were encountered.

---

# **Alert Logic**

For a given month (e.g., `2026-01-01`), alert on accounts where:  
`status == "At Risk"`

To reduce noise, include a configurable `ARR_THRESHOLD` and explain your chosen default in the README.

Compute how many continuous months the account has been At Risk up to and including the target month.

Rules:

* `month` is always the first day of month  
* Count backward month-by-month while `status == "At Risk"`  
* Stop when:  
  * status changes, or  
  * a month is missing  
* If no prior At Risk month exists → duration \= 1

Include:

* `duration_months`  
* `Risk_start_month`

Example: 

`2025-10 Status = At Risk`  
`2025-11 Status = At Risk`  
`2025-12 Status = Healthy`  
`2026-01 Status = At Risk`

**`Duration: 1 Month`**

---

# **Slack Alerts**

Each alert must include:

* `🚩 At Risk: {account_name} ({account_id})`  
* Region  
* `At Risk for: X months (since YYYY-MM-01)`  
* ARR  
* Renewal date (or “Unknown”)  
* Owner (`account_owner`, if present)  
* Details URL: `https://app.yourcompany.com/accounts/{account_id}`  
   (base URL configurable)

### **Channel Routing**

Route alerts by `account_region` using config:

`"regions": {`  
    `"AMER": "amer-risk-alerts",`  
    `"EMEA": "emea-risk-alerts",`  
    `"APAC": "apac-risk-alerts"`  
  `}`

There is **no default channel**. If `account_region` is missing, null or not present in the configuration the system must:

1. **NOT send a Slack alert.** Record the alert outcome as `failed` with reason `"unknown_region"`  
2. After processing the run, send a **single aggregated notification** to `support@quadsci.ai` containing:

(Implementation can be a real email sender **or** a clearly documented stub/logging mechanism—your README should explain what would be used in production.)

---

# **Replay Safety**

Prevent duplicate Slack alerts on re-running the same month. Persist alert outcomes in SQLite and enforce uniqueness on: `(account_id, month, alert_type)`

On replay:

* If already sent → mark `skipped_replay`  
* If previously failed → you may retry (document behavior)

---

# **API**

Expose a small FastAPI service.

## **POST /runs**

Request:

`{`  
  `"source_uri": "gs://bucket/monthly_account_status.parquet",`  
  `"month": "2026-01-01",`  
  `"dry_run": false`  
`}`

Behavior:

* Processes the run **synchronously**  
* The request blocks until processing is complete  
* Reads Parquet from `source_uri`  
* Computes alerts for the specified month  
* Sends Slack messages (unless `dry_run=true`)  
* Persists run metadata and alert outcomes  
* Completes even if some Slack sends fail  
* Returns a `run_id` after completion

Response:

`{`  
  `"run_id": "uuid-or-string"`  
`}`

## **GET /runs/{run\_id}**

Returns persisted run results including:

* `status` (`succeeded` / `failed`)  
* `counts` (`rows_scanned`, `alerts_sent`, `skipped_replay`, `failed_deliveries`)  
* sample alerts/errors

## **POST /preview**

Same request as `/runs`, but does not send Slack.

## **GET /health**

---

# **Slack Integration**

Your service must support either:

1. **Base URL mode** (local test, using provided mock service):  
   1. Set `SLACK_WEBHOOK_BASE_URL`  
      Your service must POST to: `{SLACK_WEBHOOK_BASE_URL}/{channel}`

2. **Single webhook mode** (optional, for real Slack):  
   1. Set `SLACK_WEBHOOK_URL.` Your service must POST to that URL.

If both are set, `SLACK_WEBHOOK_BASE_URL` should take precedence.

Retry on:

* HTTP 429 and 5xx  
* Use exponential backoff  
* Honor `Retry-After` if present (stretch)

---

# **Persistence**

Use SQLite. Store:

* `runs`  
* `alert_outcomes` (account\_id, month, channel, status, sent\_at, error)

---

# **Runtime & Deployment**

Include in README:

* Config examples  
* Dockerfile

---

# **Deliverables**

* Code repository  
* README  
* Dockerfile  
* Example output from `/preview` and `/runs/{id}`  
* Architecture/Sequence Diagram


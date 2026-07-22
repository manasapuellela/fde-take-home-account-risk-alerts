# Account Risk Alert Service

A cloud-ready FastAPI service that processes monthly account-health data from
Parquet, identifies accounts that have remained continuously at risk, and sends
alerts to region-specific Slack channels.

This repository was created for a Forward Deployed Engineer take-home exercise
focused on correctness, integration design, failure handling, replay safety,
and production awareness.

## Key Capabilities

- Reads Parquet data from local storage, Google Cloud Storage, or Amazon S3
- Uses filtered, two-pass reads to reduce unnecessary data scanning
- Resolves duplicate account-month records using the latest `updated_at`
- Calculates continuous At Risk duration and risk start month
- Applies a configurable ARR threshold
- Routes alerts to region-specific Slack channels
- Retries transient HTTP 429 and 5xx failures with exponential backoff
- Prevents duplicate alerts across repeated runs using SQLite
- Aggregates unknown-region failures into one support notification
- Includes Docker support, automated tests, architecture documentation, and
  captured API outputs

## Repository Structure

```text
.
├── fde-take-home/          # Complete application and technical documentation
├── example-outputs/        # Captured preview, run, replay, and Slack outputs
└── README.md               # Repository overview
```

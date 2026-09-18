# Forma → Databricks Connector

Pre-beta **vendor-hosted SaaS ETL pipeline**: Autodesk Construction Cloud → customer Databricks (Unity Catalog, Bronze Delta).

## Start here

| Audience | Document |
|----------|----------|
| Customer / sales | [Pitch](docs/customer/PITCH.md) |
| New engineer | [Runbook](docs/engineering/RUNBOOK.md) |
| Architecture diagram | [Overview](docs/architecture/OVERVIEW.md) |
| Run tests | [Testing](docs/engineering/TESTING.md) |
| AI session resume | [Session state](docs/meta/SESSION_STATE.md) |

## Pillars (canonical)

| Document | Purpose |
|----------|---------|
| [PLATFORM](docs/architecture/PLATFORM.md) | Strategy, auth, ingestion lock, phasing, locked decisions |
| [RUNBOOK](docs/engineering/RUNBOOK.md) | Engineer onboarding, sync sequence, components, risks |
| [SCHEMA](docs/architecture/SCHEMA.md) | PK registry, schema evolution, pipeline behavior |
| [ACC_APIS](docs/reference/ACC_APIS.md) | API research — **§1.1 service group capability matrix** |

## Reference

- [Data Connector concepts](docs/reference/DATA_CONNECTOR.md)
- [CDC techniques](docs/reference/CDC_TECHNIQUES.md)
- [Future REST ingest](docs/reference/REST_FUTURE.md)

## Operations

- [Launch & production checklist](docs/operations/LAUNCH.md)

## Code

Application lives in [`acc-connector/`](acc-connector/).

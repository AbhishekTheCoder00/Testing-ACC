# Architecture Overview

Control plane / data plane split for the Forma → Databricks Connector.

## Control plane vs data plane

```mermaid
flowchart TB
    subgraph ControlPlane["Control Plane — Vendor SaaS"]
        UI[WebUI]
        Orch[ETLOrchestrator]
        State[SyncState_and_Tokens]
    end

    subgraph DataPlane["Data Plane — Customer Databricks"]
        Volume[UCVolume]
        Pipelines[LakeflowPipelines]
        Bronze[BronzeDelta_SCD2]
        Analytics[SQL_BI_ML]
    end

    ACC[AutodeskACC] --> Orch
    UI --> Orch
    Orch --> State
    Orch -->|"OAuth_and_REST"| DataPlane
    Orch -->|"Bulk_export_jobs"| ACC
    ACC -->|"Signed_URL_CSVs"| Volume
    Volume --> Pipelines
    Pipelines --> Bronze
    Bronze --> Analytics
```

- **Control plane (SaaS):** Flask app — OAuth, orchestration, scheduling, encrypted token/state store. No long-term ACC project data on the host when `ENABLE_NOTEBOOK_DOWNLOAD=true`.
- **Data plane (customer Databricks):** UC Volume landing, bulk downloader job, two Lakeflow pipelines, Bronze Delta tables.
- **Source:** Autodesk ACC via Data Connector API.

## Data flow

```mermaid
flowchart LR
    ACC[AutodeskACC] --> Extract[BulkExtract]
    Extract --> Volume[UCVolume]
    Volume --> Bronze[BronzeDelta_SCD2]
    Bronze --> Analytics[CustomerAnalytics]
```

## See also

- [PLATFORM.md](PLATFORM.md) — auth, ingestion decisions, phasing
- [RUNBOOK.md](../engineering/RUNBOOK.md) — operational sequence and components
- [PITCH.md](../customer/PITCH.md) — customer-facing summary

# Launch & Production Checklist

Consolidated priorities for production SaaS, Autodesk App Store, and Databricks Partner Connect.

**Assessed:** April–June 2026  
**Target:** Multi-tenant production on Azure App Service; Partner Validation when ready

---

## Summary verdict

| Layer | Pre-beta today | Production target |
|-------|----------------|-------------------|
| Auth & identity | Per-Autodesk-user session | Enterprise SSO (Entra ID) + SSA for sync |
| Data store | SQLite + Fernet | PostgreSQL / Azure SQL |
| Background jobs | Daemon threads | Celery + Redis or Azure Service Bus |
| Security | OAuth PKCE (DBX), basics | CSRF, OAuth `state`, rate limiting |
| Observability | stdout | Application Insights / structured logs |
| Resilience | Manual retry | Auto-retry, health checks, concurrency guards |

Distribution order: Direct sales → Autodesk App Store → Databricks Partner Connect.

---

## P0 — Before any real users

| # | Item | Effort |
|---|------|--------|
| 1 | Multi-user / enterprise auth | Medium |
| 2 | OAuth `state` parameter (ACC CSRF) | Low |
| 3 | CSRF on POST routes | Low |
| 4 | PostgreSQL / Azure SQL (replace SQLite) | Medium |

---

## P1 — Before scaling beyond pilot

| # | Item | Effort |
|---|------|--------|
| 5 | Task queue (Celery / Service Bus) | High |
| 6 | Rate limiting on `/sync` | Low |
| 7 | `/health` endpoint | Very low |
| 8 | Sync concurrency guard (409 if running) | Very low |
| 9 | Retry with backoff on sync failure | Medium |

---

## P2 — Observability & hardening

| # | Item | Effort |
|---|------|--------|
| 10 | Application Insights / structured logging | Low |
| 11 | Azure Key Vault for secrets | Medium |
| 12 | Input validation on API routes | Low–Medium |
| 13 | HTTPS / secure cookie flags | Very low |
| 14 | Token expiry UX | Low |
| 15 | Configurable Databricks node type | Very low |
| 16 | End-to-end tests | Medium |

---

## Databricks Partner Connect (PWAF)

Mandatory for validation:

| Pillar | Status (Apr 2026 assess.) | Blocking? |
|--------|---------------------------|-----------|
| User-Agent telemetry | Done in `databricks_client.py` | Was YES |
| OAuth U2M (PKCE, state) | Partial | YES |
| Unity Catalog / Volumes | Mostly pass | NO |
| M2M for auto-sync | Recommended | NO |

---

## Autodesk App Store

Engineering blockers overlap P0/P1 above. Non-engineering items (legal, branding, support, pricing) are tracked separately.

---

## Already production-ready (no change needed)

- Fernet encryption for tokens at rest
- `SECRET_KEY` from environment
- Incremental watermark semantics
- ACC data not written to connector disk (notebook download path)
- Bootstrap idempotency
- Unity Catalog Volume landing
- User-Agent on Databricks REST calls

---

## References

- [PLATFORM.md](../architecture/PLATFORM.md) — auth & ingestion decisions
- [RUNBOOK.md](../engineering/RUNBOOK.md) — operational risks
- [PITCH.md](../customer/PITCH.md) — customer-facing scope

# Plugin Launch Locations — Distribution Channels for the Connector

> **Purpose:** Capture the distribution / go-to-market decisions for the ACC ↔ Databricks
> connector — where it eventually lives, who can install it, who pays for it, and in what
> order to launch on each channel.
>
> **Audience:** Founders, GTM leads, and engineers who need to understand which features
> the product MUST support to qualify for each marketplace.
>
> **Status:** Living document. v1 strategy locked in (direct sales). v2/v3 channels
> identified but not committed.

---

## TL;DR

1. **The connector is a vendor-hosted SaaS.** Customer's data stays in their Databricks; we host the orchestration tier on our Azure App Service.
2. **There are four legitimate distribution channels** we've evaluated: Direct Sales, Autodesk App Store, Databricks Partner Connect, and Databricks Marketplace (data products).
3. **Recommended order:** Direct Sales (v1) → Autodesk App Store (v2) → Databricks Partner Connect (v3).
4. **Mature state:** listed on both source side (Autodesk) and target side (Databricks) — the bigger players in this space do this.

---

## 1. The Four Distribution Channels

| # | Channel | What it is | Who finds you here | Effort to list |
|---|---|---|---|---|
| 1 | **Direct Sales** | No marketplace. Private contracts, invoice billing, customer-specific URL. | Customers Autodesk introduces, referrals, your sales calls | Low — days |
| 2 | **Autodesk App Store** (apps.autodesk.com) | Source-side discovery for ACC / Forma users | ACC admins, project IT teams searching for "send ACC data to Databricks/Lakehouse" | Medium — APS app submission, security review, marketing assets |
| 3 | **Databricks Partner Connect** | Tile in customer's Databricks workspace under Marketplace → Partner Connect integrations | Databricks data engineers shopping for ingestion connectors | High — Partner Validation, multi-tenant SaaS, security review, SOC 2 |
| 4 | **Databricks Marketplace (data products)** | Free / paid datasets, ML models, notebooks (NOT connectors) | Databricks customers shopping for data products | Low — works as a marketing hook OR a standalone product |

---

## 2. Disambiguating the Three "Databricks" Things

People conflate these constantly. They are three distinct products with different certification tracks:

| Surface | What's inside | Who builds them | Example |
|---|---|---|---|
| **Partner Connect** | Connector apps (vendor-hosted SaaS) | Third-party vendors | Fivetran, dbt, Hevo, Airbyte |
| **Databricks Marketplace** | Data products — datasets, ML models, notebooks | Anyone (data providers, vendors) | Bloomberg datasets, Mobility Stream's Jira sample |
| **Databricks Apps** (Lakehouse Apps) | Apps that run inside the customer's Databricks workspace | Anyone | Custom dashboards, internal tools |

**In the modern UI, Partner Connect lives UNDER the "Marketplace" sidebar entry**, alongside Data Products. They share a UI shell but are separate certification pipelines.

**Apps is unrelated** — different deployment model, runs inside customer infrastructure, not a fit for our vendor-hosted SaaS shape.

**Our connector belongs in Partner Connect**, not Marketplace data products and not Apps.

---

## 3. Source-Side vs Target-Side — Where Connectors Normally Live

The convention is **driven by who the actual buyer is**:

### Target-side launch (dominant pattern for data ingestion)

If the buyer sits in the analytics tool thinking *"I need data from X here,"* the connector lives target-side.

| Connector | Source | Target | Lives in |
|---|---|---|---|
| Fivetran | 500+ sources | Databricks / Snowflake | Partner Connect (target) |
| Airbyte | Many | Databricks | Partner Connect (target) |
| dbt | Sources via warehouse | Databricks | Partner Connect (target) |
| Tableau, Power BI | Many | Various | Partner Connect (target) |

### Source-side launch (productivity / business apps pattern)

If the buyer sits in the source system thinking *"I want my data to flow out,"* the connector lives source-side.

| Connector | Source | Target | Lives in |
|---|---|---|---|
| Mobility Stream "Databricks for Jira" | Jira | Databricks | Atlassian Marketplace (source) |
| Salesforce Data Cloud connectors | Salesforce | Various | Salesforce AppExchange (source) |
| Workday / SAP exporters | Workday / SAP | Warehouses | Workday / SAP marketplaces (source) |

### Both sides (mature products)

Big vendors list in both — Fivetran has Partner Connect tiles AND presence in some source-system marketplaces.

### What this means for our product

Construction-data ingestion is closer to the **Fivetran pattern** than the Mobility Stream pattern. The decision-maker / budget-holder is usually a construction firm's data engineer (in Databricks), not the field ACC user. So:

- **Primary:** Databricks Partner Connect (target side) — matches industry convention.
- **Secondary:** Autodesk App Store (source side) — useful for ACC admins originating the request.
- **End state:** listed on both, like the bigger players.

---

## 4. The Mobility Stream Reference Point

A real comparable product proves this market exists. Mobility Stream's "Databricks for Jira" has the same shape as our connector (just with Jira instead of ACC):

- Lists in **Atlassian Marketplace** (source side) for the actual paid product.
- Lists in **Databricks Marketplace** (target side) as a **free sample dataset** that links back to the paid product.
- Uses **PAT-based Databricks auth** (customer brings their own token), not the full OAuth U2M.
- Sells to enterprise logos: Bayer, UN, Honda, Palantir.
- Has SOC 2 compliance.

**Implications for our roadmap:**

- The PAT-based auth path is officially supported by Databricks (see [Connect to Fivetran docs](https://docs.databricks.com/aws/en/partners/ingestion/fivetran), "Connect manually" section). Full OAuth U2M is only needed for higher-tier validation.
- A free sample dataset in Databricks Marketplace is a known, working lead-gen tactic.
- Listing only on the source side (no Partner Connect tile) is a viable v1 strategy — Mobility Stream's enterprise customers exist without it.

---

## 5. The Fivetran Reference Point

Looking at how Fivetran's Partner Connect listing actually works (per Databricks docs):

- Customer clicks Fivetran tile in Databricks Marketplace → Partner Connect.
- Databricks generates a PAT, picks SQL warehouse + Unity Catalog.
- Customer is redirected to Fivetran's hosted SaaS with credentials prefilled.
- Fivetran creates a customer trial account and starts the integration.

**The PAT model is the path of least resistance.** Full OAuth U2M, PKCE, refresh-token rotation are required for **Databricks Validated Partner** status (a higher tier than just being in Partner Connect), but not strictly for the Partner Connect listing itself.

**Required customer permissions** (verbatim from Databricks docs):

- Workspace admin role OR (CAN USE on a SQL warehouse + CAN USE for token usage)
- USE CATALOG and CREATE SCHEMA on the destination Unity Catalog
- (Optional) CREATE EXTERNAL TABLE + cloud storage access for custom destinations

These map directly to what our `bootstrap.py` already requires.

---

## 6. Recommended Launch Sequence

| Phase | When | What we list | Effort | Why |
|---|---|---|---|---|
| **v1** | Now → first paying customer | Direct sales only | Days | No marketplace certification needed; fastest revenue; learn the product |
| **v2** | After 1–2 customers live, ~6 months in | Autodesk App Store (ACC plugin) | Months | Modest certification effort; ACC-side discovery for inbound leads |
| **v3** | After SOC 2 + multi-tenant production stack ready, ~12–18 months in | Databricks Partner Connect (target tile) | 6–12 months | Highest-quality channel; matches Fivetran pattern; enables Databricks GTM partnership |
| **Bonus, anytime** | Once we have anonymized schema | Databricks Marketplace — free sample dataset | Days | Lead-gen tactic, $0 revenue but drives discovery |

**The eventual mature state:** listed in **all** of them. They are not mutually exclusive.

---

## 7. Hosting & Operating Implications (Same Across All Channels)

Regardless of which marketplace(s) we list in, the connector itself is **vendor-hosted SaaS**:

| Component | Where | Approx monthly cost |
|---|---|---|
| Web tier | Azure App Service (Linux, Python) | $200 |
| State DB | Azure Database for PostgreSQL | $100 |
| Background workers | Celery on App Service + Azure Cache for Redis | $50 |
| Secrets | Azure Key Vault | $10 |
| Logs / monitoring | Azure Application Insights | $50 |
| Bandwidth | Variable | $50–200 |
| **Total** | | **~$500–700/month** baseline |

**Maintenance responsibility:** ours. Every channel — direct sales, App Store, Partner Connect — assumes we run the SaaS, do 24/7 on-call, deploy updates, handle security incidents, and pass annual SOC 2 audits.

**Critical implication for the Autodesk deal:** "build it for some amount" must either (a) include a recurring operating retainer, or (b) be paired with customer-paid SaaS subscriptions that fund ongoing operations. A one-time build fee with no recurring revenue means we lose money the moment the first customer goes live.

---

## 8. Decisions Required Before We Commit to a Channel

1. **What does Autodesk actually want?** One-time build for one specific customer, or productized SaaS to distribute through their channels?
2. **Branding model?** "[OurCompany] Connector for ACC" vs "Autodesk Lakehouse Connector powered by [OurCompany]"?
3. **Listing ownership?** Our listing, Autodesk's listing, or co-listed?
4. **Customer support owner?** Us, Autodesk, or split by tier?
5. **Pricing model?** Per-project, per-user, per-volume, flat enterprise?
6. **Multi-tenant or single-customer?** Determines whether the production checklist's multi-tenant work is required.

These need clarity before committing to any of v2 / v3 marketplace certifications, because the answers shape what gets built.

---

## 9. Revenue Model Per Channel

| Channel | Revenue model | Marketplace cut |
|---|---|---|
| Direct sales | Per-customer SaaS subscription, invoiced annually | 0% — full margin |
| Autodesk App Store | Per-customer subscription, billed via Autodesk | ~15–30% (typical app store rate) |
| Databricks Partner Connect | Per-customer SaaS subscription, billed direct or via Databricks | Direct: 0%; via Databricks: ~25% |
| Databricks Marketplace (paid dataset) | Per-customer data subscription | ~25% |
| Databricks Marketplace (free sample) | $0 revenue, lead-gen for paid product | N/A |

**Implication:** direct sales has the highest per-customer margin but lowest discoverability. Marketplaces trade margin for reach. Mature companies use both — direct for high-touch enterprise deals, marketplaces for self-service inbound.

---

## 10. References

- Databricks Partner Connect catalog: https://www.databricks.com/partnerconnect
- Connect to Fivetran via Partner Connect (canonical example): https://docs.databricks.com/aws/en/partners/ingestion/fivetran
- Mobility Stream "Databricks for Jira": https://mobilitystream.com/databricks-for-jira/
- Mobility Stream installation flow: https://docs.mobilitystream.com/dbr/installation
- Autodesk App Store: https://apps.autodesk.com
- Related repo docs: [SESSION_STATE.md](SESSION_STATE.md), [DATABRICKS_PARTNER_INTEGRATION_CHECKLIST.md](DATABRICKS_PARTNER_INTEGRATION_CHECKLIST.md)

---

*Last updated: May 9, 2026*

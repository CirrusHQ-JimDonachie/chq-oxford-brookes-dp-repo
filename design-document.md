# Oxford Brookes University - Building Data PoC
## Solution Design and Rationale

Audience: SoW author. This document describes exactly what is being built for the PoC, and why it is built that way. Scope is locked to the seven deliverables in the internal quote - nothing here adds to that scope. Production follow-ups are listed at the end and are explicitly not part of this build.

---

## 1. Overview

Oxford Brookes University (OBU) wants to understand how its buildings and spaces are used by combining data that currently sits in separate systems: room bookings/timetabling, occupancy sensors (Smart-Vis), Wi-Fi/network device counts, BMS environmental readings, and external weather. Their incumbent tool (Smart-Vis) cannot combine datasets - that combination is the core value proposition of this PoC.

The PoC is a fully serverless, single-account, single-environment (eu-west-2) build that proves four mechanisms end to end:

1. **Ingest and combine** - customer-dropped files from multiple sources are validated, normalised and landed in a governed Apache Iceberg lakehouse.
2. **Analyse and ask** - dashboards, multi-dataset comparison reports, and natural-language querying via QuickSight + Q.
3. **Alert** - threshold breaches (e.g. room temperature) detected on data arrival and delivered as email within seconds of the file landing.
4. **Serve live state** - an authenticated read API exposing current room/occupancy state for a customer-built dashboard.

The overriding design constraint: **production capability must be additive**. Every deferred feature (ServiceNow ticketing, BMS actions, streaming, web app, Bedrock NL, WAF, SSO) bolts onto the delivered core - the lake, the stores, the event bus, the read API - with no teardown or rework. The PoC is the production foundation, deliberately stripped to the minimum that proves each mechanism.

---

## 2. Requirements coverage

| Customer requirement (use-cases doc) | PoC treatment |
|---|---|
| UC1 - Booked vs actual occupancy: dashboards, multi-dataset comparison, NLP | **In scope** - curated Iceberg tables joined in Athena views, QuickSight dashboards + Q Topic |
| UC2 - Social space usage: multi-dataset reports, building visualisation, NLP | **In scope** for reports/NLP. Visualisation is via QuickSight (charts/heatmaps by zone), not a floor-plan/3D twin (see follow-ups) |
| UC3 - Internal/external factors (BMS + weather combined with usage) | **In scope** - BMS and weather are onboarded sources like any other; correlation analysis via QuickSight/Q over joined curated tables |
| UC4 - Actions: BMS webhook, ServiceNow ticket on threshold, room-availability app | **Mechanism proven, actions deferred.** The detect -> event -> notify loop is built and demonstrated (EventBridge -> SNS email). ServiceNow/BMS targets and the student app are additive follow-ups on the same bus/API |
| UC4 - AP-down detection | **Deferred** - absence detection (missing heartbeats) is a different mechanism from value-threshold detection, its action (the ServiceNow ticket) is itself deferred, and the customer's network monitoring likely already covers it |
| NFR - 1-minute processing / 30-second actions | **Bounded by ingestion cadence.** The pipeline itself processes and alerts within seconds of a file landing; end-to-end latency is dominated by how often the customer drops files. Sub-minute end-to-end needs a streaming source (follow-up) |
| NFR - GDPR / no PII | Customer supplies aggregated/anonymised data (no PII enters the platform); all stores SSE-KMS via a customer-managed key; eu-west-2 only |
| NFR - RBAC | Two deliberately separate models: QuickSight row-level security for the analytics surface; Cognito user pool + groups for the read API |
| NFR - Scalability / extensibility | Config-driven onboarding (new source = new config row + mapping), Iceberg schema evolution, parameterised IaC. Designed to scale; not load-tested in the PoC |
| NFR - Cost forecasting, 99.9% uptime, accuracy targets | Production targets, not guaranteed by the PoC. Cost estimation is a separate activity |

---

## 3. Architecture

Fully serverless - no VPC exists anywhere in the design. Reference diagram: `hld.png` alongside this document.

**One engine, two paths.** A single config-driven Lambda (the transform + threshold engine) is triggered by S3 event notification when the customer drops a file into the raw landing bucket. In that one invocation it:

1. Reads the per-path mapping config from DynamoDB (`raw/{source}/{type}/...` determines which config applies).
2. Validates the input; records that fail validation are written to an `error/` prefix (data problem, not a platform problem).
3. Writes canonical rows into the curated Iceberg tables (the **cold path** - feeds Athena, QuickSight and Q).
4. Upserts the current-state snapshot and alert state into DynamoDB (the **hot path** store - feeds the read API).
5. Evaluates readings against alert rules and, on an OK -> BREACH transition, emits a status-change event to a custom EventBridge bus, which routes it to SNS for email delivery.

**Cold path (analytics):** curated S3 Tables (managed Iceberg V2) -> auto-mounted into the Glue Data Catalog via the s3tablescatalog federated catalog -> governed by Lake Formation -> queried by Athena -> QuickSight SPICE datasets (hourly refresh) -> dashboards + Q Topic for natural language.

**Hot path (live state + alerting):** DynamoDB current-snapshot table -> API Gateway REST API with a native DynamoDB integration (no Lambda on the read path) -> Cognito-authenticated consumers. Alerting: EventBridge bus -> SNS email, with alert-state tracked in DynamoDB so notifications fire on state *transitions*, not on every reading.

Everything is deployed as parameterised IaC through a CI/CD pipeline (Deliverable 1).

### 3.1 Worked example: one file, end to end

Illustrative only - field names, key shapes and the config schema below are indicative and get finalised per source against real sample data during D5 onboarding. They are not contractual.

**1. The customer drops a BMS environmental export:**

```
s3://obu-poc-raw/raw/bms/environmental/2026/06/25/nhhb-env-20260625T1005.csv
```

```csv
SensorRef,Timestamp,RoomTemp,RH
NHHB-2.14,1750845900000,262,55
```

**2. S3 event notification -> transform engine.** The S3 event (prefix `raw/`) asynchronously invokes the Lambda. The engine parses the key: `raw/{source}/{type}/...` -> `source=bms`, `type=environmental` -> config lookup in the config table.

**3. Config lookup (DynamoDB).** Example mapping-config item the path resolves to:

```json
{
  "pk": "config#bms#environmental",
  "input_format": "csv",
  "has_header": true,
  "fields": [
    { "from": "SensorRef",  "to": "room_id",       "type": "string" },
    { "from": "Timestamp",  "to": "reading_at",    "type": "timestamp", "format": "epoch_ms" },
    { "from": "RoomTemp",   "to": "temperature_c", "type": "decimal",   "scale": 0.1 },
    { "from": "RH",         "to": "humidity_pct",  "type": "decimal" }
  ],
  "validation": {
    "required": ["room_id", "reading_at"],
    "ranges": { "temperature_c": { "min": -20, "max": 60 } }
  },
  "target_table": "s3tablescatalog/obu-curated/building_data/bms_environmental"
}
```

Onboarding a new source (D5) = agreeing the file format with the customer and writing one of these config items; no new infrastructure and no new code. Every agreed source must be expressible this way - **the SoW excludes bespoke per-source transform code**, so a source that cannot be mapped declaratively is a Change Control item, not an engine change.

**4. Validate and transform.** The row passes validation (`RoomTemp 262` x scale `0.1` = 26.2 degC, within range). A row that failed - missing `room_id`, temperature 700 - would be written to `s3://obu-poc-raw/error/bms/environmental/...` with a **machine-readable reason code** attached, counted on the rejected-record custom metric (dimensioned by source and reason), and processing would continue. Bad data raises the lower-severity data-quality alarm, never the platform on-call (see 5.8).

**5. Cold path - curated Iceberg write.** The canonical row lands in the target table (auto-mounted in Glue, governed by the Lake Formation WRITE grant):

```json
{ "room_id": "NHHB-2.14", "reading_at": "2026-06-25T10:05:00Z", "temperature_c": 26.2, "humidity_pct": 55.0, "source_file": "nhhb-env-20260625T1005.csv", "ingested_at": "2026-06-25T10:05:41Z" }
```

Athena can query it immediately; QuickSight sees it on the next SPICE refresh.

**6. Hot path - snapshot upsert + threshold evaluation.** The engine upserts the current-state item in the state table (this is what the D7 read API serves):

```json
{ "pk": "room#NHHB-2.14", "sk": "snapshot", "temperature_c": 26.2, "humidity_pct": 55.0, "updated_at": "2026-06-25T10:05:41Z" }
```

It then evaluates the reading against the alert rules for that entity - rules are config rows in the same table family:

```json
{ "pk": "rule#room#NHHB-2.14", "sk": "temperature_c", "comparison": ">", "threshold": 26.0, "severity": "warning" }
```

26.2 > 26.0 -> breach. The engine reads the alert-state item, finds `state: OK`, and performs a **conditional** update (precondition `state = OK`) to `state: BREACH`, stamping `breach_started_at` - the conditional write is what makes two near-simultaneous readings race-safe (only one wins the transition).

**7. Event -> email.** Because this was an OK -> BREACH *transition* (not just a breach reading), the engine emits a status-change event to the EventBridge bus; a rule routes it to the SNS topic and estates/IT get the email.

**What deliberately does not happen next:**
- The next file shows 26.4 degC -> still BREACH -> snapshot updated, **no event, no email** (transition-only alerting; no notification storm).
- A later file shows 24.8 degC -> BREACH -> OK transition -> state resets (with a recovery notification if configured), and the *next* breach will alert again.
- If the Lambda itself blew up on the file (bug, timeout, poison input), the event lands in the SQS DLQ after async retries and the DLQ-depth alarm fires - a platform failure, distinct from the `error/` data path in step 4.

---

## 4. Deliverables

Scope is exactly the seven deliverables in the internal quote.

### D1 - IaC Deployment Pipeline
CI/CD pipeline that builds and deploys every other deliverable as IaC: CodeConnections connection to the customer's repo, CodePipeline, CodeBuild project + service role, artifact bucket, pipeline service role, IaC execution role, build log group. The deploy action's permissions sit on the pipeline role while the IaC execution role is **separate**, so provisioned-resource permissions stay split from pipeline permissions. **IaC flavour is settled: CloudFormation** - the deploy stage is a CloudFormation deploy action assuming the separate execution role. CI/CD tooling is confirmed at kickoff; if the customer has an existing pipeline, this deliverable adapts to it. **Manual post-apply step:** the CodeConnections connection is created PENDING and a human must authorise the installation app in the provider before the pipeline can pull source.

**Status (2026-09-23): not yet built.** The sandbox deployment this far has been entirely manual (`sam build`/`sam package`/`sam deploy`) - zero CodePipeline/CodeBuild/CodeConnections resources exist yet. This waits on the Customer granting access to their GitHub repo and AWS account; the plan is CodeConnections against that repo, triggering on changes to its main branch.

### D2 - Foundation
The shared baseline: one customer-managed KMS key (single trust boundary - encrypts S3, DynamoDB, SNS, SQS and log groups; automatic rotation; treat as non-destroyable since losing it bricks every encrypted store), the SNS alert topic (sole alert sink for the PoC; email subscriptions; also the CloudWatch alarm target), the one-time s3tablescatalog-to-Glue integration, and Lake Formation data-lake settings + catalog/database-level grants. Foundation carries the account-level items deliberately - see implementation notes on ordering.

### D3 - Data Stores and Lake
The stateful core, all retain-on-delete: two DynamoDB tables (one for mapping configs + alert rules; one for alert state + the current-snapshot hot store, with TTL, GSI for the read access pattern, PITR), the raw landing bucket (path-per-source convention, `error/` prefix, aggressive lifecycle expiry - raw is re-droppable, not precious), the curated S3 Tables bucket (managed Iceberg V2), and the Athena results bucket.

### D4 - Catalog, Query and Governance
Glue Data Catalog (the curated tables auto-mount via the federated catalog - no authored Hive table definitions, no crawler), the Athena workgroup, and the Lake Formation per-principal grants: WRITE to the transform function (the Iceberg writer), SELECT to Athena and the QuickSight service role. Without these grants readers get nothing regardless of IAM - see implementation notes.

### D5 - Ingestion, Transform and Eventing
The transform + threshold engine (single Lambda, single S3 trigger), its execution role and log group, the SQS dead-letter queue for function failures, the EventBridge custom bus + rule routing breach events to SNS, and **three** CloudWatch alarms (DLQ depth > 0; transform error rate; rejected-record count). The engine emits a **custom CloudWatch metric counting rejected records, dimensioned by source and rejection reason**, and writes each rejected record to `error/` with a **machine-readable reason code** - this is what makes the SoW's three failure categories (data issues, unsupported data, general failures) visible rather than silent. Includes **per-source onboarding for up to five agreed sources**: mapping configuration (field mapping, types, units, validation) and file-format agreement/sample-data validation with the customer. **Every source must be declaratively mappable - bespoke transform code is out of scope** and goes to Change Control. Exactly **one alert rule** is configured and demonstrated end to end.

### D6 - Analytics and Natural Language (QuickSight + Q)
Athena data source, SPICE dataset(s) on hourly refresh (cross-dataset joins done in Athena views so datasets land flat), up to **two dashboards** and exactly **one** Q Topic (the natural-language semantic model - friendly names, synonyms, semantic types, field descriptions, sample questions), row-level security, and the Pro author role for generative BI. Licensing is customer-borne AWS usage: **Enterprise Edition**, at least one **Author Pro** (creating a Q Topic is an Author Pro capability), Reader or Reader Pro per dashboard consumer, plus the per-account Amazon Q infrastructure fee. **The Q Topic modelling is where the real effort and the accuracy live** - the plumbing is trivial by comparison. QuickSight's built-in ML forecasting (seasonality-aware, point-and-click on time-series visuals) comes free with this deliverable and can be demonstrated on occupancy trends; it is univariate only (see follow-ups for multivariate prediction).

### D7 - Read API
API Gateway REST API with a native AWS-service integration to DynamoDB (no Lambda on the read path), authenticated by a native Cognito User Pool authorizer. Cognito user pool + app client, no hosted UI. The **Supplier creates the initial read API users** from customer-supplied names/e-mails; the customer manages further users after handover. The customer's dashboard authenticates via InitiateAuth (SRP) for a JWT and sends it as a Bearer token. Plus the API Gateway integration role and log group. Proven end to end with a curl using an InitiateAuth token.

---

## 5. Design decisions

Each decision below states what was chosen, why, and what was rejected. These are the load-bearing choices; the SoW should not re-open them without understanding the consequence column.

### 5.1 Fully serverless, no VPC
**Decision:** Every component is a managed/serverless service; there is no VPC, no subnets, no NAT.
**Why:** Nothing in the design needs network placement - S3, DynamoDB, Lambda, EventBridge, SNS, SQS, Athena, Glue, QuickSight, API Gateway and Cognito are all regional services. A VPC would add cost and IaC surface for zero benefit.
**Rejected:** VPC-resident design. There is no EC2, no RDS, no container runtime anywhere in scope.
**Consequence:** If a future source requires private connectivity to on-prem (e.g. a BMS that can only be reached over VPN/DX), that lands with the streaming/poller follow-up, not this build.

### 5.2 File-drop ingestion; no pollers, no streaming
**Decision:** The customer extracts data from their systems and drops files into the raw bucket. The platform does not connect to source systems.
**Why:** Every source integration (Smart-Vis API, Wi-Fi controller API, BMS protocol, booking system) is an unknown with its own auth, format and reachability questions - the highest-uncertainty part of the whole engagement. Pushing extraction to the customer removes all of it from the PoC's critical path, removes the need for any Secrets Manager footprint (nothing in the PoC holds a credential to a customer system), and still proves everything downstream of landing.
**Rejected:** Scheduler-driven API pollers per source (adds per-source auth/format integration risk); edge gateways (SiteWise/Greengrass - heavy, assumes protocol access); streaming ingestion (no streaming source exists yet).
**Consequence:** End-to-end latency is bounded by drop frequency. The alerting mechanism is proven end to end, but the requirements' sub-minute/30-second targets need a streaming source - an explicit, priced follow-up rather than a silent gap. If one source later needs automated pull (e.g. weather), a single scheduled poller can be added without redesign.

### 5.3 S3 Tables (managed Apache Iceberg V2) for the curated layer
**Decision:** The curated store is an S3 Tables table bucket - managed Iceberg V2 - not plain Parquet.
**Why:** The customer's stated direction is to keep adding datasets whose shape isn't known yet ("throw all sorts of stuff in there and compare"). Iceberg gives schema evolution (add/rename/drop columns without rewrites) and MERGE semantics for corrections; S3 Tables specifically removes the classic Iceberg operational tax (compaction, snapshot expiry, orphan cleanup are managed). This is "PoC as production foundation" in storage form - no later migration from a PoC format to a production one.
**Rejected:** Plain Parquet + Athena partition projection (cheaper for pure append-only, but a dead end for schema evolution and a guaranteed migration later - exactly the teardown this design forbids); Iceberg V3 (Athena cannot query V3; V3/Variant would force a different query engine and unravel the QuickSight/Athena story - see follow-ups).
**Consequence:** Curated tables are Athena-compatible (V2). Truly unstructured/variant data is out of scope until a non-Athena engine is added.

### 5.4 Lake Formation governance (not optional)
**Decision:** Lake Formation governs the curated store, with account-level settings + catalog/database grants in foundation and per-principal grants placed with their consumers (WRITE with the transform engine, SELECT with Athena/QuickSight).
**Why:** Not a style choice - S3 Tables access runs through Lake Formation. IAM permission on a role is necessary but not sufficient; without the LF grant, reads and writes fail.
**Consequence:** Grant management is part of the build, and the SELECT grant to the QuickSight service role is a mandatory step - miss it and QuickSight sees nothing. Column/row-level filters are available later without rework if finer access control is ever needed.

### 5.5 No Glue crawler, no partition projection, no authored table definitions
**Decision:** None of the classic Hive-era catalog machinery exists in this design.
**Why:** All three are solutions to problems Iceberg doesn't have. Iceberg tables are self-describing (schema and partition spec live in table metadata); Athena prunes from Iceberg metadata, so partition projection (a Hive-table mechanism) doesn't apply; and the s3tablescatalog federated catalog auto-mounts every table bucket into Glue (bucket -> child catalog, namespace -> database, table -> table), so there is nothing to crawl and nothing to author. The transform's mapping config already defines each source's schema - there is nothing to "discover".
**Consequence:** New curated tables become queryable the moment they're created. A crawler only ever returns as a follow-up if someone starts dropping ad-hoc non-Iceberg data outside the pipeline - which is not how this platform works.

### 5.6 Single Lambda transform engine, config-driven
**Decision:** One Lambda is the entire processing layer. Per-source behaviour is data, not code: a DynamoDB config row per source/output-type (field mapping, types, unit conversions, validation rules), and nothing else. The SoW excludes bespoke per-source transform code, so there is no custom-transform hook: a source that cannot be expressed in the mapping config goes to Change Control rather than into the engine.
**Why (vs Glue ETL):** PoC volumes are megabytes; Glue Spark brings cluster spin-up latency and DPU-hour minimums for no benefit, and a Spark job could never also serve a future streaming/inline invocation - Lambda keeps one codebase for both. **Why config-driven:** onboarding a new source becomes "add a config row + agree the format" rather than "write and deploy a new pipeline" - this is the extensibility requirement made concrete, and it is what makes D5's per-source onboarding cheap and repeatable.
**Rejected:** Glue ETL (weight, cost, no streaming future); one Lambda per source (code sprawl, N deployments, no shared validation).
**Consequence:** Source acceptance now has a hard gate - sample data must be structured, tabular (CSV/JSON/JSONL) and consistently typed, and format sign-off happens per source before its pipeline is built. Anything awkward is priced through Change Control, not absorbed. If a single transform ever outgrows Lambda's 15-min/10GB ceiling at production scale, that specific job can move to Glue without touching the architecture.

### 5.7 Single trigger - the cold and hot paths share one invocation
**Decision:** The engine has exactly one trigger: the S3 file-drop notification. That one invocation does the Iceberg write, the state upsert, and the threshold evaluation.
**Why:** With file-drop ingestion there is no separate real-time input, so a second trigger would be dead code. The "one engine, two triggers" shape only returns if a streaming source is added (follow-up) - the engine's code is structured so the inline invocation can be added without redesign.

### 5.8 Two failure paths, deliberately distinct
**Decision:** Known-bad *data* (fails validation) is written to the `error/` prefix in-code and does not page anyone. *Function* failures (exception/timeout/poison event) land in an SQS DLQ after Lambda's async retries, and DLQ depth > 0 alarms to SNS.
**Why:** A malformed CSV from a customer system is routine and inspectable at leisure; broken processing is an incident. Conflating them either pages people for bad data or silently swallows real failures.
**Consequence:** Three alarms total, all routed to the same SNS topic: DLQ depth and transform error rate (platform failures, on-call actionable) plus a rejected-record count alarm on the engine's custom metric (data-quality, actionable by the customer's data owners). The engine emits that metric dimensioned by source and rejection reason, and stamps a machine-readable reason code on every record it writes to `error/`, so the three failure categories the SoW names - data issues, unsupported data, general failures - are each visible rather than silent. Reviewing and remediating `error/` records is a customer activity: the Supplier provides the alarm and the reason code, not the data correction. DLQ redrive is manual in the PoC.

### 5.9 EventBridge bus -> SNS email only; alert-state machine in DynamoDB
**Decision:** Breach events go onto a custom EventBridge bus and a single rule routes them to the SNS topic. Alert state (OK/BREACH per entity+metric, with conditional-write dedup and an alert-sent marker) lives in DynamoDB; notifications fire only on state transitions.
**Why the bus at all, with one consumer:** the bus is the extension point the whole action roadmap hangs off. ServiceNow ticketing, BMS webhook actions and absence detection all attach as new rules/targets on this same bus - the PoC proves the detect -> event -> notify loop, and production adds consumers without touching the detector. EventBridge publishes to SNS via the topic resource policy (no IAM role, no Step Functions on this path).
**Why the state machine:** threshold logic without transition tracking sends an email per reading while a room is hot - a notification storm that discredits the demo. Conditional writes make the transition race-safe; `breach_started_at` is stored so duration-based rules ("over threshold for 10 minutes") can be added as config later.
**Rejected for the PoC:** EventBridge API Destinations to ServiceNow/BMS + Step Functions failover wrapper + its DLQ + Secrets Manager credentials - the BMS contract and ServiceNow instance/schema were all TBC, making the action layer unpriceable now. It is the first follow-up, and it is purely additive.

### 5.10 Threshold-on-arrival only; no absence detection
**Decision:** The PoC detects bad *values* in data that arrives. It does not detect data that *fails to arrive* (AP down, sensor stopped).
**Why:** Absence detection is a different mechanism (scheduled staleness sweep over last-seen markers), its triggering use-case's action (the ServiceNow ticket) is itself deferred, and the customer's network monitoring almost certainly already alerts on AP-down - building it now would duplicate their tooling to feed an action layer that doesn't exist yet.
**Consequence:** UC4's AP-down scenario is explicitly follow-up; the sweep bolts onto the same bus and state table later.

### 5.11 DynamoDB for config, rules, alert state and the hot store
**Decision:** One DynamoDB table holds mapping configs + alert rules; a second holds alert state + the current-snapshot hot store (TTL on state items, GSI shaped for the read API's access pattern, PITR on both).
**Why:** These are point-lookup, single-digit-ms, key-shaped workloads - exactly DynamoDB's lane. Keeping config out of S3 also means the raw bucket's lifecycle can be aggressive without risking config loss, and the hot store is what makes the read API fast without a cache layer. Athena is deliberately *not* on any live path - it is the analytical engine, not an operational store.
**Consequence:** The GSI must be shaped to the dashboard's actual queries (access patterns are design-time in DynamoDB). PITR is operational recovery, not a GDPR retention control - retention is the TTL and lifecycle rules.

**Built (2026-09-23):** `EntityTypeIndex` - hash key `entity_type` (the mapping config's `entity.type`, e.g. `"room"`), range key `pk`, full projection. It lists every entity of one type; it does not filter by a finer business category (e.g. a `space_type` of `computer_room`) since no mapping config maps such a field yet. The read API itself still only does single-item `GetItem` by room ID and does not use this index - it exists for the not-yet-built dashboard access pattern.

### 5.12 QuickSight + Q as the entire analytics and NL surface
**Decision:** QuickSight (with SPICE datasets on hourly refresh) is the dashboarding, reporting and natural-language surface. Users ask questions inside QuickSight via a curated Q Topic. No web app, no Bedrock in the PoC.
**Why:** It satisfies UC1-UC3's dashboards, comparison reports and NLP demonstration with zero front-end build and governed access (row-level security). SPICE is chosen over direct query for dashboard speed and Athena-cost control - volumes are trivially within SPICE limits - and hourly refresh matches the analytical freshness need (live data is the hot path's job, not SPICE's).
**Two honest limits, stated so the SoW doesn't oversell:** (1) **Q is not dynamic over the catalog** - it answers only over datasets modelled into Topics; a new table in Glue does not auto-appear. The Topic modelling (synonyms, semantic types, sample questions) is the real effort and where the accuracy target lives - budget it as such, driven by the customer's question set. (2) Q's built-in forecasting is univariate (a metric from its own history + seasonality) - useful for "expected occupancy next week" demos at no extra build, but multivariate prediction (occupancy given weather + bookings) is SageMaker follow-up territory.
**Rejected for the PoC:** Bedrock text-to-SQL (the "ask anything across any dataset" capability - a real build with an accuracy problem to own; follow-up); Amazon Q Business (document RAG - wrong tool for structured telemetry); a custom web front-end (out of scope by customer decision).
**Consequence:** Cross-dataset joins are built into Athena views/curated tables so datasets land flat - Q accuracy over well-modelled flat tables is far better than over view-time joins. QuickSight never touches Iceberg directly; Athena abstracts the format entirely.

### 5.13 Read API: API Gateway -> DynamoDB native integration, Cognito authorizer
**Decision:** A REST API with a native AWS-service integration mapping directly to DynamoDB (no Lambda on the read path), authorized by a native Cognito User Pool authorizer. Cognito pool + app client with admin-created users and no hosted UI; the customer's dashboard obtains a JWT via InitiateAuth (SRP) and sends it as a Bearer token.
**Why no Lambda:** the read is a straight key/GSI lookup - VTL mapping does it with less latency, less cost and no code to own. REST API (not HTTP API) is required for both the native DynamoDB integration and the Cognito user-pool authorizer.
**Why Cognito (the auth decision, in order of rejection):** API keys are metering, not auth. IAM/SigV4 would force AWS credentials into a customer-built browser dashboard - an anti-pattern. A Lambda PSK authorizer is a custom Lambda plus a weak static secret. Cognito's user-pool authorizer is native (no code), gives real per-user identity and JWTs, and Cognito groups provide the future per-role RBAC path; Entra/Azure AD federation later attaches to this same pool (the hosted UI returns then - federated sign-in is a redirect flow, which is why the PoC doesn't build it).
**Consequence:** Two independent RBAC models exist by design - QuickSight RLS governs the analytics surface; Cognito governs the API. They serve different consumers and should not be merged.

### 5.14 Single customer-managed KMS key
**Decision:** One CMK encrypts everything (S3, DynamoDB, SNS, SQS, logs).
**Why:** One trust boundary, one key policy, one place to audit - proportionate for a single-account PoC holding no PII. Per-service keys add policy surface without a boundary that justifies it here.
**Consequence:** The key policy grows as each deliverable adds its role as a grantee. The key must be retained on stack delete - losing it bricks every encrypted store.

### 5.15 Deployment via CI/CD pipeline (D1)
**Decision:** All infrastructure deploys through a pipeline from a source repo, as parameterised IaC.
**Why:** Repeatable deploys during the build, and the pipeline itself is part of the production foundation being proven - production promotion later is a parameter change, not a process change. IaC flavour is settled as **CloudFormation**; CI/CD tooling (customer's existing vs CodePipeline) is confirmed at kickoff and the deliverable adapts.

---

## 6. Implementation notes

Things a builder must get right; each has bitten someone before.

- **Ordering: the s3tablescatalog integration is an account+region singleton and bucket-independent.** Enable it in foundation, before any table bucket exists - there is no chicken-and-egg. Every table bucket (current and future) then auto-mounts. Enable once per account/region (one-time console/CLI bootstrap or a single foundation-stack resource: `aws_glue_catalog` with `federated_catalog` / `AWS::Glue::Catalog`) - never per project stack; it is account-global.
- **Lake Formation grants:** account-level data-lake settings and catalog/database-level grants cascade to tables created later, so foundation can grant before tables exist. Per-principal grants land where consumed: WRITE -> transform role (D5), SELECT -> Athena workgroup + QuickSight service role (D6). IAM allow without the LF grant = access denied.
- **Iceberg V2 <-> Athena is a hard pairing.** Athena cannot read V3. Do not let anyone "upgrade" the table format without also planning the query-engine change.
- **Curated tables reach Glue with no per-bucket setup** (bucket -> child catalog, namespace -> database, table -> table; queried as `s3tablescatalog/<bucket>/<namespace>/<table>`). Tables can be IaC-created or created at runtime by the transform engine for evolving datasets.
- **SPICE refresh floor is hourly** (scheduled); on-demand refresh via CreateIngestion is rate-limited to tens per day - do not design anything that assumes minute-level SPICE freshness. Live data is the hot path's job.
- **Joins belong in Athena views / curated tables**, not QuickSight - Q accuracy depends on flat, well-modelled datasets.
- **Alert-state writes must be conditional** (state = OK precondition on the OK -> BREACH transition) or near-simultaneous readings double-alert.
- **EventBridge -> SNS uses the topic resource policy** - no IAM role on that hop.
- **Cognito flow:** users are admin-created (`AdminCreateUser`); the client authenticates with `InitiateAuth`/USER_SRP_AUTH (SDK handles SRP). No hosted UI or domain exists in the PoC. The curl proof uses an InitiateAuth/AdminInitiateAuth token.
- **REST API, not HTTP API** - the native DynamoDB service integration and the Cognito user-pool authorizer both require it.
- **Retention:** aggressive lifecycle expiry on `raw/` is safe (files are re-droppable; configs live in DynamoDB); Iceberg compaction/snapshot expiry is managed by S3 Tables; DynamoDB TTL is the retention control, PITR is recovery.
- **Service quotas at production scale** (not PoC blockers, on the radar): Lambda concurrency for the engine, QuickSight SPICE capacity - both console/Support raises, not IaC.

---

## 7. Production follow-ups (explicitly not in this build)

Everything below is additive to the delivered core - no teardown, no rework. Listed so the SoW can reference them as future phases; none of them change what is built above.

| Follow-up | Attaches to | Notes |
|---|---|---|
| ServiceNow ticketing + BMS action webhook | The D5 event bus | EventBridge API Destinations (+ Step Functions failover wrapper, DLQ, Secrets Manager creds). Deferred because the BMS contract and ServiceNow instance/schema are TBC |
| Absence detection (AP-down / sensor-stopped) | Same event bus + state table | Scheduled staleness sweep over last-seen markers. Confirm the customer's network monitoring doesn't already cover it before building |
| Real-time / streaming ingestion | The same transform engine (inline invocation) | What lifts end-to-end latency to the sub-minute/30-second NFR targets |
| Student web app + Entra/Azure AD federation | The D7 read API + Cognito pool | App consumes the existing API; federation adds the IdP + hosted UI (redirect flow) to the same pool |
| Bedrock natural language (text-to-SQL) | The Glue catalog + lake | Two routes: managed (Bedrock KB structured retrieval + Redshift Serverless over the same S3 Tables - note structured KB requires Redshift as the engine, not Athena) or custom (Bedrock Agent + Lambda + Athena). This is the "ask anything, including brand-new datasets" capability Q deliberately doesn't attempt |
| Predictive ML (multivariate forecasting) | The lake (train) + QuickSight datasets (surface) | SageMaker (Canvas or DeepAR-class models) for occupancy-given-weather-and-bookings prediction; predictions feed back into datasets so Q surfaces them. Note: Amazon Forecast is closed to new customers - SageMaker is the path |
| WAF | The D7 API Gateway stage | Managed rule sets + rate limiting |
| Multi-region / DR / backup beyond PITR + versioning | The existing stores | Production resilience posture |
| Iceberg V3 / Variant | The curated tables | Only with a non-Athena engine; extends the V2 lake for unstructured shapes |
| Glue crawler / auto-discovery | The catalog | Only if ad-hoc non-Iceberg data ever bypasses the pipeline |
| Multi-building scale | Parameterised IaC | Same architecture; needs quota raises + load validation |
| 3D digital twin / floor-plan visualisation | The lake | TwinMaker-class capability; needs spatial/BIM data the PoC doesn't require |

Separate activities (not bolt-ons): AWS cost estimation for production; penetration testing / formal security review / GDPR certification.

---

## 8. Assumptions and customer dependencies

Condensed from the quote (the quote wording is authoritative for the SoW):

- Single AWS account, single environment; customer provides account access and owns all AWS costs.
- Customer provides the source repo / confirms CI/CD tooling for D1.
- **Customer extracts and drops data as files** to the agreed S3 locations in agreed formats; the platform never connects to source systems.
- Customer confirms the source set (sizes D5's per-source onboarding) and provides sample data + format sign-off per source before its pipeline is built.
- **No PII enters the platform** - customer aggregates/anonymises (Wi-Fi/device data especially) before drop.
- Customer supplies alert thresholds, the NL question set for the Q Topic, alert email addresses, and creates/manages Cognito users via the console.
- Historical backfill, if wanted, is customer-supplied data through the same drop mechanism.
- The PoC demonstrates mechanisms; production NFR targets (accuracy percentages, 99.9% uptime, response times) are not certified by the PoC.

# WorkHunter architecture

WorkHunter is a local-first pipeline for discovering and evaluating software
engineering opportunities. It is intentionally designed as a single-user
service with durable local state and explicit operational boundaries.

```mermaid
flowchart LR
    A[Public feeds and APIs] --> B[Collectors]
    C[LinkedIn browser extension] --> D[Local ingest queue]
    B --> E[Normalization and identity]
    D --> E
    E --> F[Eligibility and evidence]
    F --> G[Gemini or OpenRouter]
    F --> H[Heuristic fallback]
    G --> I[Decision audit]
    H --> I
    I --> J[SQLite and local state]
    I --> K[Notification adapters]
    K --> K1[Telegram]
    K --> K2[Email]
    K --> K3[WhatsApp Cloud optional]
    J --> L[Health and heartbeat]
```

## Boundaries

- Collectors retrieve public listings and record collection evidence.
- The LinkedIn extension submits only content visible in the user's browser to
  a loopback-only receiver.
- Normalization and source identity make URLs and native IDs comparable.
- Deterministic eligibility and evidence checks run before an LLM call.
- Gemini is the default provider; OpenRouter is an alternative. If no provider
  is available, the heuristic path keeps the cycle alive.
- SQLite stores durable local decisions, manual cases and recovery information.
- Telegram is a delivery channel, not the source of truth.
- Delivery uses adapters so channels can be compared or changed without
  changing collection, scoring or audit logic. WhatsApp remains disabled until
  official credentials and an approved template are configured.
- systemd keeps the local services running and restarts recoverable failures.

## Reliability model

Timeouts, HTTP failures and rate limits are recorded and handled per source.
The manual inbox has its own worker so a user-submitted job does not have to
wait for the scheduled radar cycle. A local health snapshot and an external
heartbeat provide independent signals when the host stops responding.

## Deliberate limitations

The project does not promise complete coverage of LinkedIn. Public endpoints
and visible browser content have different coverage, and a browser capture
requires the LinkedIn feed to be open and loaded. The system also does not
attempt to bypass challenges or authenticated access controls.

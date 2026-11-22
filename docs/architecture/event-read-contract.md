# Event read contract (one timeline rule)

Status: 2026-10-17 candidate integration contract. Read-only layer; artifacts,
`source_id`, `event_id`, human corrections and decision branches are never
rewritten by readers.

## 1. Canonical source

`life_logger.event_view.build_event_view(...)` is the only canonical event
read layer. The HTTP product routes must consume it rather than independently
re-reading `daily_summary.json` or `watch-sessions/*/*/events.json`.

Canonical response schema: `event-view-v1`.

Required metadata:
- `timezone: "Asia/Shanghai"`
- `scope`: `settled` or `provisional`
- `view`: `display` or `all`
- `counts.settled` / `counts.provisional` / `counts.raw`
- `selected_counts` for the returned event list
- `dedup` counts and candidate aliases
- `model_branches.root_only = true`
- `scope_semantics`

Product HTTP routes expose `read_contract` with the same `scope`/`view`,
`timezone`, date basis and `canonical_event_view` flag.

## 2. Time rules

All day boundaries, clocks and “today” calculations use **Asia/Shanghai**.

Event time fields have different meanings:
- `occurred_at`: occurrence time, when evidence establishes it.
- `reported_at`: when the user spoke/reported it. It is not occurrence time.
- `effective_date` / `date_basis`: the date bucket used by the read view.
  `date_basis` is one of `occurrence`, `reported_local` or
  `artifact_directory`.
- `time.absolute` from legacy artifacts may be occurrence evidence only when
  the artifact semantics support it; do not silently turn transcript/audio
  start time into occurrence time.

Date selection is based on `effective_date`, never on a summary directory
alone. A valid occurrence timestamp is not replaced by
`recording_start + relative_seconds`. If occurrence is unknown, a reported
local date may organise the event and is labeled `reported_local`; the
occurrence date stays unknown. Artifact directory date is a last-resort
fallback labeled `artifact_directory`.

Unknown date/time stays unknown. It must not be assigned to “today” silently.

## 3. Scope and view

`scope`:
- `settled`: formal pipeline artifacts only. **Settled is not human-confirmed
  truth.** It means the formal pipeline produced the record.
- `provisional`: formal branch plus live Watch events. Exact cross-source
  duplicates are represented as one canonical event with aliases/provenance;
  original ids remain intact.

`view`:
- `display`: use `display_timeline` when present. An empty `display_timeline`
  is authoritative and is not replaced by the raw timeline.
- `all`: use the raw `timeline`.

Raw counts, display counts, provisional counts and external-agent confidence
subsets are different quantities and must be labeled separately. Do not force
them equal.

## 4. Decision branches

Jev and Clef results are isolated variants:
- Variant artifacts are never merged into one ledger/event list.
- `event_view` excludes `decision-variants/` and `comparison-history/`.
- Readers must not sum Jev + Clef totals.
- A root index with no selected variant is “no variant selected”, not zero
  events.

## 5. Canonical identity and provenance

Each canonical event carries:
- `canonical_event_id` (original id preserved)
- `event_key` (canonical business/entity key when derivable)
- `pipeline_event_key` (source-specific key)
- `aliases` for deduplicated cross-source records
- `provenance` and `review_provenance`
- `conflicts` when branches disagree

Dedup is a read view, not a destructive history edit. Financial fields are not
back-filled from the live branch when the formal branch left them empty; live
values remain in aliases/conflicts for review.

## 6. Financial facts and currency

Product accounting uses the shared expense policy ledger built from the same
canonical event list as `event_count` for the same `scope`/`view`/dictation
filter.

- `total_amount` and `currency="CNY"` are the legacy **CNY-only** aliases.
- `totals_by_currency` / `category_totals_by_currency` are required for real
  multi-currency reads.
- `payment_transactions` (or `kind in {consumption, transfer}`) are payments.
- `transactions` in the current ledger is the payment list; withdrawals and
  refunds are exposed through `non_payment_transactions` /
  `all_transactions` and their own currency totals. Consumers must never sum
  raw `all_transactions` as “expense”.
- Missing/unknown amounts remain `pending_transactions`; they are never closed
  at zero or invented.
- `settled`/`provisional` is pipeline source, not human-confirmed financial
  truth.

## 7. HTTP consumer contract

Canonical product routes:
- `GET /api/stats?from=YYYY-MM-DD&to=YYYY-MM-DD&scope=&view=`
- `GET /api/daily?date=YYYY-MM-DD&scope=&view=`
- `GET /api/daily-brief?date=YYYY-MM-DD&scope=&view=`
- `GET /api/events/search?q=&type=all|event|utterance&date=&from=&to=&category=&amount_min=&amount_max=&currency=&import_source=&scope=&view=`

Defaults: `scope=provisional`, `view=display`.

For the same `scope`/`view` and the same dashboard dictation filter:
- `/api/stats.event_count` equals `/api/daily.events.length`.
- `/api/stats.expense_total_cny` equals
  `/api/daily.expense_ledger.total_amount`.
- Stats and daily both apply `DashboardDictationFilter`; the filter is a read
  view only and never edits artifacts.

`/api/daily-brief` is the external agent projection:
- schema `agent-brief-v1`
- confidence-gated at `AGENT_MIN_CONFIDENCE` (0.90)
- `event_count` is the high-confidence external count, not the canonical
  count
- `event_count_basis` and `confidence_filter` must label the source count,
  included count and filtered count
- it must not expose raw audio paths, full transcripts, evidence ids or
  internal artifact paths.

Empty days are valid reads: return an empty payload with the requested
`scope`/`view` and empty metadata. Invalid `scope`/`view` is `400`, never
silently coerced.

### Canonical event search

`GET /api/events/search` is the normal product search endpoint:
- event results come from `build_event_view` with the same `scope`/`view`
  defaults and the same dashboard dictation filter as stats/daily;
- event dates use canonical `effective_date`/`date_basis`, not summary folder
  names;
- live/formal duplicates appear once with `aliases`/`provenance`;
- financial fields come from the shared expense policy: `expense_amount`,
  `expense_currency`, `expense_amount_cny` (CNY only), `expense_kind`,
  `financial_status`;
- amount filters only pass `payment_transactions` with a known amount and a
  matching currency. If no `currency` is supplied, amount filters default to
  CNY, so `7 USD` never satisfies a CNY 7 filter; unknown/pending amounts do
  not become zero;
- category/amount/currency filters select the event branch even when
  `type=all`;
- `type=utterance` returns self-utterance/transcript evidence only, labeled
  `date_basis="speech_time"`, `source_kind="transcript"`,
  `canonical_event=false`. It never returns raw event ids as event results;
- each event result keeps safe link fields (`job_id`, `artifact_path` when the
  artifact is under that job) plus `source_job_id`, `source_job_ids`,
  `source_artifact`, `provenance` and `aliases`;
- top-level fields: `result_count`, `returned_count`, `truncated`,
  `event_count`, `utterance_count`, `scope`, `view`, `timezone`,
  `read_contract`.

`type=all` may combine canonical events + explicit speech-time utterances.
Their counts are separate and are not “all confirmed facts”.

`GET /api/search` remains the legacy/diagnostic raw search and is not part of
normal product event accounting.

## 8. Pending review counters

`GET /api/pending-sources` is a human review queue and is not filtered by the
dashboard dictation filter.

Use open counters for badges:
- `totals.open_actionable`
- `totals.open_extraction_gap`
- `totals.open_batch_failure`
- `totals.scoped_open_actionable`
- `totals.scoped_open_extraction_gap`

`totals.needs_review` is historical and includes resolved items. Do not use it
for an open badge.

## 9. Diagnostic/raw routes

The following are diagnostic and explicitly not canonical accounting:
- `GET /api/search` — legacy diagnostic search only; normal product event
  search is `/api/events/search`. Its date basis is the artifact timestamp
  prefix, not canonical occurrence reconciliation.
- `GET /api/watch-live-events` — raw live-session inspection; reported/session
  time is not promoted to occurrence time.
- `GET /api/dashboard/daily` and `/api/timeline-scenes` — dashboard-owned
  read paths; the dashboard worker owns migration to the shared event view and
  audio coverage minutes.

These routes return `read_contract` with `canonical_event_view=false` and a
notice. Product/consumer accounting must use `/api/stats`, `/api/daily` or
`/api/daily-brief`. `/api/events/search` is the normal event-search path;
`/api/search` is kept for raw diagnostic evidence.

Other artifact readers found in the audit are not day-ledger authorities:
- `JobStore.read_job_events` / job artifact endpoints read one job’s files for
  inspection and editing;
- `passport_sync.py` reads daily summaries for device sync;
- `memory_rag.py` scans Watch events for retrieval context;
- `daily_dashboard.py` is presentation/scene material and is owned by the
  dashboard worker.

They must not be used to answer “the day’s total event count / money” or to
reclassify transcript time as occurrence time without going through the
canonical view.

## 10. Frontend/UI consumer rules

- Display `scope`, `view`, `date_basis` and `timezone` when they affect the
  answer.
- Never label `settled` as “human confirmed”.
- Never add Jev and Clef variants together.
- Never display unknown nutrition/amount as zero; display unknown/pending and
  coverage.
- When showing external agent data, label it as confidence-filtered.
- Normal event search uses `/api/events/search`; raw transcript/diagnostic
  search uses `/api/search` and must be labeled separately.
- Treat diagnostic search/live-session payloads as diagnostic, not as the
  day ledger.

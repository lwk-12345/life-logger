# Journal review / day-boundary / action-candidate rules

This note defines the user-visible behavior introduced for the DAILY MEMORY fixes.

## Review badge

The dashboard badge and the review-center cards use the same pending-review queue.

A review candidate is counted only when it still needs user truth. Duplicate processing jobs
are collapsed by review kind + recording + segment. If any duplicate copy has a completed
decision, the candidate is not counted again.

The main badge also follows the review viewer's **default visible queue**. `background` / low-priority
fragments do not inflate the badge for normal unknown-review pages because those rows are collapsed
by default. Sample-harvest remains counted because that review page intentionally
shows its candidate pool by default. Expanding background rows is an explicit drill-down, not a
change to the main pending count.

## Day boundary

**Product definition:** a Life Logger “day” is the **Asia/Shanghai local calendar day that owns the evidence**. Ownership is determined by the event/recording start time; crossing midnight does not move the item to the next day. When opening the diary without an explicit valid evidence day, select the most recent local day that actually contains recording or event evidence.

The diary uses the **Asia/Shanghai local midnight** boundary.

1. Event ownership uses the event start timestamp.
2. If start is missing, the event end timestamp is used.
3. If both are missing, the artifact directory date is the fallback.
4. An event that starts before 00:00 and ends after 00:00 belongs to the start day and is
   marked as crossing into the next day.
5. A job creation/update date is never evidence for a diary day. A job created after midnight may
   still contain a recording owned by the previous local day.
6. The artifact directory date is allowed only as a fallback when an actual event, review segment,
   self-transcript segment, or source recording exists but lacks a usable timestamp. An empty
   summary/artifact directory must not create a diary day by itself.
7. On first load, the diary defaults to the newest local day with recording/event evidence. The
   Today/Yesterday shortcuts may open a day with no evidence, but that view must stay empty rather
   than borrowing entries from another day.
8. When an artifact folder date disagrees with the source recording wall-clock date, untimed
   events/review rows inherit the source recording day. This prevents a job created after midnight
   from leaking its previous-evening recording into the new calendar day.
9. Recorder filenames with an explicit wall-clock timestamp are interpreted in the job timezone.
   If neither a user-provided start nor a filename wall-clock exists, new recordings keep absolute
   time unknown. Container metadata and filesystem mtime are not promoted into a recording start.
   Legacy journal rendering may still rebuild old event wall-clock time from
   `filename start + relative_seconds` without rerunning ASR.

Artifact folders remain provenance containers; the Web daily timeline/aggregation is regrouped
by this event-local date so a cross-midnight recording does not force every event onto its
recording-start day.

## NEXT becomes action candidates

The homepage lane is an **action candidate** view, not a second task database.

Only actionable todo events are eligible. Pending candidates older than 7 days are hidden from
the homepage; explicitly in-progress tasks remain visible. Completed and dismissed items do not
appear.

“从首页移除” persists the local task state as `dismissed`. It is not a hard delete: the underlying
LifeEvent/evidence remains in the diary. This is intentionally compatible with
`docs/openclaw-todo-unify.md`: once the OpenClaw bridge is connected, `dismissed` maps to the
canonical task status rather than creating a separate Life Logger todo store.

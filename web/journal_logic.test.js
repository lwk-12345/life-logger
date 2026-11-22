'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');

const {
  actionCandidateVisible,
  collectSyncSummary,
  collapseReviewRows,
  eventCrossesMidnight,
  eventLocalDate,
  entryEvidenceDates,
  groupReviewTargets,
  initialJournalDate,
  jobFailedFileCount,
  jobFailureSuffix,
  reviewCandidateVisible,
  relativeAbsoluteTimestamp,
  sourceFileLocalDate,
  shanghaiDateFromTimestamp,
  statsProcessingLine,
  sourceCandidateTitle,
  sourceOutcomeForSegment,
  sourceOutcomeStatus,
  sourceOutcomeStatusCounts,
  sourceScopedItemsForSegment,
  relationReviewMap,
  relationReviewState,
  watchActivityAgeLabel,
  watchEmptyReceptionState,
  sortWatchReceptionSessions,
  summarizeSyncResults,
  syncToastMessage,
  watchSessionDisplayState,
  watchSessionNeedsAttention,
  visibleWatchReceptionSessions,
} = require('./journal_logic.js');

test('transcript source outcomes preserve semantic status and keep missing distinct', () => {
  assert.equal(sourceOutcomeStatus({semantic_status: 'processed'}), 'processed');
  assert.equal(sourceOutcomeStatus({semantic_status: 'no_event'}), 'no_event');
  assert.equal(sourceOutcomeStatus({semantic_status: 'pending'}), 'pending');
  assert.equal(sourceOutcomeStatus({semantic_status: 'failed'}), 'failed');
  assert.equal(sourceOutcomeStatus({}), 'unknown');
  assert.deepEqual(sourceOutcomeStatusCounts([
    {semantic_status: 'processed'}, {semantic_status: 'no_event'},
    {semantic_status: 'pending'}, {semantic_status: 'failed'}, {},
  ]), {processed: 1, no_event: 1, pending: 1, failed: 1, unknown: 1});
});

test('transcript outcome and pending candidates attach by source and segment evidence', () => {
  const segment = {segment_id: 'seg-1'};
  const outcomes = [
    {source_id: 'source-a', segment_id: 'seg-1', semantic_status: 'failed'},
    {source_id: 'source-b', segment_id: 'seg-1', semantic_status: 'processed'},
  ];
  assert.equal(sourceOutcomeForSegment(outcomes, segment, 'source-a'), outcomes[0]);
  assert.equal(sourceOutcomeForSegment(outcomes, segment, 'source-c'), null);
  const candidates = [
    {topic: '健身后的身体不适', evidence: {source_id: 'source-a', segment_id: 'seg-1'}},
    {title: '其他录音候选', evidence: {source_id: 'source-b', segment_id: 'seg-1'}},
  ];
  assert.deepEqual(sourceScopedItemsForSegment(candidates, segment, 'source-a'), [candidates[0]]);
  assert.equal(sourceCandidateTitle(candidates[0]), '健身后的身体不适');
  assert.equal(sourceCandidateTitle({}), '待复核主题候选');
});

test('relation review overlay decisions override candidate pending status', () => {
  const candidates = [
    {candidate_id: 'rel-1', review_status: 'pending'},
    {candidate_id: 'rel-2', review_status: 'pending'},
    {candidate_id: 'rel-3', review_status: 'pending'},
  ];
  const reviews = relationReviewMap([
    {candidate_id: 'rel-1', decision: 'accepted'},
    {candidate_id: 'rel-2', decision: 'rejected'},
  ]);
  assert.equal(relationReviewState(candidates[0], reviews.get('rel-1')), 'accepted');
  assert.equal(relationReviewState(candidates[1], reviews.get('rel-2')), 'rejected');
  assert.equal(relationReviewState(candidates[2], reviews.get('rel-3')), 'pending');
  assert.equal(relationReviewState({review_status: 'accepted'}), 'accepted');
});

test('Asia/Shanghai day ownership uses event start and understands offsets', () => {
  assert.equal(shanghaiDateFromTimestamp('2026-09-21T23:59:00+08:00'), '2026-09-21');
  assert.equal(shanghaiDateFromTimestamp('2026-09-21T16:05:00Z'), '2026-09-22');
  assert.equal(shanghaiDateFromTimestamp('2026-09-22T00:05:00'), '2026-09-22');

  const event = {
    time: {absolute: '2026-09-21T23:59:30+08:00'},
    evidence: {absolute_ended_at: '2026-09-22T00:02:00+08:00'},
  };
  assert.equal(eventLocalDate(event, '2026-09-20'), '2026-09-21');
  assert.equal(eventCrossesMidnight(event), true);
});

test('day ownership falls back to end then artifact date', () => {
  assert.equal(
    eventLocalDate(
      {time: {absolute: null}, evidence: {absolute_ended_at: '2026-09-22T00:02:00+08:00'}},
      '2026-09-20',
    ),
    '2026-09-22',
  );
  assert.equal(eventLocalDate({time: {}, evidence: {}}, '2026-09-20'), '2026-09-20');
});


test('job created after midnight does not create a diary day when recording belongs to previous day', () => {
  const entry = {
    artifactDate: '2026-09-22',
    jobCreatedAt: '2026-09-22T00:12:31+08:00',
    sourceFile: {
      original_name: '2026年9月21日 13_52_46.m4a',
      effective_start_at: '2026-09-21T15:22:03+00:00',
    },
    events: [{
      time: {absolute: '2026-09-21T23:59:30+08:00'},
      evidence: {absolute_ended_at: '2026-09-22T00:02:00+08:00'},
    }],
  };

  assert.deepEqual(entryEvidenceDates(entry), ['2026-09-21']);
});


test('artifact directory alone is not diary evidence', () => {
  assert.deepEqual(entryEvidenceDates({artifactDate: '2026-09-22'}), []);
  assert.deepEqual(
    entryEvidenceDates({
      artifactDate: '2026-09-22',
      selfTranscript: [{absolute_started_at: '2026-09-21T20:03:00+08:00'}],
    }),
    ['2026-09-21'],
  );
});


test('initial diary day ignores a stale no-evidence persisted day', () => {
  assert.equal(initialJournalDate(['2026-09-21'], '2026-09-22'), '2026-09-21');
  assert.equal(
    initialJournalDate(['2026-09-20', '2026-09-21'], '2026-09-20'),
    '2026-09-20',
  );
  assert.equal(initialJournalDate([], '2026-09-22'), null);
});


test('product day definition keeps cross-midnight evidence on start day and defaults to newest evidence day', () => {
  const event = {
    time: {absolute: '2026-09-21T23:59:30+08:00'},
    evidence: {absolute_ended_at: '2026-09-22T00:02:00+08:00'},
  };

  assert.equal(eventLocalDate(event), '2026-09-21');
  assert.equal(eventCrossesMidnight(event), true);
  assert.equal(initialJournalDate(['2026-09-20', '2026-09-21'], null), '2026-09-21');
});

test('source recording day is the fallback for untimed evidence, not artifact folder day', () => {
  const entry = {
    artifactDate: '2026-09-22',
    sourceFile: {effective_start_at: '2026-09-21T15:22:03+00:00'},
    events: [
      {time: {}, evidence: {}},
      {
        time: {absolute: '2026-09-21T23:59:30+08:00'},
        evidence: {absolute_ended_at: '2026-09-22T00:02:00+08:00'},
      },
    ],
  };

  assert.deepEqual(entryEvidenceDates(entry), ['2026-09-21']);
});


test('legacy recorder filename plus relative offset repairs false next-day timestamps', () => {
  const sourceFile = {
    original_name: '2026年9月21日 13_52_46.m4a',
    effective_start_at: '2026-09-21T15:22:03+00:00',
  };
  const repaired = relativeAbsoluteTimestamp(sourceFile, 32995.3);

  assert.equal(repaired, '2026-09-21T15:02:41.300Z');
  assert.equal(sourceFileLocalDate(sourceFile), '2026-09-21');
  assert.equal(
    eventLocalDate(
      {
        time: {
          absolute: '2026-09-22T00:31:58.300000+00:00',
          relative_seconds: 32995.3,
        },
        evidence: {start_offset_seconds: 32995.3},
      },
      '2026-09-22',
      sourceFile,
    ),
    '2026-09-21',
  );
});

test('main review queue follows viewer default background visibility', () => {
  assert.equal(reviewCandidateVisible({review_priority: 'high'}), true);
  assert.equal(reviewCandidateVisible({review_priority: 'medium'}), true);
  assert.equal(reviewCandidateVisible({review_priority: 'low'}), false);
  assert.equal(reviewCandidateVisible({review_layer: 'background'}), false);
  assert.equal(reviewCandidateVisible({review_layer: 'background'}, true), true);
});

test('review queue deduplicates duplicate jobs and completed truth wins', () => {
  const rows = [
    {key: 'unknown:rec:seg-1', kind: 'unknown', jobId: 'job-a', path: 'a.json', completed: false, partial: false},
    {key: 'unknown:rec:seg-1', kind: 'unknown', jobId: 'job-b', path: 'b.json', completed: true, partial: false},
    {key: 'unknown:rec:seg-2', kind: 'unknown', jobId: 'job-a', path: 'a.json', completed: false, partial: true},
    {key: 'harvest:rec:seg-3', kind: 'harvest', jobId: 'job-a', path: 'h.json', completed: false, partial: false},
  ];

  const collapsed = collapseReviewRows(rows);
  assert.deepEqual(collapsed.pending.map((row) => row.key).sort(), [
    'harvest:rec:seg-3',
    'unknown:rec:seg-2',
  ]);
  assert.deepEqual(collapsed.completed.map((row) => row.key), ['unknown:rec:seg-1']);

  const targets = groupReviewTargets(collapsed.pending);
  assert.equal(targets.reduce((sum, target) => sum + target.count, 0), collapsed.pending.length);
});

test('action candidates hide stale pending and dismissed but retain in-progress', () => {
  const selectedStart = Date.parse('2026-09-22T00:00:00+08:00');
  const sixDaysAgo = Date.parse('2026-09-16T12:00:00+08:00');
  const sevenDaysAgo = Date.parse('2026-09-15T23:59:00+08:00');

  assert.equal(actionCandidateVisible('pending', sixDaysAgo, selectedStart), true);
  assert.equal(actionCandidateVisible('pending', sevenDaysAgo, selectedStart), false);
  assert.equal(actionCandidateVisible('in_progress', sevenDaysAgo, selectedStart), true);
  assert.equal(actionCandidateVisible('completed', sixDaysAgo, selectedStart), false);
  assert.equal(actionCandidateVisible('dismissed', sixDaysAgo, selectedStart), false);
});

test('watch session lifecycle trusts superseded over any recency signal', () => {
  // 同一设备已经开出更晚的会话就是硬判据：设备同一时刻只有一个会话。
  assert.equal(watchSessionDisplayState({status: 'superseded', superseded: true, activity_age_seconds: 47}), 'superseded');
  assert.equal(watchSessionDisplayState({status: 'receiving', superseded: true}), 'superseded');
  assert.equal(watchSessionDisplayState({status: 'stalled', stalled: true}), 'stalled');
  assert.equal(watchSessionDisplayState({status: 'receiving', stalled: true}), 'stalled');
  assert.equal(watchSessionDisplayState({status: 'receiving', superseded: false, stalled: false}), 'receiving');
  assert.equal(watchSessionDisplayState({status: 'complete'}), 'complete');
});

test('watch session lifecycle falls back to the legacy state field', () => {
  assert.equal(watchSessionDisplayState({state: 'receiving'}), 'receiving');
  assert.equal(watchSessionDisplayState({state: 'complete'}), 'complete');
  assert.equal(watchSessionDisplayState({}), 'unknown');
  assert.equal(watchSessionDisplayState(null), 'unknown');
});

test('unfinished watch sessions and degraded extraction results need attention', () => {
  assert.equal(watchSessionNeedsAttention({status: 'superseded'}), true);
  assert.equal(watchSessionNeedsAttention({status: 'stalled'}), true);
  assert.equal(watchSessionNeedsAttention({state: 'incomplete'}), true);
  assert.equal(watchSessionNeedsAttention({status: 'complete', formal_job: {context_extraction: {status: 'degraded'}}}), true);
  assert.equal(watchSessionNeedsAttention({status: 'complete', formal_job: {context_extraction: {status: 'failed'}}}), true);
  assert.equal(watchSessionNeedsAttention({status: 'complete', formal_job: {context_extraction: {status: 'completed'}}}), false);
  assert.equal(watchSessionNeedsAttention({status: 'receiving'}), false);
  assert.equal(watchSessionNeedsAttention({status: 'complete'}), false);
});

test('Watch reception list keeps newest sessions visible before older attention items', () => {
  const now = Date.parse('2026-10-06T10:00:00+08:00');
  const rows = [
    {session_id: 'old-attention', started_at: '2026-10-02T08:00:00+08:00', status: 'superseded'},
    {session_id: 'newest', started_at: '2026-10-06T09:50:00+08:00', status: 'receiving'},
    {session_id: 'middle', started_at: '2026-10-05T17:00:00+08:00', status: 'receiving'},
    {session_id: 'bad-clock', started_at: '2026-10-07T10:00:00+08:00', status: 'receiving'},
    {session_id: 'unknown-time', status: 'stalled'},
  ];

  const ordered = sortWatchReceptionSessions(rows, now);
  assert.deepEqual(ordered.map((row) => row.session_id), [
    'newest', 'middle', 'old-attention', 'bad-clock', 'unknown-time',
  ]);
  assert.deepEqual(
    visibleWatchReceptionSessions(ordered, false, 3).map((row) => row.session_id),
    ['newest', 'middle', 'old-attention'],
  );
  assert.deepEqual(
    visibleWatchReceptionSessions(ordered, true).map((row) => row.session_id),
    ordered.map((row) => row.session_id),
  );
});

test('zero-segment Watch sessions say no audio reached the Mac', () => {
  assert.equal(watchEmptyReceptionState({received_segments: 0, status: 'receiving'}), '等待首个音频分段');
  assert.equal(watchEmptyReceptionState({received_segments: 0, status: 'superseded'}), '旧会话 · Mac未收到音频分段');
  assert.equal(watchEmptyReceptionState({received_segments: 0, completion_pending: true}), '手表已报告结束 · Mac未收到音频分段');
  assert.equal(watchEmptyReceptionState({received_segments: 1, status: 'superseded'}), null);
});

test('activity age never claims 刚刚 for a session that is hours or days old', () => {
  assert.equal(watchActivityAgeLabel(47), '刚刚');
  assert.equal(watchActivityAgeLabel(2820), '47 分钟前');
  assert.equal(watchActivityAgeLabel(21600), '6 小时前');
  assert.equal(watchActivityAgeLabel(Math.round(2.7 * 86400)), '2.7 天前');
  assert.equal(watchActivityAgeLabel(15 * 86400), '15 天前');
  // 缺字段不能退化成 0 秒（那会把停滞说成"刚刚"）。
  assert.equal(watchActivityAgeLabel(null), '');
  assert.equal(watchActivityAgeLabel(undefined), '');
  assert.equal(watchActivityAgeLabel(''), '');
  assert.equal(watchActivityAgeLabel(-1), '');
});

test('job rows report partial failures instead of a bare 已完成', () => {
  const partial = {status: 'partial', failed_file_count: 1, files: new Array(12).fill({})};
  assert.equal(jobFailureSuffix(partial), ' · 失败 1/12 个文件');
  assert.equal(jobFailureSuffix({status: 'partial', failed_files: [{}, {}], files: [{}, {}, {}]}), ' · 失败 2/3 个文件');
  assert.equal(jobFailureSuffix({status: 'partial', failed_file_count: 2}), ' · 失败 2 个文件');
  // 任务级 status 已经在说失败，就不再重复计数。
  assert.equal(jobFailureSuffix({status: 'failed', failed_file_count: 3, files: [{}, {}, {}]}), '');
  assert.equal(jobFailureSuffix({status: 'completed', failed_file_count: 0, files: [{}]}), '');
  assert.equal(jobFailureSuffix({status: 'completed', files: [{}]}), '');
  assert.equal(jobFailureSuffix(null), '');
});

test('jobFailedFileCount prefers the server count and falls back to the array', () => {
  assert.equal(jobFailedFileCount({failed_file_count: 4, failed_files: [{}]}), 4);
  assert.equal(jobFailedFileCount({failed_files: [{}, {}]}), 2);
  assert.equal(jobFailedFileCount({}), 0);
});

test('stats processing line surfaces the files that event_count hides', () => {
  const line = statsProcessingLine({
    processing: {
      processed_file_count: 99,
      failed_file_count: 25,
      unprocessed_job_count: 24,
      job_status_counts: {completed: 24, partial: 9, failed: 7, draft: 4, interrupted: 4},
    },
  });
  assert.equal(line, '已处理 99 个文件 · 失败 25 个 · 未处理任务 24 个 · 部分完成任务 9 个');
  assert.equal(
    statsProcessingLine({processing: {processed_file_count: 3, failed_file_count: 0, unprocessed_job_count: 0, job_status_counts: {}}}),
    '已处理 3 个文件 · 失败 0 个 · 未处理任务 0 个',
  );
  assert.equal(statsProcessingLine({}), '');
  assert.equal(statsProcessingLine({processing: {}}), '');
});

test('sync summary treats optional Passport as skipped without blocking core success', () => {
  assert.deepEqual(
    summarizeSyncResults([
      {label: 'Apple Watch录音', status: 'success'},
      {label: '任务与日记', status: 'success'},
      {label: 'AI Passport', status: 'skipped', optional: true},
    ]),
    {status: 'success', requiredFailures: [], optionalFailures: [], skipped: ['AI Passport']},
  );
});

test('sync summary reports required failures even when other requests succeed', () => {
  assert.deepEqual(
    summarizeSyncResults([
      {label: 'Apple Watch录音', status: 'failed'},
      {label: '任务与日记', status: 'success'},
      {label: 'AI Passport', status: 'success', optional: true},
    ]),
    {status: 'failed', requiredFailures: ['Apple Watch录音'], optionalFailures: [], skipped: []},
  );
});

test('sync summary reports optional failures as a partial update', () => {
  assert.deepEqual(
    summarizeSyncResults([
      {label: 'Apple Watch录音', status: 'success'},
      {label: '任务与日记', status: 'success'},
      {label: 'AI Passport', status: 'failed', optional: true},
    ]),
    {status: 'partial', requiredFailures: [], optionalFailures: ['AI Passport'], skipped: []},
  );
});

test('a rejected core refresh cannot be summarized as sync success', async () => {
  const summary = await collectSyncSummary([
    {label: 'Apple Watch录音', promise: Promise.reject(new Error('offline'))},
    {label: '任务与日记', promise: Promise.resolve({status: 'success'})},
    {label: 'AI Passport', optional: true, promise: Promise.resolve({status: 'skipped'})},
  ]);
  assert.deepEqual(summary, {
    status: 'failed',
    requiredFailures: ['Apple Watch录音'],
    optionalFailures: [],
    skipped: ['AI Passport'],
  });
});

test('a rejected optional refresh is reported as partial after core success', async () => {
  const summary = await collectSyncSummary([
    {label: 'Apple Watch录音', promise: Promise.resolve({status: 'success'})},
    {label: '任务与日记', promise: Promise.resolve({status: 'success'})},
    {label: 'AI Passport', optional: true, promise: Promise.reject(new Error('unavailable'))},
  ]);
  assert.deepEqual(summary, {
    status: 'partial',
    requiredFailures: [],
    optionalFailures: ['AI Passport'],
    skipped: [],
  });
});

test('sync summary fails closed when a refresh has no result status', async () => {
  const summary = await collectSyncSummary([
    {label: 'Apple Watch录音', promise: Promise.resolve(undefined)},
  ]);
  assert.deepEqual(summary, {
    status: 'failed',
    requiredFailures: ['Apple Watch录音'],
    optionalFailures: [],
    skipped: [],
  });
  assert.deepEqual(await collectSyncSummary([]), {
    status: 'failed',
    requiredFailures: ['同步请求'],
    optionalFailures: [],
    skipped: [],
  });
});

test('sync notifications distinguish success, partial failure, and core failure', () => {
  assert.deepEqual(syncToastMessage({
    status: 'success',
    requiredFailures: [],
    optionalFailures: [],
    skipped: ['AI Passport'],
  }), {
    message: '同步完成：录音、任务与日记已更新（AI Passport未启用）。',
    isError: false,
  });
  assert.deepEqual(syncToastMessage({
    status: 'partial',
    requiredFailures: [],
    optionalFailures: ['AI Passport'],
    skipped: [],
  }), {
    message: '录音、任务与日记已更新；AI Passport状态读取失败。',
    isError: true,
  });
  assert.deepEqual(syncToastMessage({
    status: 'failed',
    requiredFailures: ['Apple Watch录音'],
    optionalFailures: [],
    skipped: [],
  }), {
    message: '同步未完成：Apple Watch录音读取失败，请稍后重试。',
    isError: true,
  });
});

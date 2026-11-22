(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.LifeLoggerJournalLogic = api;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  // Reuse locale/time-zone formatters across every segment in the recording index.
  const shanghaiDayFormatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  });
  const shanghaiClockFormatter = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });

  function shanghaiDateFromTimestamp(value, fallback = null) {
    const text = String(value || "").trim();
    if (!text) return fallback;
    if (/^\d{4}-\d{2}-\d{2}T/.test(text) && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(text)) {
      return text.slice(0, 10);
    }
    const millis = Date.parse(text);
    if (!Number.isFinite(millis)) return fallback;
    const parts = shanghaiDayFormatter.formatToParts(new Date(millis));
    const pick = (type) => parts.find((part) => part.type === type)?.value || "";
    return `${pick("year")}-${pick("month")}-${pick("day")}`;
  }

  function sourceFilenameStartMillis(sourceFile) {
    const name = String(sourceFile?.original_name || sourceFile?.relative_path || "");
    let match = name.match(/(\d{4})年(\d{1,2})月(\d{1,2})日[ _-]+(\d{1,2})[_:-](\d{2})[_:-](\d{2})/u);
    if (!match) {
      match = name.match(/(\d{4})-(\d{2})-(\d{2})[_ -]+(\d{2})[-_:](\d{2})(?:[-_:](\d{2}))?/u);
    }
    if (!match) return null;
    const [, year, month, day, hour, minute, second = "00"] = match;
    const stamp = Date.parse(`${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}T${String(hour).padStart(2, "0")}:${minute}:${second}+08:00`);
    return Number.isFinite(stamp) ? stamp : null;
  }

  function relativeAbsoluteTimestamp(sourceFile, relativeSeconds) {
    const relative = Number(relativeSeconds);
    if (!Number.isFinite(relative)) return null;
    const filenameStart = sourceFilenameStartMillis(sourceFile);
    if (Number.isFinite(filenameStart)) return new Date(filenameStart + relative * 1000).toISOString();
    const fallbackStart = sourceFile?.effective_start_at
      || sourceFile?.inferred_start_at
      || sourceFile?.recorded_at
      || null;
    const startMillis = Date.parse(String(fallbackStart || ""));
    return Number.isFinite(startMillis) ? new Date(startMillis + relative * 1000).toISOString() : null;
  }

  function eventLocalDate(event, fallback = null, sourceFile = null) {
    const relativeStart = event?.time?.relative_seconds ?? event?.evidence?.start_offset_seconds;
    const repairedStart = sourceFile ? relativeAbsoluteTimestamp(sourceFile, relativeStart) : null;
    const start = event?._journalAbsolute
      || repairedStart
      || event?.time?.absolute
      || event?.evidence?.reported_at
      || event?.evidence?.occurrence_time_evidence
      || event?.evidence?.absolute_started_at;
    const relativeEnd = event?.evidence?.end_offset_seconds;
    const repairedEnd = sourceFile ? relativeAbsoluteTimestamp(sourceFile, relativeEnd) : null;
    const end = event?._journalEndAbsolute
      || repairedEnd
      || event?.evidence?.absolute_ended_at;
    return shanghaiDateFromTimestamp(start, null)
      || shanghaiDateFromTimestamp(end, null)
      || fallback;
  }

  function eventCrossesMidnight(event, sourceFile = null) {
    // For a reported past event, the evidence offsets describe when it was
    // spoken. They do not define the duration of the event being recalled.
    if (event?.evidence?.temporal_relation === "past") return false;
    const relativeStart = event?.time?.relative_seconds ?? event?.evidence?.start_offset_seconds;
    const relativeEnd = event?.evidence?.end_offset_seconds;
    const start = event?._journalAbsolute
      || relativeAbsoluteTimestamp(sourceFile, relativeStart)
      || event?.time?.absolute
      || event?.evidence?.absolute_started_at;
    const end = event?._journalEndAbsolute
      || relativeAbsoluteTimestamp(sourceFile, relativeEnd)
      || event?.evidence?.absolute_ended_at;
    const startDay = shanghaiDateFromTimestamp(start, null);
    const endDay = shanghaiDateFromTimestamp(end, null);
    return Boolean(startDay && endDay && startDay !== endDay);
  }

  function sourceFileLocalDate(sourceFile, fallback = null) {
    if (!sourceFile || typeof sourceFile !== "object") return null;
    const filenameStart = sourceFilenameStartMillis(sourceFile);
    if (Number.isFinite(filenameStart)) {
      return shanghaiDateFromTimestamp(new Date(filenameStart).toISOString(), fallback);
    }
    const timestamp = sourceFile.effective_start_at
      || sourceFile.inferred_start_at
      || sourceFile.recorded_at
      || null;
    return shanghaiDateFromTimestamp(timestamp, null) || fallback;
  }

  function segmentLocalDate(segment, fallback = null, sourceFile = null) {
    const relativeStart = segment?.start_offset_seconds ?? segment?.time?.relative_seconds;
    const start = relativeAbsoluteTimestamp(sourceFile, relativeStart)
      || segment?.absolute_started_at
      || segment?.absolute_start_at
      || segment?.time?.absolute
      || null;
    const end = relativeAbsoluteTimestamp(sourceFile, segment?.end_offset_seconds)
      || segment?.absolute_ended_at
      || segment?.absolute_end_at
      || null;
    return shanghaiDateFromTimestamp(start, null)
      || shanghaiDateFromTimestamp(end, null)
      || fallback;
  }

  function recordingIntervalRange(interval, ranges = null) {
    if (interval && typeof interval === "object" && ranges) {
      if (ranges.has(interval)) return ranges.get(interval);
      const range = parseRecordingIntervalRange(interval);
      ranges.set(interval, range);
      return range;
    }
    return parseRecordingIntervalRange(interval);
  }

  function parseRecordingIntervalRange(interval) {
    if (!interval || typeof interval !== "object") return null;
    const start = Date.parse(String(
      interval.started_at || interval.start_at || interval.absolute_started_at || ""
    ));
    if (!Number.isFinite(start)) return null;

    let end = Date.parse(String(
      interval.ended_at || interval.end_at || interval.absolute_ended_at || ""
    ));
    if (!Number.isFinite(end)) {
      const durationMs = Number.isFinite(Number(interval.duration_ms))
        ? Number(interval.duration_ms)
        : Number(interval.duration_seconds) * 1000;
      if (!Number.isFinite(durationMs) || durationMs <= 0) return null;
      end = start + durationMs;
    }
    if (end <= start || end - start > 31 * 24 * 60 * 60 * 1000) return null;
    return {start, end};
  }

  function recordingIntervalDates(intervals, ranges = null) {
    const dates = new Set();
    for (const interval of intervals || []) {
      const range = recordingIntervalRange(interval, ranges);
      if (!range) continue;
      const firstDate = shanghaiDateFromTimestamp(new Date(range.start).toISOString(), null);
      const lastDate = shanghaiDateFromTimestamp(new Date(range.end - 1).toISOString(), null);
      if (!firstDate || !lastDate) continue;
      let dayStart = Date.parse(`${firstDate}T00:00:00+08:00`);
      const lastDayStart = Date.parse(`${lastDate}T00:00:00+08:00`);
      for (let dayCount = 0; dayStart <= lastDayStart && dayCount < 32; dayCount += 1) {
        dates.add(shanghaiDateFromTimestamp(new Date(dayStart).toISOString(), null));
        dayStart += 24 * 60 * 60 * 1000;
      }
    }
    return [...dates].filter(Boolean).sort();
  }

  function recordingCoverageForDate(intervals, date, ranges = null) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(date || ""))) return [];
    const dayStart = Date.parse(`${date}T00:00:00+08:00`);
    if (!Number.isFinite(dayStart)) return [];
    const dayEnd = dayStart + 24 * 60 * 60 * 1000;
    const timeLabel = (timestamp, atDayEnd = false) => {
      if (atDayEnd) return "24:00";
      return shanghaiClockFormatter.format(new Date(timestamp));
    };

    return (intervals || []).flatMap((interval, index) => {
      const range = recordingIntervalRange(interval, ranges);
      if (!range || range.end <= dayStart || range.start >= dayEnd) return [];
      const clippedStart = Math.max(range.start, dayStart);
      const clippedEnd = Math.min(range.end, dayEnd);
      const startMinute = (clippedStart - dayStart) / 60_000;
      const endMinute = (clippedEnd - dayStart) / 60_000;
      const item = {
        id: `${interval.session_id || interval.recording_id || "recording"}:${interval.sequence ?? index}:${clippedStart}`,
        startMinute,
        endMinute,
        startPercent: (startMinute / 1440) * 100,
        endPercent: (endMinute / 1440) * 100,
        started_at: new Date(clippedStart).toISOString(),
        ended_at: new Date(clippedEnd).toISOString(),
        startLabel: timeLabel(clippedStart),
        endLabel: timeLabel(clippedEnd, clippedEnd === dayEnd),
        session_id: interval.session_id || null,
        sequence: Number.isInteger(interval.sequence) ? interval.sequence : null,
      };
      if (interval.battery_snapshot && typeof interval.battery_snapshot === "object") {
        item.battery_snapshot = interval.battery_snapshot;
      }
      return [item];
    }).sort((left, right) => left.startMinute - right.startMinute);
  }

  function recordingCoverageSummary(intervals) {
    const valid = (intervals || []).filter((interval) => (
      Number.isFinite(interval?.startMinute)
      && Number.isFinite(interval?.endMinute)
      && interval.endMinute > interval.startMinute
    ));
    if (!valid.length) return null;
    const earliest = valid.reduce((current, interval) => (
      interval.startMinute < current.startMinute ? interval : current
    ));
    const latest = valid.reduce((current, interval) => (
      interval.endMinute > current.endMinute ? interval : current
    ));
    const batteryLevels = valid
      .filter((i) => typeof i?.battery_snapshot?.level_percent === "number")
      .map((i) => i.battery_snapshot.level_percent);
    const summary = {
      startLabel: earliest.startLabel,
      endLabel: latest.endLabel,
      intervalCount: valid.length,
    };
    if (batteryLevels.length) {
      summary.startBattery = batteryLevels[0];
      summary.endBattery = batteryLevels[batteryLevels.length - 1];
    }
    return summary;
  }

  function recordingCoverageStatus(watchCoverageAvailable, intervalCount) {
    if (!watchCoverageAvailable) return "录音覆盖数据暂不可用";
    return Number(intervalCount) > 0
      ? "已显示可验证录音区间"
      : "没有可验证的录音时间段";
  }

  function watchCoverageState(previousIntervals, fetchResult) {
    if (fetchResult?.available === true && Array.isArray(fetchResult.intervals)) {
      return {available: true, intervals: fetchResult.intervals};
    }
    return {
      available: false,
      intervals: Array.isArray(previousIntervals) ? previousIntervals : [],
    };
  }

  function entryEvidenceDates(entry) {
    const artifactDate = entry?.artifactDate || entry?.date || null;
    const sourceDate = sourceFileLocalDate(entry?.sourceFile, null);
    const fallbackDate = sourceDate || artifactDate;
    const dates = new Set();
    for (const event of entry?.events || []) {
      const date = eventLocalDate(event, fallbackDate, entry?.sourceFile);
      if (date) dates.add(date);
    }
    for (const row of entry?.reviewRows || []) {
      const date = row?.date || segmentLocalDate(row, fallbackDate, entry?.sourceFile);
      if (date) dates.add(date);
    }
    for (const segment of entry?.selfTranscript || []) {
      const date = segmentLocalDate(segment, fallbackDate, entry?.sourceFile);
      if (date) dates.add(date);
    }
    if (sourceDate) dates.add(sourceDate);
    return [...dates].sort();
  }

  function initialJournalDate(availableDates, persistedDate = null) {
    const dates = [...new Set(availableDates || [])].filter(Boolean).sort().reverse();
    if (persistedDate && dates.includes(persistedDate)) return persistedDate;
    return dates[0] || null;
  }

  function reviewLayer(item) {
    if (item?.review_layer) return item.review_layer;
    if (item?.review_priority === "high") return "focus";
    if (item?.review_priority === "medium") return "standard";
    if (item?.review_priority === "low") return "background";
    return "standard";
  }

  function reviewCandidateVisible(item, includeBackground = false) {
    return includeBackground || reviewLayer(item) !== "background";
  }

  function actionCandidateVisible(status, occurrenceMillis, selectedDayStartMillis, windowDays = 7) {
    const normalized = String(status || "pending");
    if (["completed", "dismissed"].includes(normalized)) return false;
    if (normalized === "in_progress") return true;
    if (!Number.isFinite(occurrenceMillis) || !Number.isFinite(selectedDayStartMillis)) return false;
    const cutoff = selectedDayStartMillis - ((Math.max(1, windowDays) - 1) * 24 * 60 * 60 * 1000);
    return occurrenceMillis >= cutoff;
  }

  function reviewCandidateKey(kind, item) {
    const recording = String(item?.recording_id || item?.source_file || "");
    const segment = String(item?.segment_id || item?.event_id || "");
    if (recording || segment) return `${kind}:${recording}:${segment}`;
    return `${kind}:${JSON.stringify(item || {})}`;
  }

  function collapseReviewRows(rows) {
    const byKey = new Map();
    for (const row of rows || []) {
      if (!row?.key) continue;
      const current = byKey.get(row.key);
      if (!current) {
        byKey.set(row.key, {...row});
        continue;
      }
      if (row.completed) current.completed = true;
      current.partial = Boolean(current.partial || row.partial);
    }
    const all = [...byKey.values()];
    return {
      pending: all.filter((row) => !row.completed),
      completed: all.filter((row) => row.completed),
    };
  }

  function groupReviewTargets(rows) {
    const groups = new Map();
    for (const row of rows || []) {
      const key = [row.kind, row.jobId, row.path].join(":");
      const current = groups.get(key) || {
        kind: row.kind,
        jobId: row.jobId,
        path: row.path,
        count: 0,
        partialCount: 0,
      };
      current.count += 1;
      if (row.partial) current.partialCount += 1;
      groups.set(key, current);
    }
    return [...groups.values()];
  }

  const SOURCE_OUTCOME_STATUSES = ["processed", "no_event", "pending", "failed"];

  function sourceOutcomeStatus(outcome) {
    const status = String(outcome?.semantic_status || outcome?.status || "").trim();
    return SOURCE_OUTCOME_STATUSES.includes(status) ? status : "unknown";
  }

  function sourceOutcomeStatusCounts(outcomes) {
    const counts = {processed: 0, no_event: 0, pending: 0, failed: 0, unknown: 0};
    for (const outcome of Array.isArray(outcomes) ? outcomes : []) {
      counts[sourceOutcomeStatus(outcome)] += 1;
    }
    return counts;
  }

  function sourceScopedItemsForSegment(items, segment, sourceId = null) {
    const segmentId = String(segment?.segment_id || "");
    const resolvedSourceId = String(sourceId || segment?.source_id || segment?.sourceId || segment?.source_file_id || "");
    return (Array.isArray(items) ? items : []).filter((item) => {
      const evidence = item?.evidence && typeof item.evidence === "object" ? item.evidence : {};
      const itemSegmentId = String(item?.segment_id || evidence.segment_id || "");
      const itemSourceId = String(item?.source_id || evidence.source_id || item?.source_file_id || "");
      if (itemSourceId && resolvedSourceId && itemSourceId !== resolvedSourceId) return false;
      if (itemSegmentId) return Boolean(segmentId) && itemSegmentId === segmentId;
      return Boolean(itemSourceId && resolvedSourceId && itemSourceId === resolvedSourceId);
    });
  }

  function sourceOutcomeForSegment(outcomes, segment, sourceId = null) {
    const matches = sourceScopedItemsForSegment(outcomes, segment, sourceId);
    return matches.find((item) => String(item?.segment_id || item?.evidence?.segment_id || "") === String(segment?.segment_id || ""))
      || matches[0]
      || null;
  }

  function sourceCandidateTitle(candidate) {
    for (const value of [candidate?.title, candidate?.topic, candidate?.claim, candidate?.summary, candidate?.text, candidate?.description, candidate?.label]) {
      if (typeof value === "string" && value.trim()) return value.trim();
    }
    return "待复核主题候选";
  }

  function relationReviewState(candidate, review = null) {
    const decision = String(review?.decision || review?.review_status || candidate?.review_status || "pending").trim();
    if (["accepted", "confirmed"].includes(decision)) return "accepted";
    if (["rejected", "dismissed"].includes(decision)) return "rejected";
    return "pending";
  }

  function relationReviewMap(reviews) {
    const byCandidate = new Map();
    for (const review of Array.isArray(reviews) ? reviews : []) {
      const candidateId = String(review?.candidate_id || "");
      if (candidateId) byCandidate.set(candidateId, review);
    }
    return byCandidate;
  }

  const WATCH_SESSION_ATTENTION_STATES = ["superseded", "stalled", "incomplete"];

  // 会话生命周期只认服务端给的硬判据：superseded（同一设备已开出更晚会话）优先于任何时间阈值，
  // 因为设备同一时刻只有一个会话；只有拿不到新字段时才退回过期的 state。
  function watchSessionDisplayState(session) {
    if (!session || typeof session !== "object") return "unknown";
    const raw = String(session.status || session.state || "").trim();
    if (session.superseded === true || raw === "superseded") return "superseded";
    if (session.stalled === true || raw === "stalled") return "stalled";
    return raw || "unknown";
  }

  function watchSessionNeedsAttention(session) {
    const extractionStatus = String(session?.formal_job?.context_extraction?.status || "");
    return WATCH_SESSION_ATTENTION_STATES.includes(watchSessionDisplayState(session))
      || ["degraded", "failed"].includes(extractionStatus);
  }

  function watchSessionStartMillis(session, now = Date.now()) {
    const millis = Date.parse(String(session?.started_at || "").trim());
    if (!Number.isFinite(millis) || millis > now + 5 * 60 * 1000) {
      return Number.NEGATIVE_INFINITY;
    }
    return millis;
  }

  function sortWatchReceptionSessions(sessions, now = Date.now()) {
    const rows = Array.isArray(sessions) ? [...sessions] : [];
    return rows.sort((left, right) => {
      const leftTime = watchSessionStartMillis(left, now);
      const rightTime = watchSessionStartMillis(right, now);
      if (leftTime === rightTime) return 0;
      return rightTime > leftTime ? 1 : -1;
    });
  }

  function visibleWatchReceptionSessions(sessions, expanded, limit = 3) {
    const rows = Array.isArray(sessions) ? sessions : [];
    if (expanded) return [...rows];
    const safeLimit = Number.isFinite(Number(limit))
      ? Math.max(0, Math.floor(Number(limit)))
      : 3;
    return rows.slice(0, safeLimit);
  }

  function watchEmptyReceptionState(session) {
    if (!session || typeof session !== "object") return "Mac未收到音频分段";
    const received = Number(session.received_segments);
    if (Number.isFinite(received) && received > 0) return null;
    if (session.completion_pending === true) return "手表已报告结束 · Mac未收到音频分段";
    switch (watchSessionDisplayState(session)) {
      case "receiving": return "等待首个音频分段";
      case "superseded": return "旧会话 · Mac未收到音频分段";
      case "stalled": return "接收已停滞 · 尚未收到音频分段";
      case "incomplete": return "录音未收齐 · 没有可转写音频";
      case "complete": return "录音已结束 · Mac未收到音频分段";
      default: return "Mac未收到音频分段";
    }
  }

  function watchActivityAgeLabel(seconds) {
    if (seconds === null || seconds === undefined || seconds === "") return "";
    const raw = Number(seconds);
    if (!Number.isFinite(raw) || raw < 0) return "";
    if (raw < 60) return "刚刚";
    if (raw < 3600) return `${Math.floor(raw / 60)} 分钟前`;
    if (raw < 86400) return `${Math.floor(raw / 3600)} 小时前`;
    const days = raw / 86400;
    if (days >= 10) return `${Math.floor(days)} 天前`;
    return `${(Math.round(days * 10) / 10).toFixed(1)} 天前`;
  }

  function jobFailedFileCount(job) {
    const direct = Number(job?.failed_file_count);
    if (Number.isFinite(direct) && direct >= 0) return Math.floor(direct);
    return Array.isArray(job?.failed_files) ? job.failed_files.length : 0;
  }

  // 任务级 status 已经是 failed 时整行都在说失败，再挂一次计数是重复信息。
  function jobFailureSuffix(job) {
    if (!job || job.status === "failed") return "";
    const count = jobFailedFileCount(job);
    if (count <= 0) return "";
    const total = Number(job?.files?.length);
    return Number.isFinite(total) && total > 0
      ? ` · 失败 ${count}/${total} 个文件`
      : ` · 失败 ${count} 个文件`;
  }

  function statsProcessingLine(stats) {
    const processing = stats?.processing;
    if (!processing || typeof processing !== "object") return "";
    const pick = (value) => {
      const number = Number(value);
      return Number.isFinite(number) ? Math.floor(number) : null;
    };
    const parts = [];
    const processed = pick(processing.processed_file_count);
    const failed = pick(processing.failed_file_count);
    const unprocessed = pick(processing.unprocessed_job_count);
    const partial = pick(processing.job_status_counts?.partial);
    if (processed !== null) parts.push(`已处理 ${processed} 个文件`);
    if (failed !== null) parts.push(`失败 ${failed} 个`);
    if (unprocessed !== null) parts.push(`未处理任务 ${unprocessed} 个`);
    if (partial !== null && partial > 0) parts.push(`部分完成任务 ${partial} 个`);
    return parts.join(" · ");
  }

  function summarizeSyncResults(outcomes) {
    const rows = Array.isArray(outcomes) ? outcomes : [];
    const requiredFailures = [];
    const optionalFailures = [];
    const skipped = [];
    for (const row of rows) {
      const label = String(row?.label || "同步项");
      const status = row?.status;
      if (status === "success") continue;
      if (status === "skipped" && row?.optional) {
        skipped.push(label);
        continue;
      }
      if (row?.optional) optionalFailures.push(label);
      else requiredFailures.push(label);
    }
    return {
      status: requiredFailures.length ? "failed" : optionalFailures.length ? "partial" : "success",
      requiredFailures,
      optionalFailures,
      skipped,
    };
  }

  async function collectSyncSummary(refreshes) {
    const tasks = Array.isArray(refreshes) ? refreshes : [];
    if (!tasks.length) {
      return summarizeSyncResults([{label: "同步请求", status: "failed"}]);
    }
    const settled = await Promise.allSettled(tasks.map((item) => item.promise));
    return summarizeSyncResults(settled.map((result, index) => ({
      label: tasks[index].label,
      optional: Boolean(tasks[index].optional),
      status: result.status === "rejected"
        ? "failed"
        : ["success", "skipped"].includes(result.value?.status)
          ? result.value.status
          : "failed",
    })));
  }

  function syncToastMessage(summary) {
    if (summary?.status === "failed") {
      return {
        message: `同步未完成：${summary.requiredFailures.join("、")}读取失败，请稍后重试。`,
        isError: true,
      };
    }
    if (summary?.status === "partial") {
      return {
        message: `录音、任务与日记已更新；${summary.optionalFailures.join("、")}状态读取失败。`,
        isError: true,
      };
    }
    if (summary?.skipped?.length) {
      return {
        message: `同步完成：录音、任务与日记已更新（${summary.skipped.join("、")}未启用）。`,
        isError: false,
      };
    }
    return {message: "同步完成：最新录音、任务与日记已更新。", isError: false};
  }

  // 【2026-10-28 B6】主计数口径：**只有 canonical 才允许用「条原始记录」**。
  // 加载期常见的是“扫描/覆盖片段数”（例如 4,377），与完成态的 canonical 事件数（37）
  // 相差两个数量级，若共用同一标签会被读成“数据丢了”。这里把口径固定成数据契约：
  //   rawCountSource:
  //     "canonical" → day.eventCount / structuredEventCount（可与完成态比较）
  //     "scan"      → activityTimeline.raw_count / raw_record_count / coverage.source_count（扫描片段，未完成）
  //     "timeline"  → facts.events（时间轴统计，未完成）
  //     "unknown"   → 没有可用计数（绝不显示 0）
  // 返回值：
  //   text        摘要文案
  //   rawLabel    原始计数单位文案（无计数时为 null）
  //   comparable  是否可与完成态直接比较（只有 canonical 为 true）
  //   provisional 是否为未完成态
  function primaryCountSummary(input) {
    const data = input || {};
    // null/undefined/空串一律视为“未知”，绝不能经 Number(null)===0 变成 0（那会凭空造出“0 个活动”）。
    const toCount = (v) => (v === null || v === undefined || v === "" ? null
      : (Number.isFinite(Number(v)) ? Number(v) : null));
    const actCount = toCount(data.activityCount);
    const rawCount = toCount(data.rawCount);
    const source = ["canonical", "scan", "timeline", "unknown"].includes(data.rawCountSource)
      ? data.rawCountSource
      : "unknown";
    const fmt = typeof data.formatCount === "function" ? data.formatCount : (n) => String(n);
    const actText = actCount === null ? "" : `${fmt(actCount)} 个活动`;

    if (source === "canonical" && rawCount !== null) {
      return {
        text: `${actText} · ${fmt(rawCount)} 条原始记录`,
        rawLabel: `${fmt(rawCount)} 条原始记录`,
        comparable: true,
        provisional: false,
      };
    }
    if (source === "scan" && rawCount !== null) {
      return {
        // 明确写“扫描片段 + 未完成”，且刻意不使用「条原始记录」这一完成态口径
        text: `${actText}（候选）· 扫描片段 ${fmt(rawCount)} · 未完成`,
        rawLabel: `扫描片段 ${fmt(rawCount)}（未完成）`,
        comparable: false,
        provisional: true,
      };
    }
    if (source === "timeline" && rawCount !== null) {
      return {
        text: `${actText}（候选）· 时间轴统计 ${fmt(rawCount)} 条事件 · 未完成`,
        rawLabel: `时间轴统计 ${fmt(rawCount)} 条事件（未完成）`,
        comparable: false,
        provisional: true,
      };
    }
    return {
      text: actText ? `${actText}（候选）· 原始记录待统计` : "",
      rawLabel: null,
      comparable: false,
      provisional: true,
    };
  }

  // 【2026-10-28 返工】主口径数值的安全显示：
  //   - 有有效数值（含 canonical 的 0）→ 显示该数值
  //   - 全部未知（null/undefined）→ 显示占位「—」，**绝不输出 undefined/null**
  // eventCount 只在 primary 未给出数值时作为次选（canonical 兜底）。
  function primaryCountDisplay(input) {
    const data = input || {};
    const num = (v) => (v === null || v === undefined || v === "" ? null
      : (Number.isFinite(Number(v)) ? Number(v) : null));
    const primary = num(data.value);
    const fallback = num(data.eventCount);
    const chosen = primary !== null ? primary : fallback;
    const fmt = typeof data.formatCount === "function" ? data.formatCount : (n) => String(n);
    if (chosen === null) {
      return { value: null, text: "—", unknown: true, ariaLabel: "事件数统计中" };
    }
    return { value: chosen, text: fmt(chosen), unknown: false, ariaLabel: null };
  }

  return {
    primaryCountSummary,
    primaryCountDisplay,
    shanghaiDateFromTimestamp,
    watchSessionDisplayState,
    watchSessionNeedsAttention,
    sortWatchReceptionSessions,
    visibleWatchReceptionSessions,
    watchEmptyReceptionState,
    watchActivityAgeLabel,
    jobFailedFileCount,
    jobFailureSuffix,
    statsProcessingLine,
    summarizeSyncResults,
    collectSyncSummary,
    syncToastMessage,
    sourceFilenameStartMillis,
    relativeAbsoluteTimestamp,
    eventLocalDate,
    eventCrossesMidnight,
    sourceFileLocalDate,
    segmentLocalDate,
    recordingIntervalDates,
    recordingCoverageForDate,
    recordingCoverageSummary,
    recordingCoverageStatus,
    watchCoverageState,
    entryEvidenceDates,
    initialJournalDate,
    reviewLayer,
    reviewCandidateVisible,
    actionCandidateVisible,
    reviewCandidateKey,
    collapseReviewRows,
    groupReviewTargets,
    sourceOutcomeStatus,
    sourceOutcomeStatusCounts,
    sourceScopedItemsForSegment,
    sourceOutcomeForSegment,
    sourceCandidateTitle,
    relationReviewState,
    relationReviewMap,
  };
});

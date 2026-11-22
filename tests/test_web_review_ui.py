import plistlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_review_center_uses_unknown_review_only() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "声源待确认" in app
    assert "抽取待确认" in app
    assert "review-tab-voice" in app
    assert "review-tab-source" in app
    assert "统一复核中心" not in app
    assert "对着手表重说一遍" not in app
    assert "标记只影响列表" in app
    assert "普通声源" in app
    assert "抽取待确认暂时读不到" in app
    assert "正在读取抽取待确认的条目" in app
    assert 'operation: "读取抽取待确认"' in app
    assert "待人工审核暂时读不到" not in app
    assert "正在读取待人工审核的条目" not in app
    assert 'operation: "读取待人工审核"' not in app
    assert "<span>待复核</span>" not in app
    assert "普通待复核" not in app
    assert "sample-harvest.json" not in app


def test_import_reuses_interrupted_job_and_collapses_duplicate_history() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "reusableJobForSelection" in app
    assert "发现相同的中断任务，正在从原任务断点继续" in app
    assert "不再重复上传" in app
    assert "collapseDuplicateJobs" in app
    assert "次相同任务已折叠" in app
    assert '"X-Reuse-Existing": files.length === 1 ? "1" : "0"' in app
    assert "reused_existing_job" in app
    assert "SHA256 确认是同一份录音" in app


def test_auto_ingest_ui_exposes_local_watch_configuration() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert "refreshAutoIngest" in app
    assert "saveAutoIngest" in app
    assert "enableAutoIngest" in app
    assert 'const H8_RECORD_PATH = "/Volumes/H8精英版/RECORD"' in app
    assert "scanAutoIngestNow" in app
    assert "/api/auto-ingest" in app
    assert 'id="auto-ingest-enabled"' in index
    assert 'id="auto-ingest-folders"' in index
    assert 'id="choose-auto-ingest-folder"' in index
    assert 'id="enable-auto-ingest"' in index
    assert 'id="auto-ingest-history"' in index
    assert 'id="auto-ingest-scan-state"' in index
    assert "scanLabels" in app
    assert "已选择并保存监控目录" in app
    assert "重新挂载设备后会自动继续扫描" in app
    assert "chooseAutoIngestFolder" in app
    assert "renderAutoIngestHistory" in app
    assert "journalReviewCache" in app
    assert "`${jobId}:${path}:manual`" in app
    assert "`${jobId}:${path}:corrections`" in app
    assert "/api/auto-ingest/choose-folder" in app
    assert "文件大小和修改时间稳定后" in index
    assert "/Volumes/H8精英版/RECORD" in index
    assert "一键启用" in index
    assert index.count('id="current-card"') == 1
    assert index.index('class="surface auto-ingest-surface"') < index.index(
        'id="current-card"'
    )


def test_agent_examples_expose_one_brief_consumption_seam() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert "稳定消费 API: GET ${base}/api/daily-brief?date=2026-09-21" in app
    assert "api_features 包含 agent_brief_v1" in app
    assert "一个消费 API" in index
    assert "一个本地 outbox" in index
    assert "原始音频、全文转写、evidence 不出站" in index
    examples = app.split("function agentQueryExamplesText", 1)[1].split(
        "function agentPromptText", 1
    )[0]
    assert "/api/search" not in examples
    assert "/api/jobs" not in examples
    assert "/api/patterns" not in examples


def test_statistics_view_exposes_week_month_range_and_core_metrics() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'data-view="stats"' in index
    assert 'data-panel="stats"' in index
    assert 'data-stats-days="7"' in index
    assert 'data-stats-days="30"' in index
    assert 'id="stats-date-from"' in index
    assert 'id="stats-date-to"' in index
    assert 'id="stats-expense-total"' in index
    assert 'id="stats-exercise-count"' in index
    assert 'id="stats-category-counts"' in index
    assert 'id="stats-routine-distribution"' in index
    assert "refreshStatistics" in app
    assert "/api/stats?" in app
    assert "routine_distribution" in app
    assert "category_counts" in app
    assert index.count('data-panel="search"') == 1
    assert index.index('data-panel="stats"') < index.index('data-panel="search"')


def test_search_result_times_use_shanghai_wall_clock() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "function shanghaiDateTimeLabel(value" in app
    assert 'timeZone: "Asia/Shanghai"' in app
    assert "return shanghaiDateTimeLabel(item.timestamp" in app
    assert "timestamp.slice(0, 10)" not in app


def test_watch_recording_list_uses_human_titles_and_safe_fallbacks() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "function watchRecordingTitle(value)" in app
    assert "function watchRecordingSortMillis(value)" in app
    assert "function watchRecordingFreshnessLabel(value)" in app
    assert "function watchRecordingDetailTime(value, fallback)" in app
    assert "function compactDurationLabel(milliseconds)" in app
    assert "时长未知" in app
    assert "正在录音中 · 已录制" in app
    assert "正在准备录音 · 等待首段" in app
    assert "录音已收齐 · 等待处理" in app
    assert "状态待确认" in app
    assert "Array.isArray(payload.sessions)" in app
    assert 'typeof item === "object"' in app
    assert "最近接收" in app


def test_product_design_polish_contracts_are_present() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    # 【2026-10-17 修】测试腐烂：2026-10 的 Apple 风格改版（body 底色 #fbfbfd、
    # 面板圆角收紧）把 --radius-panel 从 22px 调到 20px，令牌本身是刻意改的，
    # 测试没跟着更新。这里对齐当前设计令牌，并保留“令牌必须存在”的本意。
    assert "--radius-panel: 20px" in css
    assert ".selected-files { --nested-padding: 8px; --nested-radius: 10px;" in css
    assert "border-radius: calc(var(--nested-radius) + var(--nested-padding))" in css
    assert "padding: 15px; border-radius: 25px" in css
    assert ".review-day-list { --nested-padding: 8px; --nested-radius: 14px;" in css
    assert ".review-day-list { --nested-padding: 6px; --nested-radius: 12px;" in css
    assert "min-height: 72px; padding: 13px; border-radius: var(--nested-radius)" in css
    assert 'input[type="date"], input[type="datetime-local"], input[type="number"], input[type="time"]' in css
    assert ".auto-ingest-history { --nested-padding: 8px; --nested-radius: 10px;" in css
    assert ".auto-ingest-history { --nested-padding: 6px;" in css
    assert ".auto-ingest-history-row { display: grid;" in css
    assert "-webkit-line-clamp: 2" in css
    assert css.count(".selected-files { --nested-padding: 8px; --nested-radius: 10px;") == 1
    assert ".selected-files .file-name { display: -webkit-box;" in css
    assert "text-wrap: pretty" in css
    assert "font-variant-numeric: tabular-nums" in css
    assert "text-wrap: balance" in css
    assert "review-day-card:focus-visible" in css
    assert '.primary[aria-busy="true"]' in css
    assert "reviewPulse" in css
    assert "正在整理声源待确认内容" in app
    assert "等待你选择要处理的录音" in app
    assert "需要我判断的内容已更新；声源与抽取分开计数。" in app
    assert "需要我判断的内容已是最新；声源与抽取分开计数。" in app
    assert 'reviewStatus.textContent = pendingCount' in app
    assert 'sourceInfo.value ? "声源待确认已清空；抽取待确认仍有条目。"' in app
    assert '<div class="review-empty" role="status">' not in app
    assert '.review-open::after { content: "›"' in css
    assert ".review-day-card:is(:hover, :focus-visible) .review-open::after" in css
    assert "function renderSelected({preserveUploadStatus = false} = {})" in app
    assert "renderSelected({preserveUploadStatus: true})" in app
    assert ".file-picker:focus-within" in css, "缺少 file-picker 焦点态（:focus-within）"
    assert ".file-picker:focus-visible" in css, "缺少 file-picker 键盘焦点环（:focus-visible）"
    assert "声源待确认内容暂时读取失败" in app
    assert 'reviewRoot.innerHTML = \'<div class="review-empty"><div class="review-empty-icon">···</div>' in app
    assert 'reviewRoot.innerHTML = `<div class="review-empty"><div class="review-empty-icon">!</div>' in app
    assert 'setImportLiveStatus("checking", "正在重新启动", jobId)' in app
    assert 'setImportLiveStatus("ready", "任务已重新启动", jobId)' in app
    assert 'setImportLiveStatus("error", "重新启动失败", error.message)' in app
    assert 'id="upload-status" class="status-copy" role="status" aria-live="polite" aria-atomic="true"' in html
    assert 'id="manual-drop-hero" class="drop-hero"' in html
    assert ".drop-hero.drag-active {" in css
    assert 'manualDropHero.addEventListener("dragover"' in app
    assert 'manualDropHero.addEventListener("drop", handleManualImportDrop)' in app
    assert 'setImportLiveStatus("warning", "没有可导入的音频"' in app
    assert 'id="review-action-status" class="setting-status" role="status" aria-live="polite" aria-atomic="true"' in html
    assert 'id="review-dashboard" class="review-dashboard" role="region" aria-label="需要我判断" aria-busy="true"' in html
    assert '<h2 id="review-heading">需要我判断</h2>' not in html
    assert "review-heading" not in html
    assert ".status-copy.ready::before" in css
    assert ".status-copy.ready, .setting-status.ready { color: #55705e; }" in css
    assert ".status-copy.error, .setting-status.error { color: #9b625d; }" in css
    assert ".current-live-status, .journal-freshness {" in css
    assert ".current-live-status.ready, .journal-freshness.ready {" in css
    assert 'font-variant-numeric: tabular-nums; font-feature-settings: "tnum" 1;' in css
    assert '.job-row .muted, .job-mode { font-variant-numeric: tabular-nums;' in css
    assert ".job-title .muted { white-space: normal; text-wrap: pretty; }" in css
    assert ".local-reminder-copy span" not in css
    assert ".pending-review-bulk { min-height: 30px;" in css
    assert ".pending-review-scope {" in css
    assert "pendingReviewBulkIgnore()" in app
    assert "命中 ${pendingReviewNumber(totals.scoped)}" not in app
    assert '${state?.allDates ? "全库" : "本日"}开放需确认 ${actionableText} · 抽取异常 ${gapText}' in app
    assert 'box.setAttribute("role", "status")' not in app
    assert 'box.setAttribute("aria-live", "polite")' not in app
    assert ".status-copy.checking::before, .setting-status.checking::before" in css
    assert "@keyframes statusPulse" in css
    assert ".status-copy.checking::before, .setting-status.checking::before { animation: none; }" in css
    # 只数「选择器恰好是 .primary」的规则，别把 `.broadcast-action.primary {` 这种复合选择器也算进去。
    # 原写法 css.count(".primary {") 是子串计数，任何 `xxx.primary {` 都会命中，会在新增合法复合类时误报。
    assert sum(1 for line in css.splitlines() if line.strip().startswith(".primary {")) == 1
    assert css.count(".file-picker { min-height: 40px;") == 1
    assert css.count(".file-picker.secondary {") == 1
    assert css.count(".file-picker input {") == 1
    assert 'id="watch-reception-status" class="status-copy checking" role="status" aria-live="polite" aria-atomic="true"' in html
    assert 'id="watch-reception-sessions" class="auto-ingest-history" aria-busy="true"' in html
    assert 'status.className = "status-copy ready"' in app
    assert 'status.className = "status-copy error"' in app
    assert 'root.setAttribute("aria-busy", "false")' in app
    assert ".day-event-meta, .review-summary strong, .review-summary small" in css


def test_watch_lock_screen_meter_recovery_contract_is_preserved() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    coordinator = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "WatchRecordingCoordinator.swift"
    ).read_text(encoding="utf-8")
    home = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "RecorderHomeView.swift"
    ).read_text(encoding="utf-8")

    assert "guard let currentRecorder, currentRecorder.isRecording else {" in coordinator
    assert ("levels.append(0)" in coordinator or "meter.push(0)" in coordinator or "meter.push(level)" in coordinator)
    assert "currentRecorder.updateMeters()" in coordinator
    assert "WatchCaptureProtectionPolicy.shouldMeterUpdate(" in home
    assert "while !Task.isCancelled {" in home
    assert "recorder.updateMeter()" in home
    assert "recorder.suspendMeterDisplay()" in home
    assert "recorder.reconcileAfterForeground()" in home
    assert "AVAudioSession.interruptionNotification" in coordinator
    assert "handleAudioSessionInterruption" in coordinator
    assert ("try audioSession.setActive(true)" in coordinator or "try AVAudioSession.sharedInstance().setActive(true)" in coordinator)
    assert "isAudioInterrupted = true" in coordinator
    assert "isAudioInterrupted = false" in coordinator
    assert "func reconcileAfterForeground()" in coordinator
    assert "if handledRecorderPaths.contains(standardizedPath)" in coordinator
    assert "handledRecorderPaths.insert(standardizedPath)" in coordinator
    assert "try startSegment(sequence: context.sequence + 1)" in coordinator
    assert "currentRecorder = nil" in coordinator
    assert "小时" in app
    assert "还没有收到 Apple Watch 录音" in app
    assert "sessions.sort(" in app
    assert "设备时间异常" in app
    assert "刚刚" in app
    assert "seenWatchSessions" in app


def test_watch_interruption_failure_recovers_record_button() -> None:
    coordinator_path = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "WatchRecordingCoordinator.swift"
    )
    home_path = ROOT / "watchos" / "LifeLoggerWatch" / "App" / "RecorderHomeView.swift"
    coordinator = coordinator_path.read_text(encoding="utf-8")
    home = home_path.read_text(encoding="utf-8")

    assert "private func handleAudioSessionInterruption" in coordinator
    assert "CaptureStopDisposition.resolve(" in coordinator
    assert "case .finalizeSessionWithoutRecorder:" in coordinator
    assert "private func finalizeSessionWithoutRecorder()" in coordinator
    assert "recorder.isRecording" in home


def test_watch_failed_segment_enters_busy_state_before_async_finalization() -> None:
    coordinator = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "WatchRecordingCoordinator.swift"
    ).read_text(encoding="utf-8")
    assert "private func handleFinishedSegment(" in coordinator
    assert "guard let sessionID = currentSessionID else { return }" in coordinator
    assert "state = .stopping" in coordinator
    assert "private func recoverStagedAudio(" in coordinator
    assert "nonisolated func audioRecorderEncodeErrorDidOccur" in coordinator


def test_watch_segment_offsets_and_completion_respect_persisted_staging() -> None:
    coordinator = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "WatchRecordingCoordinator.swift"
    ).read_text(encoding="utf-8")
    assert "private func startSegment(sequence: Int)" in coordinator
    assert "private func recoverStagedAudio(" in coordinator
    assert "private func finalizeSessionWithoutRecorder()" in coordinator
    assert "CaptureSessionCompletionDisposition.resolve" in coordinator
    assert "func start()" in coordinator
    assert "func stop()" in coordinator


def test_watch_home_manual_sync_shows_acknowledged_pending_count() -> None:
    home = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "RecorderHomeView.swift"
    ).read_text(encoding="utf-8")
    sync_source = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "MacWatchSessionTransport.swift"
    ).read_text(encoding="utf-8")
    status_policy = (
        ROOT
        / "watchos"
        / "LifeLoggerWatch"
        / "CorePackage"
        / "Sources"
        / "WatchCore"
        / "WatchSyncStatusPolicy.swift"
    ).read_text(encoding="utf-8")

    assert "homeSyncPanel" in home or "HomeSyncPanel" in home
    assert "recorder.pendingSegmentCount" in home
    assert "sync.isSyncing" in home
    assert "Mac 同步" in home
    assert "Mac 当前不可达" in status_policy
    assert "WatchSyncStatusPolicy" in sync_source


def test_watch_queue_is_chinese_and_home_shows_small_build_number() -> None:
    home = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "RecorderHomeView.swift"
    ).read_text(encoding="utf-8")
    project = (
        ROOT / "watchos" / "LifeLoggerWatch" / "project.yml"
    ).read_text(encoding="utf-8")
    app_info = plistlib.loads(
        (
            ROOT
            / "watchos"
            / "LifeLoggerWatch"
            / "App"
            / "WatchAppInfo.plist"
        ).read_bytes()
    )

    for label in (
        "手表已保存",
        "Mac 同步",
        "Mac 连接",
    ):
        assert label in home
    assert "待传队列" in home or "上传队列" in home
    assert 'CFBundleVersion:' in project
    assert 'CFBundleVersion' in app_info
    assert 'CFBundleVersion' in home


def test_watch_app_previews_do_not_require_swift_macro_plugin() -> None:
    home = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "RecorderHomeView.swift"
    ).read_text(encoding="utf-8")
    settings = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "MacSyncSettingsView.swift"
    ).read_text(encoding="utf-8")

    assert "#Preview" not in home
    assert "#Preview" not in settings
    assert "RecorderHomeView_Previews: PreviewProvider" in home
    assert "MacSyncSettingsView_Previews: PreviewProvider" in settings


def test_watch_project_uses_personal_team_automatic_signing() -> None:
    project = (
        ROOT / "watchos" / "LifeLoggerWatch" / "project.yml"
    ).read_text(encoding="utf-8")

    assert "CODE_SIGN_STYLE: Automatic" in project
    assert 'DEVELOPMENT_TEAM:' in project


def test_watch_view_models_publish_state_without_observation_macros() -> None:
    recorder = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "WatchRecordingCoordinator.swift"
    ).read_text(encoding="utf-8")
    sync = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "MacWatchSessionTransport.swift"
    ).read_text(encoding="utf-8")
    home = (
        ROOT / "watchos" / "LifeLoggerWatch" / "App" / "RecorderHomeView.swift"
    ).read_text(encoding="utf-8")

    assert "import Combine" in recorder
    assert "import Combine" in sync
    assert "final class WatchRecordingCoordinator:" in recorder
    assert "final class WatchSyncCoordinator: ObservableObject" in sync
    for published_property in (
        "@Published private(set) var state",
        "@Published private(set) var statusMessage",
        "@Published private(set) var pendingSegmentCount",
        "@Published private(set) var sessions",
        "@Published private(set) var hasUnrecoveredStagedAudio",
    ):
        assert published_property in recorder
    assert "@Published private(set) var isSyncing" in sync
    assert "@Published private(set) var statusMessage" in sync
    assert "syncCoordinator: WatchSyncCoordinator" in recorder
    assert "@StateObject private var recorder" in home or "@ObservedObject var recorder: WatchRecordingCoordinator" in home
    assert "private var sync: WatchSyncCoordinator { recorder.syncCoordinator }" in home
    assert "@ObservedObject var recorder" in home
    assert "@ObservedObject var sync" in home
    assert "@Observable" not in recorder + sync
    assert "@ObservationIgnored" not in recorder + sync


def test_patterns_view_shows_minimum_sample_hint_and_evidence() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'data-open-view="patterns"' in index
    assert 'data-panel="patterns"' in index
    assert 'data-pattern-days="14"' in index
    assert 'data-pattern-days="30"' in index
    assert 'id="patterns-sample-hint"' in index
    assert 'id="patterns-results"' in index
    assert "refreshPatterns" in app
    assert "/api/patterns?" in app
    assert "renderPatternEvidence" in app
    assert "查看证据" in app
    assert "sample_hint" in app


def test_job_details_show_simple_time_source_or_unknown() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "renderTimeCalibration" not in app
    assert "time_calibration" not in app
    assert "timeSourceLabel" in app
    assert "precisionLabel" in app
    assert "文件名时间 / 未知" in app
    assert (
        'const absoluteTime = file.effective_start_at || file.inferred_start_at || ""'
        in app
    )
    assert 'shanghaiDateTimeLabel(absoluteTime, "未获得绝对时间")' in app


def test_mobile_access_ui_requires_token_and_exposes_lan_toggle() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    style = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    viewer = (ROOT / "web" / "viewer.js").read_text(encoding="utf-8")
    viewer_style = (ROOT / "web" / "viewer.css").read_text(encoding="utf-8")

    assert 'data-settings-page-target="mobile"' in index
    assert 'data-settings-page="mobile"' in index
    assert 'id="lan-access-enabled"' in index
    assert 'id="mobile-access-token"' in index
    assert 'id="mobile-pair-url"' in index
    assert 'id="access-token-overlay"' in index
    assert 'aria-describedby="access-token-status"' in index
    assert 'id="access-token-status" class="setting-status" role="status"' in index
    assert "requestAnimationFrame(() => input?.focus())" in app
    assert 'id="natural-correction-input"' in index
    assert 'id="mobile-bottom-nav"' in index
    assert 'data-mobile-view="today"' in index
    assert 'data-mobile-view="review"' in index
    assert 'setMobileBriefLoading(true, "正在读取今日简报")' in app
    assert 'setMobileBriefLoading(false, "今日简报已更新")' in app
    assert 'id="natural-correction-input"' in index
    assert 'id="mobile-bottom-nav"' in index
    assert 'id="mobile-capture-card"' in index
    assert (
        'id="mobile-capture-card" class="mobile-capture-card" aria-busy="false"'
        in index
    )
    assert 'id="mobile-capture-status" class="setting-status" role="status"' in index
    assert 'id="mobile-capture-input"' in index
    assert 'capture="microphone"' in index
    assert 'id="mobile-capture-enabled"' in index
    assert 'id="mobile-capture-auto-start"' in index
    assert (
        'id="mobile-access-surface" class="surface mobile-access-surface" '
        'aria-busy="false"' in index
    )
    assert (
        'id="mobile-capture-settings" class="mobile-capture-settings" aria-busy="false"'
        in index
    )
    assert (
        'id="mobile-capture-settings-status" class="setting-status" role="status"'
        in index
    )
    assert 'id="mobile-access-status" class="setting-status" role="status"' in index
    assert 'aria-describedby="mobile-access-status"' in index
    assert 'aria-describedby="mobile-capture-settings-status"' in index
    assert 'rel="manifest" href="/manifest.webmanifest"' in index
    assert 'id="trusted-network-bind"' in index
    assert 'id="review-action-status"' in index
    assert "不要做路由器端口映射" in index
    assert 'headers.set("Authorization", `Bearer ${accessToken}`)' in app
    assert 'headers.set("X-Life-Logger-CSRF", "1")' in app
    assert "history.replaceState" in app
    assert "/api/mobile-access" in app
    assert "/api/daily-brief" in app
    assert "renderMobileBrief" in app
    assert "sendMobileBrief" in app
    assert "runMobileDoctor" in app
    assert "renderMobileCapture" in app
    assert "handleMobileCapture" in app
    assert 'card.setAttribute("aria-busy", "true")' in app
    assert 'card.setAttribute("aria-busy", "false")' in app
    assert "录音已交给导入流程" in app
    assert "/api/mobile-capture" in app
    assert 'settings.setAttribute("aria-busy", "true")' in app
    assert 'settings.setAttribute("aria-busy", "false")' in app
    assert 'surface.setAttribute("aria-busy", "true")' in app
    assert 'surface.setAttribute("aria-busy", "false")' in app
    assert "same-origin" not in index.lower() or "job upload" in index.lower()
    assert 'switchView("review")' in app
    assert "trusted_network_bind" in app
    assert "secureWriteHeaders" in viewer
    assert '"X-Life-Logger-CSRF": "1"' in viewer
    assert ".mobile-brief-surface { display: none; }" in style
    assert "@media (max-width: 620px)" in style
    assert ".mobile-brief-surface { display: block;" in style
    assert ".mobile-brief-actions { display: grid;" in style
    assert ".mobile-doctor-action { grid-column: 1 / -1; }" in style
    assert ".mobile-capture-card { display: grid;" in style
    assert "min-height: 44px" in style
    assert ".review-day-card { align-items: stretch; flex-direction: column;" in style
    assert ".review-button { width: 100%; min-height: 44px; }" in viewer_style


def test_model_preferences_expose_busy_disabled_and_live_save_state() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert (
        'id="model-settings-surface" class="surface model-settings-surface" '
        'aria-busy="true" '
        'aria-describedby="model-settings-load-status model-save-status"'
        in index
    )
    assert 'id="model-save-status" role="status" aria-live="polite"' in index
    assert 'id="model-settings-load-status" class="sr-only" role="status"' in index
    assert 'loadStatus.textContent = "正在读取本机模型配置"' in app
    assert '"本机模型配置已读取"' in app
    assert '"本机模型配置读取失败，已显示安全默认值"' in app
    assert 'surface.setAttribute("aria-busy", "true")' in app
    assert 'surface.setAttribute("aria-busy", "false")' in app
    assert "saveButton.disabled = true" in app
    assert "recommendedButton.disabled = true" in app
    assert "recommendedButton.disabled = false" in app


def test_decision_layer_settings_ui_is_removed() -> None:
    # 2026-11-06 决策层已删除（Q918），设置页不再有 Jev 密钥、SDK 和决策增强选择器
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    for marker in ('id="decision-provider"', 'id="jev-key-panel"', 'id="jev-sdk-panel"', 'id="jev-action-status"'):
        assert marker not in index


def test_onboarding_dialog_moves_and_restores_focus() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "let onboardingReturnFocus = null" in app
    assert 'requestAnimationFrame(() => $("onboarding-recommended")?.focus())' in app
    assert "function closeOnboarding({restoreFocus = true} = {})" in app
    assert "requestAnimationFrame(() => target.focus())" in app
    assert 'event.key !== "Escape"' in app
    assert 'requestAnimationFrame(() => $("settings-tab-models")?.focus())' in app


def test_primary_web_polish_keeps_motion_fast_accessible_and_gpu_friendly() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    assert "@keyframes view-in" not in style
    assert "animation: view-in" not in style
    assert "transition: width" not in style
    assert "--ease-out: cubic-bezier(0.23, 1, 0.32, 1)" in style
    # 【2026-10-17 修】同上：改版把按压反馈 140ms→100ms、UI 过渡 180ms→150ms，
    # 目的是让点击响应更快。断言对齐现值，仍锁住“动效必须短”的约束。
    assert "--motion-press: 100ms" in style
    assert "--motion-ui: 150ms" in style
    assert "transform-origin: left center" in style
    assert "@media (hover: hover) and (pointer: fine)" in style
    assert "@media (prefers-reduced-motion: reduce)" in style
    assert "button:focus-visible" in style
    assert "passport-stream-surface:has(#passport-stream-badge.ready)" in style
    assert "const progressScale = ratio / 100;" in app
    assert 'style="transform:scaleX(${progressScale})"' in app
    assert 'style="width:${ratio}%"' not in app


def test_journal_loading_empty_and_date_selection_states_are_accessible() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    style = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    assert (
        'id="journal-dashboard" class="journal-dashboard" role="region" '
        'aria-labelledby="journal-heading" aria-busy="true"' in index
    )
    assert 'id="journal-load-status" class="sr-only" role="status"' in index
    assert 'id="journal-freshness" class="journal-freshness checking"' in index
    assert 'aria-describedby="journal-load-status journal-freshness"' not in index
    assert "journal-skeleton" in index
    assert 'aria-current="date"' in app
    assert 'setJournalLoading(false, "日记已更新")' in app
    assert "日记刷新失败" in app
    assert "日记暂时不可用" in app
    assert "journalFreshnessAgeLabel" in app
    assert "markJournalRefreshSuccess" in app
    assert "markJournalRefreshFailure" in app
    assert 'chip.className = "journal-freshness stale"' in app
    assert "if (root && !journalDays.length)" in app
    assert ".journal-freshness.stale" in style
    assert "@keyframes journal-skeleton-pulse" in style
    assert ".journal-skeleton span { animation: none;" in style


def test_passport_companion_status_is_accessible_and_semantic() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    style = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    assert 'id="passport-stream-badge" class="setting-badge" role="status"' in index
    assert 'id="passport-receiver-summary" aria-live="polite"' in index
    assert (
        'id="passport-stream-sessions" class="auto-ingest-history" aria-busy="false"'
        in index
    )
    assert 'root.setAttribute("role", "list")' in app
    assert 'root.removeAttribute("role")' in app
    assert 'id="passport-stream-status" class="status-copy" aria-live="polite"' in index
    assert 'role="listitem" aria-label="${escapeHtml(rowLabel)}"' in app
    assert "AI Passport companion：${badge.textContent}" in app
    assert (
        'id="passport-receiver-health" class="passport-receiver-health" '
        'role="group"' in index
    )
    assert 'id="passport-http-health" class="receiver-health-chip checking"' in index
    assert (
        'id="passport-discovery-health" class="receiver-health-chip checking"' in index
    )
    assert 'id="passport-import-health" class="receiver-health-chip checking"' in index
    assert 'importChip.textContent = `完整会话 · ${policy}`' in app
    assert '完整会话自动处理' in app
    assert '完整会话仅建草稿' in app
    assert "renderPassportReceiverHealth" in app
    assert "shanghaiDateTimeLabel(\n      item.updated_at" in app
    assert "shanghaiDateTimeLabel(item.at" in app
    assert "status.last_scan_at" in app
    assert "toLocaleString()" not in app
    assert (
        'badge.textContent = running ? "Companion 正常" : enabled ? '
        '"Companion 异常" : "Companion 关闭"' in app
    )
    assert 'TCP ${httpPort} · ${httpRunning ? "正在监听" : "未监听"}' in app
    assert (
        'UDP ${discoveryPort} · ${discoveryRunning ? "自动发现正常" : '
        '"自动发现异常"}' in app
    )
    assert 'badge.textContent = latest.status === "complete"' not in app
    assert ".receiver-health-chip.ready" in style
    assert ".auto-ingest-history-row.session-complete em" in style
    assert (
        'id="passport-stream-surface" class="surface auto-ingest-surface '
        'passport-stream-surface" aria-busy="true"' in index
    )
    assert 'class="receiver-health-chip checking"' in index
    assert "setPassportReceiverUnknown" in app
    assert 'badge.textContent = "状态未知"' in app
    assert "chip.textContent = `${label} · 检查失败`" in app
    assert ".receiver-health-chip.checking" in style


def test_p4_batch_d_ui_exposes_alerts_and_kanban() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'id="pattern-alerts"' in index
    assert 'id="kanban-dashboard"' in index
    assert "alert_candidates" in app
    assert "renderKanban" in app
    assert "kanban: [\"TASK BOARD\", \"任务看板\"]" in app
    assert '"读取中"' in app
    assert "计数加载中" not in app
    assert 'dayBadgeByDate' in app


def test_review_center_announces_loading_empty_and_pending_lists() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert (
        'id="review-dashboard" class="review-dashboard" role="region" '
        'aria-label="需要我判断" aria-busy="true"' in index
    )
    assert 'root.setAttribute("aria-busy", "false")' in app
    assert 'class="review-empty" role="status"' not in app
    assert 'id="review-action-status" class="setting-status" role="status" aria-live="polite" aria-atomic="true"' in index
    assert 'class="review-day-list" role="list"' in app
    assert 'class="review-day-card" role="listitem"' in app
    assert 'reviewRoot.setAttribute("aria-busy", "true")' in app


def test_review_viewer_counts_only_the_default_visible_queue() -> None:
    viewer = (ROOT / "web" / "viewer.js").read_text(encoding="utf-8")

    assert "visibleRecords.filter((item) => isReviewed" in viewer
    assert "pendingCount} 待确认" in viewer
    assert "主角标只统计当前默认可见队列" in viewer


def test_review_viewer_exposes_fast_keyboard_shortcuts() -> None:
    viewer = (ROOT / "web" / "viewer.js").read_text(encoding="utf-8")

    assert '"1": "self"' in viewer
    assert '"2": "other_person"' in viewer
    assert '"3": "media"' in viewer
    assert '"4": "non_speech"' in viewer
    assert '"5": "unclear"' in viewer
    assert 'key === "j"' in viewer
    assert 'key === "k"' in viewer
    assert 'key === " "' in viewer
    assert "isTypingTarget(event.target)" in viewer


def test_review_viewer_exposes_audio_clip_playback() -> None:
    viewer = (ROOT / "web" / "viewer.js").read_text(encoding="utf-8")

    assert "audioClipUrl" in viewer
    assert "/audio-clip?" in viewer
    assert "review-audio-button" in viewer
    assert "试听本段" in viewer


def test_day_axis_uses_filtered_timeline_without_hiding_workspace_records() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "fallbackTimelineEvents(curatedEvents)" in app
    assert "timelineEvents" in app
    assert "workspaceEvents" in app
    assert "structuredEventCount" in app
    assert "发生了什么" in app
    assert "dayWord" in app


def test_daily_workspace_deduplicates_cards_and_filters_fragment_ideas() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "dedupeWorkspaceEvents" in app
    assert "isUsefulIdeaEvent" in app
    assert (
        "const cleanedWorkspaceEvents = dedupeWorkspaceEvents(workspaceEvents);" in app
    )
    assert "filter(isUsefulIdeaEvent)" in app


def test_file_selection_announces_count_and_list_semantics() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert (
        'id="selected-files" class="selected-files empty" '
        'aria-describedby="selected-files-status"' in index
    )
    assert 'id="selected-files-status" class="sr-only" role="status"' in index
    assert 'box.setAttribute("role", "list")' in app
    assert 'role="listitem"' in app
    assert "已选择 ${formatCount(files.length)} 个文件，共 ${formatBytes(totalBytes)}" in app
    assert 'box.removeAttribute("role")' in app


def test_global_refresh_has_user_only_busy_and_live_status() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert 'id="focus-capture-button"' in index
    assert 'id="global-refresh"' not in index
    assert 'id="journal-refresh"' not in index
    assert 'id="sync-now-button"' not in index
    assert "async function runGlobalRefresh()" in app
    assert '$("global-refresh")?.addEventListener("click", runGlobalRefresh)' in app
    assert "await refreshAll(true)" in app


def test_job_history_exposes_list_busy_and_failure_states() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    assert (
        'id="jobs-list" class="jobs-list" role="list" '
        'aria-labelledby="jobs-heading" '
        'aria-describedby="jobs-load-status jobs-live-status" '
        'aria-busy="true"' in index
    )
    assert 'id="jobs-load-status" class="sr-only" role="status"' in index
    assert 'id="jobs-live-status" class="setting-badge" role="status"' in index
    assert 'class="job-list-item" role="listitem"' in app
    assert 'setJobsLoading(true, "正在读取任务历史")' in app
    assert "function setJobsLiveStatus(state, label, detail = \"\")" in app
    assert 'setJobsLiveStatus("error", "读取失败", error.message)' in app
    assert 'setJobsLiveStatus("ready", visibleJobs.length' in app
    assert (
        "setJobsLoading(false, `任务历史已更新，共 ${formatCount(visibleJobs.length)} 个任务`)"
        in app
    )
    assert "任务读取失败" in app
    assert "function jobHistoryLocalDate(job)" in app
    assert "journalSourceFileLocalDate(firstFile, null)" in app
    assert "journalShanghaiDateFromTimestamp(job?.created_at, \"\")" in app
    assert "effective_start_at?.slice(0, 10)" not in app
    assert ".job-list-item:last-child .job-row" in style


def test_current_job_exposes_busy_live_and_failure_states() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert (
        'id="current-card" class="surface current-surface hidden" aria-busy="false"'
        in index
    )
    assert (
        'id="current-job" role="region" aria-labelledby="current-job-heading"' in index
    )
    assert 'id="current-job-load-status" class="sr-only" role="status"' in index
    assert 'id="current-job-live-status"' in index
    assert 'aria-live="polite"' in index
    assert 'aria-describedby="current-job-load-status current-job-live-status"' in index
    assert "setCurrentJobLoading(true, `正在读取任务 ${jobId}`)" in app
    assert "function renderCurrentJobLiveStatus(job)" in app
    assert 'setCurrentJobLiveStatus("ready", "实时更新 · 1.5s"' in app
    assert 'setCurrentJobLiveStatus("warning", "刷新失败"' in app
    assert 'setCurrentJobLiveStatus("error", "读取失败"' in app
    assert "任务读取失败" in app
    assert "任务状态刷新失败" in app


def test_auto_ingest_exposes_loading_empty_and_list_semantics() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert (
        'id="auto-ingest-surface" class="surface auto-ingest-surface" aria-busy="true"'
        in index
    )
    assert 'id="auto-ingest-badge" class="setting-badge" role="status"' in index
    assert 'id="auto-ingest-status" class="status-copy" role="status"' in index
    assert index.count('aria-describedby="auto-ingest-status"') == 4
    assert 'id="auto-ingest-history" class="auto-ingest-history"' in index
    assert 'root.setAttribute("aria-label", "自动导入最近活动")' in app
    assert 'setAutoIngestLoading(true, "正在读取自动导入状态……")' in app
    assert 'role="listitem"' in app
    assert "还没有自动导入活动" in app
    assert 'badge.textContent = "读取失败"' in app


def test_stats_and_patterns_expose_busy_and_live_status() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert 'id="stats-surface" class="surface stats-surface" aria-busy="false"' in index
    assert (
        'id="patterns-surface" class="surface patterns-surface" '
        'aria-busy="false"' in index
    )
    assert 'id="stats-status" class="search-summary" role="status"' in index
    assert 'id="stats-empty" class="stats-empty hidden" role="status"' in index
    assert 'empty.classList.toggle("hidden", hasData)' in app
    assert 'button.textContent = isBusy ? "汇总中…" : "查看统计"' in app
    assert 'id="patterns-status" class="search-summary" role="status"' in index
    assert "setStatisticsBusy(true)" in app
    assert "setStatisticsBusy(false)" in app
    assert "setPatternsBusy(true)" in app
    assert "setPatternsBusy(false)" in app
    assert (
        'id="patterns-results" class="pattern-observation-list" aria-busy="false"'
        in index
    )
    assert 'class="pattern-card" role="listitem"' in app
    assert 'root.setAttribute("role", "list")' in app
    assert "renderPatternsLoading()" in app
    assert 'button.textContent = isBusy ? "读取中…" : "查看规律"' in app
    assert '当前范围还没有可展示规律' in app


def test_search_surface_exposes_busy_live_and_list_semantics() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert (
        'id="search-form" class="global-search-form" '
        'aria-describedby="search-summary" aria-busy="false"' in index
    )
    assert (
        'id="search-summary" class="search-summary" role="status" '
        'aria-live="polite" aria-atomic="true"' in index
    )
    assert 'id="search-results" class="search-results" aria-busy="false"' in index
    assert 'id="search-submit" class="search-submit"' in index
    assert 'role="listitem"' in app
    assert 'root.setAttribute("role", "list")' in app
    assert 'root.removeAttribute("role")' in app
    assert "setSearchBusy(true)" in app
    assert "setSearchBusy(false)" in app
    assert "renderSearchLoading()" in app
    assert 'submit.disabled = Boolean(isBusy)' in app
    assert 'submit.textContent = isBusy ? "搜索中…" : "搜索"' in app
    assert "当前筛选没有结果" in app
    assert 'class="search-empty" role="status"' in app
    assert "找到 ${formatCount(results.length)} 条" in app
    assert 'data-search-type="all" aria-pressed="true"' in index
    assert 'data-search-type="utterance" aria-pressed="false"' in index
    assert 'item.setAttribute("aria-pressed", active ? "true" : "false")' in app


def test_daily_navigation_uses_date_neutral_copy() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'today: ["TODAY", "今天"]' in app
    assert "当天工作台" in app
    assert "当日回顾" in app
    assert "当日事件轴" in app
    # 【2026-10-17 修】导航重构后不再有独立的「日记」按钮，
    # 日记/当日视图并入「时间轴 (Timeline)」；<h1> 仍叫「日记」。
    assert ">今天</span></button>" in index
    assert '<h1 id="page-title">今天</h1>' in index


def test_journal_date_navigation_announces_groups_and_no_evidence_days() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    # [2026-10-14] 日期轴由「快捷日期大卡 + 最近记录 chips」两段式改为**一条连续倒序轴**：
    # 原先「最近记录」条的第一项其实是前天（真正最近的两天被抽走了），标签与内容矛盾，
    # 且 14→13→[断层]→12 读起来是断的。现在一条轴、一种组件，今天/昨天只加角标。
    assert 'role="group" aria-label="按日期查看记录（由新到旧）"' in app
    assert 'class="days-strip"' in app
    assert 'day-chip-badge' in app
    assert 'const dayBadgeByDate = new Map([[today, "今天"], [yesterday, "昨天"]]);' in app
    # 连续性：所有日期（含今天/昨天）合并去重后按日期倒序排序
    assert ".sort((a, b) => (a < b ? 1 : a > b ? -1 : 0))" in app
    assert 'aria-label="${escapeHtml(`${friendlyDate(date)}，${label}`)}"' in app
    # 旧的两段式容器不应再出现在渲染出口（否则又会切回双视觉语言）
    assert 'class="quick-days"' not in app
    assert 'class="recent-days"' not in app
    # [2026-10-14] 空日文案从「这一天没有录音或事件证据」改为「这一天没有记录 · 时间轴已确认：
    # 没有事件、场景分段或录音覆盖」。旧串已被 tests/ui-truth-counts.test.mjs 明确禁止
    # （“有内容却说没有记录”的自相矛盾文案）；本用例真正要锁的是「空日必须显式说出 + 不借用其他日期」。
    assert "这一天没有记录" in app
    assert "时间轴已确认：没有事件、场景分段或录音覆盖" in app
    assert "不会借用其他日期的内容" in app


def test_day_timeline_exposes_semantic_empty_and_event_states() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    assert 'aria-labelledby="day-timeline-heading"' in app
    assert 'id="day-timeline-heading"' in app
    assert 'class="day-axis-empty" role="status"' in app
    assert 'class="day-event-node" aria-hidden="true"' in app
    assert 'class="day-event-card" aria-label="${escapeHtml(eventLabel)}"' in app
    assert ".day-event-card:focus-within" in style


def test_timeline_shows_recording_coverage_separately() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    assert '"/api/watch-coverage"' in app
    assert "async function loadWatchCoverageIntervals()" in app
    assert "return {available: false, intervals: []}" in app
    assert "JOURNAL_LOGIC.watchCoverageState(" in app
    assert "journalWatchCoverageIntervals" in app
    assert "journalWatchCoverageIntervals = watchCoverage.intervals" in app
    assert "录音覆盖数据暂不可用" in app
    assert "覆盖未刷新" in app
    assert 'endsWith("/recordings.json")' in app
    assert "groupJournalDays(entries, watchCoverageIntervals = [], watchCoverageAvailable = true)" in app
    assert "renderDayTimeline(" in app
    assert "recordingIntervals" in app
    assert "recordingCoverageForDate" in app
    assert "recordingCoverageStatus" in app
    # ⑤5号 把 recordingIntervalDates 收进 JOURNAL_LOGIC 命名空间（web/journal_logic.js:151 定义、
    # :592 导出），并给调用点加了 recordingRanges 缓存参数。断言只认「有没有把 entry 的
    # recordingIntervals 交给这个函数」，不锁死前缀与参数个数，避免命名空间/签名调整反复带崩测试。
    assert re.search(r"recordingIntervalDates\(entry\.recordingIntervals \|\| \[\]", app)
    assert 'sourceFile?.capture_source === "apple-watch"' in app
    assert "if (!events?.length && !recordingIntervals.length)" in app
    assert "if (!prepared.length && !recordingIntervals.length)" in app
    assert "day-recording-interval" in app
    assert "绿色=有录音覆盖" in app
    assert ".day-recording-interval" in css


def test_journal_loading_region_and_cross_midnight_copy_are_explicit() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    assert (
        'id="journal-dashboard" class="journal-dashboard" role="region" '
        'aria-labelledby="journal-heading" aria-busy="true"' in index
    )
    assert (
        'id="journal-load-status" class="sr-only" role="status" '
        'aria-live="polite" aria-atomic="true"' in index
    )
    assert 'setJournalLoading(true, "正在刷新日记")' in app
    assert 'setJournalLoading(false, "日记已更新")' in app
    assert "eventEndClock" in app
    assert '跨至次日${endClock ? ` ${endClock}` : ""} · 归属 ${formatMonthDay(date)}' in app
    assert ".sr-only {" in style


def test_day_axis_resolves_real_card_height_collisions() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "resolveTimelineCardCollisions" in app
    assert "getBoundingClientRect().height" in app
    assert "anchor.dataset.lane" in app
    assert "anchor.dataset.anchorY" in app
    assert "scheduleTimelineCollisionResolution(dashboard)" in app
    assert 'details.addEventListener("toggle"' in app


def test_event_viewer_supports_durable_corrections_and_rejection() -> None:
    viewer = (ROOT / "web" / "viewer.js").read_text(encoding="utf-8")
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "event-correction" in viewer
    assert "event-corrections" in viewer
    assert "编辑事件" in viewer
    assert "这不是事件" in viewer
    assert "applyEventCorrections" in app


def test_embedded_review_mutations_invalidate_parent_journal_cache() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    viewer = (ROOT / "web" / "viewer.js").read_text(encoding="utf-8")

    assert 'type: "life-logger-data-changed"' in viewer
    assert 'notifyParentDataChanged("review")' in viewer
    assert 'notifyParentDataChanged("event-correction")' in viewer
    assert 'event.data?.type === "life-logger-data-changed"' in app
    assert "inlineViewerDirty = true" in app
    assert "if (force) journalReviewCache.clear();" in app
    assert "journalReviewCache.clear();" in app
    assert "refreshAll(true).catch(() => {});" in app


def test_incorrect_transcript_uses_in_page_correction_editor() -> None:
    viewer = (ROOT / "web" / "viewer.js").read_text(encoding="utf-8")
    html = (ROOT / "web" / "viewer.html").read_text(encoding="utf-8")

    assert "corrected_text" in viewer
    assert "openCorrectionEditor" in viewer
    assert "原始 ASR 永久保留" in viewer
    assert "window.prompt" not in viewer
    assert 'id="edit-dialog"' in html
    assert 'id="edit-corrected-text"' in html


def test_multi_day_observations_require_real_multi_day_coverage() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "multiDayObservationData" in app
    assert "renderMultiDayObservations" in app
    assert "insight.dayCount < 3" in app
    assert "不会用单日数据冒充长期规律" in app
    assert "没有录音的日期不会被当成" in app


def test_optional_event_correction_endpoint_is_silent_on_version_skew() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert "context.silent" in app
    assert '{silent: true, operation: "读取可选事件修正"}' in app
    assert "REQUIRED_BACKEND_FEATURES" in app
    assert "本地服务需要重启" in app
    assert 'id="service-version-warning"' in index
    assert 'id="data-backup-download"' in index


def test_primary_navigation_exposes_current_page_and_respects_reduced_motion() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'data-view="today" type="button" aria-current="page"' in index
    assert 'button.setAttribute("aria-current", "page")' in app
    assert 'button.removeAttribute("aria-current")' in app
    assert "function preferredScrollBehavior()" in app
    assert "prefers-reduced-motion: reduce" in app
    assert "behavior: preferredScrollBehavior()" in app


def test_sidebar_service_status_reflects_runtime_health() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    style = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    smoke = (ROOT / "scripts" / "smoke_local_web.py").read_text(encoding="utf-8")

    assert 'id="sidebar-service-status" class="sidebar-foot" role="status"' in index
    assert (
        'id="sidebar-status-dot" class="status-dot checking" aria-hidden="true"'
        in index
    )
    assert 'setSidebarServiceStatus("checking", "正在连接本地服务…")' in app
    assert (
        'setSidebarServiceStatus("error", "本地服务状态未知 · 已显示安全默认值")'
        in app
    )
    assert 'needsRestart ? "warning" : "ready"' in app
    assert ".status-dot.ready" in style
    assert ".status-dot.warning" in style
    assert ".status-dot.error" in style
    assert "  .status-dot { transition: none; }" in style
    assert '"sidebar-service-status"' in smoke
    assert '"sidebar-status-dot"' in smoke


def test_main_web_actions_use_inline_live_status_instead_of_alerts() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert "alert(" not in app
    assert (
        'id="journal-action-status" class="setting-status" aria-live="polite"' in index
    )
    assert 'id="kanban-dashboard"' in index
    assert 'id="upload-status" class="status-copy" role="status" aria-live="polite" aria-atomic="true"' in index
    assert (
        'id="manual-import-surface" class="surface import-surface" aria-busy="false"'
        in index
    )
    assert (
        'id="import-live-status" class="setting-badge" role="status" aria-live="polite"'
        in index
    )
    assert "function setImportLiveStatus(state, label, detail = \"\")" in app
    assert 'status.className = `status-copy ${state || ""}`.trim()' in app
    assert 'setImportLiveStatus("ready", `已选 ${formatCount(files.length)} 个`)' in app
    assert 'setImportLiveStatus("checking", `上传 ${index + 1}/${files.length}`' in app
    assert 'setImportLiveStatus("ready", "任务已启动"' in app
    assert 'setImportLiveStatus("error", "启动失败"' in app
    assert (
        'id="start-button" class="primary" type="button" aria-busy="false" '
        'aria-describedby="upload-status"' in index
    )
    assert "function setStartButtonBusy(isBusy)" in app
    assert 'button.setAttribute("aria-busy", isBusy ? "true" : "false")' in app
    assert 'button.textContent = isBusy ? "正在创建 / 恢复任务…"' in app
    assert "setStartButtonBusy(true)" in app
    assert "setStartButtonBusy(false)" in app
    assert "请先选择有效的提醒时间。" not in app
    assert "更新待办失败：${error.message}" in app
    assert "重新启动失败：${error.message}" in app


def test_job_history_refresh_is_associated_with_live_loading_status() -> None:
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert (
        'id="jobs-refresh" class="quiet-button" type="button" '
        'aria-describedby="jobs-load-status jobs-live-status"' in index
    )
    assert (
        'id="jobs-list" class="jobs-list" role="list" '
        'aria-labelledby="jobs-heading" '
        'aria-describedby="jobs-load-status jobs-live-status" '
        'aria-busy="true"' in index
    )
    assert (
        'id="jobs-load-status" class="sr-only" role="status" '
        'aria-live="polite"' in index
    )
    assert 'id="jobs-live-status" class="setting-badge" role="status"' in index


def test_settings_tabs_are_keyboard_accessible_tabpanels() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'aria-controls="settings-panel-models"' in index
    assert 'role="tabpanel" aria-labelledby="settings-tab-models"' in index
    assert 'role="tabpanel" aria-labelledby="settings-tab-agent" hidden' in index
    assert 'tabindex="-1">接入 Agent' in index
    assert "handleSettingsTabKeydown" in app
    assert 'event.key === "Home"' in app
    assert 'event.key === "End"' in app
    assert 'button.setAttribute("tabindex", active ? "0" : "-1")' in app
    assert "panel.hidden = !active" in app


def test_legacy_event_category_aliases_are_canonicalized_in_web_views() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    viewer = (ROOT / "web" / "viewer.js").read_text(encoding="utf-8")

    assert 'diet: "food"' in app
    assert 'work: "work_communication"' in app
    assert "canonicalEventCategory" in app
    assert "categories.map(canonicalEventCategory)" in app
    assert 'diet: "food"' in viewer
    assert "canonicalViewerCategory" in viewer
    assert ".map(canonicalViewerCategory)" in viewer


def test_data_health_ui_and_semantic_event_filter_are_present() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert "refreshDataHealth" in app
    assert "/api/data-health" in app
    assert "areSemanticEventDuplicates" in app
    assert "isMeaningfulWorkspaceEvent" in app
    assert "const curatedEvents = dedupeWorkspaceEvents" in app
    viewer = (ROOT / "web" / "viewer.js").read_text(encoding="utf-8")
    assert "curateViewerEvents" in viewer
    assert "viewerEventsAreDuplicates" in viewer
    assert "viewerTimelineWorthy" in viewer
    assert 'id="data-health-status"' in index
    assert 'id="download-data-health"' in index
    assert 'id="data-health-auto-ingest"' in index
    assert "auto_ingest_missing_folder" in app
    assert "自动导入目录不存在" in app


def test_job_rows_are_keyboard_focusable_buttons() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    style = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    assert '<button class="job-row" type="button"' in app
    assert '<div class="job-row"' not in app
    assert ".job-row:focus-visible" in style
    assert "outline: 2px solid" in style


def test_passport_receiver_status_polls_only_on_import_view() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "let passportPollTimer = null" in app
    assert "function updatePassportPolling(view)" in app
    assert 'if (view !== "import") return;' in app
    assert 'activeView !== "import"' in app
    assert "refreshPassportSessions().catch(() => {})" in app
    assert "clearInterval(passportPollTimer)" in app
    assert "updatePassportPolling(view);" in app


def test_agent_connection_exposes_busy_and_live_status() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert (
        'id="agent-connect-surface" class="surface agent-connect-surface" '
        'aria-busy="false"' in index
    )
    assert 'id="agent-connect-badge" class="setting-badge" role="status"' in index
    assert 'id="agent-connect-status" class="setting-status" role="status"' in index
    assert 'surface.setAttribute("aria-busy", "true")' in app
    assert 'surface.setAttribute("aria-busy", "false")' in app
    assert 'badge.textContent = "检测中"' in app


def test_backup_capability_is_live_and_truly_disabled_until_ready() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'id="data-backup-status" role="status" aria-live="polite"' in index
    assert (
        'id="data-backup-download" class="setting-action" download '
        'aria-disabled="true" aria-describedby="data-backup-status" '
        'tabindex="-1"' in index
    )
    assert 'backup.removeAttribute("href")' in app
    assert 'backup.setAttribute("tabindex", "-1")' in app
    assert 'backup.removeAttribute("tabindex")' in app
    assert 'backup.href = "/api/backup"' in app
    assert 'aria-describedby="data-backup-status"' in index


def test_data_health_exposes_busy_and_live_status() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'id="data-health-card" class="data-health-card" aria-busy="false"' in index
    assert 'id="data-health-status" role="status" aria-live="polite"' in index
    assert 'id="data-health-badge" class="setting-badge" role="status"' in index
    assert index.count('aria-describedby="data-health-status"') == 2
    assert 'badge.textContent = "检测中"' in app
    assert 'card.setAttribute("aria-busy", "true")' in app
    assert "button.disabled = true" in app


def test_system_doctor_has_web_entry_and_endpoint() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert 'data-settings-page-target="doctor"' in index
    assert 'data-settings-page="doctor"' in index
    assert 'id="run-doctor"' in index
    assert 'aria-describedby="doctor-summary"' in index
    assert 'id="doctor-checks"' in index
    assert "refreshDoctor" in app
    assert 'api("/api/doctor"' in app
    assert 'item.ok ? "✅" : "❌"' in app
    assert (
        'id="doctor-surface" class="surface doctor-surface" aria-busy="false"' in index
    )
    assert 'id="doctor-badge" class="setting-badge" role="status"' in index
    assert 'id="doctor-summary" class="setting-status" role="status"' in index
    assert 'id="doctor-checks" class="doctor-checks" role="list"' in index
    assert 'id="doctor-jobs" class="doctor-jobs" role="list"' in index
    assert 'badge.textContent = "检查中"' in app
    assert '$("doctor-surface")?.setAttribute("aria-busy", "true")' in app


def test_agent_integration_documents_single_brief_seam_and_troubleshooting() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    assert "agentQueryExamplesText" in app
    assert "/api/daily-brief?date=2026-09-21" in app
    assert "/api/settings" in app
    assert "/api/doctor" in app
    assert "agent_brief_v1" in app
    assert "high_confidence_points" in app
    assert 'id="agent-query-examples"' in index
    assert index.count('aria-describedby="agent-connect-status"') == 3
    assert "GET /api/daily-brief?date=YYYY-MM-DD" in index
    assert "其他 API 仍可开发调试" in index


def test_pinned_memo_and_query_dispatch_are_kept_in_timeline_and_workspace() -> None:
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    viewer = (ROOT / "web" / "viewer.js").read_text(encoding="utf-8")
    logic = (ROOT / "web" / "journal_logic.js").read_text(encoding="utf-8")

    assert 'const concrete = new Set(["routine", "expense", "food", "exercise", "todo"]);' in app
    assert "event?.pinned_memo || event?.evidence?.pinned_memo" in app and "event?.query_dispatch || event?.evidence?.query_dispatch" in app
    assert "event?.evidence?.reported_at" in app
    assert "event?.evidence?.reported_at" in logic
    assert '["routine", "expense", "food", "exercise", "todo"].includes(category)' in viewer



def test_kanban_separates_local_extraction_from_openclaw_global_pool() -> None:
    """任务看板必须分区：本地抽取按日期，OpenClaw 任务池全局且只读。"""
    app = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

    # 两个分区并存，池容器在日期看板之外（日期渲染不会覆盖它）
    assert 'id="kanban-dashboard"' in index
    assert 'id="openclaw-pool"' in index
    assert index.index('id="openclaw-pool"') > index.index('id="kanban-dashboard"')
    assert "今日抽取（本地事件）" in index
    assert "所有日期" in index

    # 首屏真实请求 + 只读 + 总数 + 报错语义
    assert '"/api/openclaw/tasks"' in app
    assert "summarizeOpenClaw" in app and "openclawPoolMarkup" in app
    assert "未连接" in app and "不代表 0 条" in app
    assert "任务池读取失败" in app
    assert "本区为只读展示" in app
    assert "OPENCLAW_POOL_LIMIT" in app

    # 有独立样式，且移动端单列
    assert ".openclaw-pool" in css and ".openclaw-task" in css
    assert "@media (max-width: 620px)" in css

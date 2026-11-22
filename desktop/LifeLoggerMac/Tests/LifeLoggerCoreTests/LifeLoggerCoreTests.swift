import XCTest
@testable import LifeLoggerCore

final class LifeLoggerCoreTests: XCTestCase {
    func testDateShiftingAndBeijingFormat() {
        XCTAssertEqual(LifeLoggerAPIClient.shiftDateString("2026-11-08", byDays: -1), "2026-11-07")
        XCTAssertEqual(LifeLoggerAPIClient.shiftDateString("2026-11-08", byDays: 1), "2026-11-09")
        XCTAssertEqual(LifeLoggerAPIClient.shiftDateString("2026-11-01", byDays: -1), "2026-10-31")

        let fixedDate = Date(timeIntervalSince1970: 1_794_114_000) // 2026-11-08 in Asia/Shanghai
        let formatted = LifeLoggerAPIClient.beijingTodayString(now: fixedDate)
        XCTAssertEqual(formatted.count, 10)
    }

    func testActivityDisplayHelpers() {
        let workDictation = LLActivity(
            activityId: "act_work_1",
            title: "优化瀑布流布局",
            lifecycle: "live",
            category: "工作·项目",
            speechTarget: "Codex",
            spokenAt: "2026-11-08T14:25:10.123000+08:00",
            inputContext: LLInputContext(appLabel: "Cursor", targetBundle: "com.todesktop.230313mzl4w4u92", kind: "dictation", workApp: true)
        )
        XCTAssertEqual(workDictation.displayTimeText, "14:25")
        XCTAssertEqual(workDictation.displayHour, 14)
        XCTAssertEqual(workDictation.displayAppBadge, "语音输入到 Cursor")
        XCTAssertEqual(workDictation.displaySpeechBadge, "对Codex")

        let frontAppAct = LLActivity(
            activityId: "act_live_2",
            title: "录音中疑似：日常记录",
            lifecycle: "live",
            category: nil,
            speechTarget: "未知",
            spokenAt: "2026-11-07T18:30:00Z",
            inputContext: LLInputContext(appLabel: "Alma", kind: "front_app", workApp: false)
        )
        XCTAssertEqual(frontAppAct.displayTitle, "日常记录")
        XCTAssertEqual(frontAppAct.displayTimeText, "02:30")
        XCTAssertEqual(frontAppAct.displayHour, 2)
        XCTAssertEqual(frontAppAct.displayAppBadge, "前台 Alma")
        XCTAssertNil(frontAppAct.displaySpeechBadge)
    }

    @MainActor
    func testNutritionLedgerRealPayloadDecodingAndFormatting() throws {
        let json = """
        {
          "date": "2026-11-07",
          "nutrition_ledger": {
            "records": [
              {
                "event_id": "activity:2026-11-07:act_store_order_chaofen_01:it_store_03",
                "item": "炒河粉",
                "title": "要求自己的炒河粉少放油，并确认自己那份是微辣",
                "weight_g": 300,
                "calories_kcal": null,
                "nutrients": {"carbohydrates_g": null, "protein_g": null, "fat_g": null, "sodium_mg": null},
                "record_type": "consumed",
                "is_consumed": true,
                "meal": "dinner",
                "quote": "我的炒河粉少放点油啊",
                "portion_estimate_candidate": {
                  "grams": 300,
                  "calories_kcal": 1077.0,
                  "nutrients": {"carbohydrates_g": 237.6, "protein_g": 23.1, "fat_g": 4.5, "sodium_mg": 25.0}
                }
              }
            ],
            "record_count": 2,
            "consumed_record_count": 1,
            "unweighed_candidate_kcal": 1077.0,
            "totals": {
              "calories_kcal": null,
              "carbohydrates_g": null,
              "protein_g": null,
              "fat_g": null,
              "sodium_mg": null
            }
          }
        }
        """
        let decoded = try JSONDecoder().decode(LLDailyResponse.self, from: Data(json.utf8))
        let ledger = try XCTUnwrap(decoded.nutritionLedger)
        XCTAssertEqual(ledger.recordCount, 2)
        XCTAssertEqual(ledger.consumedRecordCount, 1)
        XCTAssertEqual(ledger.unweighedCandidateKcal ?? 0, 1077.0, accuracy: 0.01)

        let record = try XCTUnwrap(ledger.records?.first)
        XCTAssertEqual(record.item, "炒河粉")
        XCTAssertEqual(record.displayName, "炒河粉")
        XCTAssertEqual(record.weightG, 300)
        XCTAssertEqual(record.meal, "dinner")
        XCTAssertEqual(record.isConsumed, true)
        XCTAssertNil(record.caloriesKcal)
        XCTAssertEqual(record.effectiveCaloriesKcal ?? 0, 1077.0, accuracy: 0.01)
        XCTAssertEqual(record.portionEstimateCandidate?.grams, 300)
        XCTAssertEqual(record.portionEstimateCandidate?.nutrients?.carbohydratesG ?? 0, 237.6, accuracy: 0.01)
        XCTAssertEqual(record.portionEstimateCandidate?.nutrients?.proteinG ?? 0, 23.1, accuracy: 0.01)
        XCTAssertEqual(record.portionEstimateCandidate?.nutrients?.fatG ?? 0, 4.5, accuracy: 0.01)

        let store = LifeLoggerStore(initialDate: "2026-11-07")
        store.daily = decoded
        XCTAssertEqual(store.caloriesText, "未知")
        XCTAssertEqual(store.macrosText, "未知")
    }

    @MainActor
    func testSleepAndSummaryFormatting() {
        let store = LifeLoggerStore(initialDate: "2026-11-07")
        XCTAssertEqual(store.sleepText, "还没有数据")

        store.healthSummary = LLHealthSummary(
            date: "2026-11-07",
            avgHeartRate: 76,
            minHeartRate: 58,
            maxHeartRate: 112,
            totalSteps: 1572,
            sleepMinutes: 384,
            deepSleepMinutes: 56
        )
        store.daily = LLDailyResponse(
            date: "2026-11-07",
            expenseLedger: LLExpenseLedger(totalAmount: 1.8, pendingCount: 0, transactionCount: 1)
        )

        XCTAssertEqual(store.sleepText, "6小时24分（深睡 56分）")
        XCTAssertEqual(store.stepsText, "1572 步")
        XCTAssertEqual(store.totalSpendText, "¥1.8")
        XCTAssertEqual(store.menuBarSummaryText, "¥1.8")
        XCTAssertEqual(store.heartRateText, "76 bpm（58–112）")

        store.daily = LLDailyResponse(
            date: "2026-11-08",
            expenseLedger: LLExpenseLedger(totalAmount: 11.8, pendingCount: 0, transactionCount: 2),
            nutritionLedger: LLNutritionLedger(
                totals: LLNutritionTotals(caloriesKcal: 1465.0),
                hasRecords: true
            )
        )
        XCTAssertEqual(store.menuBarSummaryText, "¥11.8 · 1465 kcal")
    }

    func testRealDataFetch2026_11_08And2026_11_07() async throws {
        let client = LifeLoggerAPIClient()
        XCTAssertNotNil(client.accessToken, "Expected mobile_access_token to load from secrets.json")

        // Verify 2026-11-08 timeline + daily expense (可乐 1.8 yuan)
        let timeline08 = try await client.fetchTimeline(date: "2026-11-08")
        XCTAssertEqual(timeline08.date, "2026-11-08")
        XCTAssertFalse(timeline08.activities.isEmpty)

        let daily08 = try await client.fetchDaily(date: "2026-11-08")
        XCTAssertEqual(daily08.date, "2026-11-08")
        let totalAmount = daily08.expenseLedger?.totalAmount ?? 0
        XCTAssertGreaterThanOrEqual(totalAmount, 1.8)
        let hasCola = (daily08.expenseLedger?.transactions ?? []).contains { tx in
            (tx.item?.contains("可乐") == true) || (tx.title?.contains("可乐") == true) || (tx.quote?.contains("可乐") == true)
        }
        XCTAssertTrue(hasCola, "Expected 2026-11-08 expense ledger to include 可乐 1.8 yuan")

        // Verify 2026-11-07 nutrition ledger & health summary (384 mins sleep, 56 mins deep sleep)
        let daily07 = try await client.fetchDaily(date: "2026-11-07")
        XCTAssertNotNil(daily07.nutritionLedger?.records)
        XCTAssertFalse(daily07.nutritionLedger?.records?.isEmpty ?? true)
        let health07 = try await client.fetchHealthSummary(date: "2026-11-07")
        XCTAssertEqual(health07.sleepMinutes, 384)
        XCTAssertEqual(health07.deepSleepMinutes, 56)
        XCTAssertEqual(LifeLoggerStore.formatSleep(sleepMinutes: health07.sleepMinutes, deepSleepMinutes: health07.deepSleepMinutes), "6小时24分（深睡 56分）")
    }

    @MainActor
    func testRealCapturedFixtures2026_11_09AndMenuBarFormatting() throws {
        // Real response structure captured via curl from 127.0.0.1:8765 on 2026-11-09 (personal text replaced with placeholders, zero token)
        let daily20261109JSON = """
        {
          "date": "2026-11-09",
          "expense_ledger": {
            "policy_version": "expense-policy-v2",
            "total_amount": 245.1,
            "currency": "CNY",
            "transaction_count": 5,
            "pending_count": 0,
            "has_records": true,
            "transactions": [
              {"event_id": "tx_1", "clock": "00:25", "title": "<ITEM_1>", "item": "<ITEM_1>", "amount": 105.0, "currency": "CNY", "payment_context": "电子支付", "quote": "<PLACEHOLDER_QUOTE>"},
              {"event_id": "tx_2", "clock": "00:25", "title": "<ITEM_2>", "item": "<ITEM_2>", "amount": 51.0, "currency": "CNY", "payment_context": "电子支付", "quote": "<PLACEHOLDER_QUOTE>"},
              {"event_id": "tx_3", "clock": "00:25", "title": "<ITEM_3>", "item": "<ITEM_3>", "amount": 56.9, "currency": "CNY", "payment_context": "电子支付", "quote": "<PLACEHOLDER_QUOTE>"},
              {"event_id": "tx_4", "clock": "00:25", "title": "<ITEM_4>", "item": "<ITEM_4>", "amount": 5.2, "currency": "CNY", "payment_context": "电子支付", "quote": "<PLACEHOLDER_QUOTE>"},
              {"event_id": "tx_5", "clock": "02:08", "title": "<ITEM_5>", "item": "<ITEM_5>", "amount": 27.0, "currency": "CNY", "payment_context": "电子支付", "quote": "<PLACEHOLDER_QUOTE>"}
            ],
            "pending_items": []
          },
          "nutrition_ledger": {
            "meals": {"breakfast": [], "lunch": [], "dinner": [], "snacks": [], "supplements": []},
            "meal_totals": {
              "breakfast": {"calories_kcal": null, "carbohydrates_g": null, "protein_g": null, "fat_g": null, "sodium_mg": null},
              "lunch": {"calories_kcal": null, "carbohydrates_g": null, "protein_g": null, "fat_g": null, "sodium_mg": null},
              "dinner": {"calories_kcal": null, "carbohydrates_g": null, "protein_g": null, "fat_g": null, "sodium_mg": null},
              "snacks": {"calories_kcal": null, "carbohydrates_g": null, "protein_g": null, "fat_g": null, "sodium_mg": null},
              "unknown": {"calories_kcal": null, "carbohydrates_g": null, "protein_g": null, "fat_g": null, "sodium_mg": null},
              "supplements": {"calories_kcal": null, "carbohydrates_g": null, "protein_g": null, "fat_g": null, "sodium_mg": null}
            },
            "food_records": [],
            "record_count": 0,
            "consumed_record_count": 0,
            "unweighed_candidate_kcal": 0,
            "totals": {"calories_kcal": null, "carbohydrates_g": null, "protein_g": null, "fat_g": null, "sodium_mg": null},
            "has_records": false
          },
          "fitness_ledger": {
            "totals": {"workout_count": 0, "session_count": 0},
            "has_records": false
          },
          "smoking_ledger": {
            "total_count": 0,
            "has_records": false
          }
        }
        """

        let health20261109JSON = """
        {
          "ok": true,
          "summary": {
            "date": "2026-11-09",
            "avg_heart_rate": 68.5,
            "min_heart_rate": 53.0,
            "max_heart_rate": 83.0,
            "total_steps": 128,
            "sleep_minutes": 0,
            "deep_sleep_minutes": 0,
            "stress_spikes_count": 0
          },
          "latest_heart_rate": {
            "timestamp": "2026-11-08T20:44:50.391000+00:00",
            "bpm": 53.0,
            "source": "watch-segment-capture",
            "context": "active"
          },
          "device": {
            "level": "alert",
            "silent_seconds": 47392.9,
            "silent_hours": 13.2,
            "last_seen": "2026-11-09T05:16:02.668753+08:00",
            "last_seen_clock": "05:16",
            "pending_completion_count": 15,
            "message": "<PLACEHOLDER_DEVICE_MESSAGE>",
            "recovered_at": null,
            "recovered_devices": []
          }
        }
        """

        let dailyDecoded = try JSONDecoder().decode(LLDailyResponse.self, from: Data(daily20261109JSON.utf8))
        let healthResponseDecoded = try JSONDecoder().decode(LLHealthSummaryResponse.self, from: Data(health20261109JSON.utf8))
        let healthDecoded = try XCTUnwrap(healthResponseDecoded.summary)

        let store = LifeLoggerStore(initialDate: "2026-11-09")
        store.daily = dailyDecoded
        store.healthSummary = healthDecoded

        let expectedSpend = LifeLoggerStore.formatCurrencyAmount(try XCTUnwrap(dailyDecoded.expenseLedger?.totalAmount))
        XCTAssertEqual(expectedSpend, "¥245.1")
        XCTAssertEqual(store.totalSpendText, expectedSpend)
        XCTAssertNil(store.caloriesValueText)
        XCTAssertEqual(store.caloriesText, "未知")
        // Menu bar title shows only spend when calories are absent in /api/daily
        XCTAssertEqual(store.menuBarSummaryText, expectedSpend)
        XCTAssertEqual(store.stepsText, "\(try XCTUnwrap(healthDecoded.totalSteps)) 步")
        XCTAssertEqual(store.sleepText, LifeLoggerStore.formatSleep(sleepMinutes: healthDecoded.sleepMinutes, deepSleepMinutes: healthDecoded.deepSleepMinutes))
        XCTAssertNil(store.lastRecordingTimeText)

        // Also verify when /api/daily provides totals.calories_kcal (as on 2026-11-08)
        let daily20261108JSON = """
        {
          "date": "2026-11-08",
          "expense_ledger": {
            "total_amount": 11.8,
            "transaction_count": 2,
            "pending_count": 0
          },
          "nutrition_ledger": {
            "totals": {"calories_kcal": 1465.0, "carbohydrates_g": 181.5, "protein_g": 126.0, "fat_g": 26.1, "sodium_mg": 2745.0},
            "has_records": true
          }
        }
        """
        let daily08Decoded = try JSONDecoder().decode(LLDailyResponse.self, from: Data(daily20261108JSON.utf8))
        store.daily = daily08Decoded
        let expectedSpend08 = LifeLoggerStore.formatCurrencyAmount(try XCTUnwrap(daily08Decoded.expenseLedger?.totalAmount))
        let expectedKcal08 = "\(Int(try XCTUnwrap(daily08Decoded.nutritionLedger?.totals?.caloriesKcal).rounded())) kcal"
        XCTAssertEqual(store.menuBarSummaryText, "\(expectedSpend08) · \(expectedKcal08)")
        XCTAssertEqual(store.menuBarSummaryText, "¥11.8 · 1465 kcal")
    }

    @MainActor
    func testUnreachableServiceShowsErrorRetainsLastDataAndReadsNoLocalFiles() async {
        final class FailingURLProtocol: URLProtocol {
            override class func canInit(with request: URLRequest) -> Bool { true }
            override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
            override func startLoading() {
                client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
            }
            override func stopLoading() {}
        }

        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [FailingURLProtocol.self]
        let failingSession = URLSession(configuration: config)

        let unreachableClient = LifeLoggerAPIClient(
            baseURL: URL(string: "http://127.0.0.1:9")!,
            accessToken: "placeholder-test-token",
            session: failingSession
        )
        let store = LifeLoggerStore(client: unreachableClient, initialDate: "2026-11-08")

        // 1. First refresh when service has never succeeded: must not crash, must not read local files, must show "Life Logger 服务没开"
        await store.refresh()
        XCTAssertNil(store.daily, "Must not build fallback daily from local files")
        XCTAssertNil(store.timeline, "Must not read local activity-timeline json files")
        XCTAssertNil(store.healthSummary, "Must not read local health_sleep.json / health_steps.json files")
        XCTAssertEqual(store.errorMessage, "Life Logger 服务没开")
        XCTAssertEqual(store.menuBarSummaryText, "Life Logger 服务没开")

        // 2. Simulate a previous success at 18:25, then service becomes unreachable: must retain previous data and show "服务连不上，显示的是 18:25 的数据"
        store.daily = LLDailyResponse(
            date: "2026-11-08",
            expenseLedger: LLExpenseLedger(totalAmount: 11.8, pendingCount: 0, transactionCount: 2),
            nutritionLedger: LLNutritionLedger(totals: LLNutritionTotals(caloriesKcal: 1465.0), hasRecords: true)
        )
        store.healthSummary = LLHealthSummary(date: "2026-11-08", totalSteps: 1402, sleepMinutes: 0, deepSleepMinutes: 0)
        store.lastSuccessHHMM = "18:25"
        store.errorMessage = nil

        await store.refresh()
        XCTAssertEqual(store.errorMessage, "服务连不上，显示的是 18:25 的数据")
        XCTAssertEqual(store.totalSpendText, "¥11.8")
        XCTAssertEqual(store.caloriesText, "1465 kcal")
        XCTAssertEqual(store.stepsText, "1402 步")
        XCTAssertEqual(store.menuBarSummaryText, "¥11.8 · 1465 kcal")
    }

    func testSourcesContainNoForbiddenFallbackStringsOrReduce() throws {
        let testFileURL = URL(fileURLWithPath: #filePath)
        let packageRootURL = testFileURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let sourcesURL = packageRootURL.appendingPathComponent("Sources")

        let enumerator = try XCTUnwrap(FileManager.default.enumerator(at: sourcesURL, includingPropertiesForKeys: nil))
        var swiftFiles: [URL] = []
        for case let fileURL as URL in enumerator where fileURL.pathExtension == "swift" {
            swiftFiles.append(fileURL)
        }
        XCTAssertFalse(swiftFiles.isEmpty, "Expected to find Swift source files under Sources/")

        let forbiddenSubstrings = [
            "output/local-web/activity-timeline",
            "health_sleep.json",
            "health_steps.json",
            ".reduce("
        ]

        for fileURL in swiftFiles {
            let content = try String(contentsOf: fileURL, encoding: .utf8)
            for forbidden in forbiddenSubstrings {
                XCTAssertFalse(
                    content.contains(forbidden),
                    "Forbidden substring '\(forbidden)' found in \(fileURL.lastPathComponent)"
                )
            }
        }
    }
}

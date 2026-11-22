// PassportHabitSyncTests.swift
//
// 习惯帧（0x04）协议与映射的单测。全部是纯函数，不碰 BLE、不碰网络。

import XCTest
@testable import AirPassportReceiver

final class PassportHabitSyncTests: XCTestCase {

    // MARK: - 清空帧

    func testClearFrameLayout() {
        let frame = PassportHabitProtocol.clearFrame()
        XCTAssertEqual(frame.count, 5)
        XCTAssertEqual(frame[frame.startIndex + 0], 0x04, "首字节必须是 0x04")
        XCTAssertEqual(frame[frame.startIndex + 1], 0, "index = 0")
        XCTAssertEqual(frame[frame.startIndex + 2], 0, "total = 0 表示清空")
        XCTAssertEqual(frame[frame.startIndex + 3], 0, "flags = 0")
        XCTAssertEqual(frame[frame.startIndex + 4], 0, "title_len = 0")
    }

    // MARK: - 编码

    func testEncodeFrameChecked() {
        let frame = PassportHabitProtocol.encodeFrame(index: 1, total: 3, checked: true, title: "冥想")
        XCTAssertEqual(frame[frame.startIndex + 0], 0x04)
        XCTAssertEqual(frame[frame.startIndex + 1], 1)
        XCTAssertEqual(frame[frame.startIndex + 2], 3)
        XCTAssertEqual(frame[frame.startIndex + 3], 0x01, "bit0 = 1 表示已打卡")
        XCTAssertEqual(frame[frame.startIndex + 4], 6, "「冥想」= 6 字节")
        XCTAssertEqual(frame.count, 5 + 6, "帧长必须 == headerSize + title_len")
    }

    func testEncodeFrameNotChecked() {
        let frame = PassportHabitProtocol.encodeFrame(index: 0, total: 1, checked: false, title: "早睡")
        XCTAssertEqual(frame[frame.startIndex + 3], 0x00, "未打卡 = flags 0")
    }

    func testEncodeFrameReservedBitsAreZero() {
        // 只有 bit0 被使用，其余保留位必须写 0（固件解析方要忽略它们）
        let checked = PassportHabitProtocol.encodeFrame(index: 0, total: 1, checked: true, title: "x")
        let unchecked = PassportHabitProtocol.encodeFrame(index: 0, total: 1, checked: false, title: "x")
        XCTAssertEqual(checked[checked.startIndex + 3] & 0xFE, 0, "保留位必须为 0")
        XCTAssertEqual(unchecked[unchecked.startIndex + 3], 0)
    }

    func testEncodeFrameIndexAndTotalClamp() {
        // 越界值在编码处收敛到 UInt8，不给调用方埋坑
        let frame = PassportHabitProtocol.encodeFrame(index: 999, total: -5, checked: false, title: "a")
        XCTAssertEqual(frame[frame.startIndex + 1], 255)
        XCTAssertEqual(frame[frame.startIndex + 2], 0)
    }

    // MARK: - 截断

    func testShortTitleUntouched() {
        let data = PassportHabitProtocol.truncatedTitleData("喝水", maxBytes: 60)
        XCTAssertEqual(String(data: data, encoding: .utf8), "喝水")
    }

    func testLongTitleTruncatedWithEllipsis() {
        let long = String(repeating: "习", count: 30)  // 90 字节
        let data = PassportHabitProtocol.truncatedTitleData(long, maxBytes: 60)
        XCTAssertLessThanOrEqual(data.count, 60, "绝不能超上限")
        let s = String(data: data, encoding: .utf8)
        XCTAssertNotNil(s, "必须是合法 UTF-8（不能在字符中间切断）")
        XCTAssertTrue(s!.hasSuffix("…"), "超长要补省略号")
    }

    func testTruncationNeverSplitsMultibyte() {
        // 每个「习」3 字节；用各种上限试探，结果必须永远能解回合法 UTF-8
        let text = String(repeating: "习", count: 10)
        for limit in 1...40 {
            let data = PassportHabitProtocol.truncatedTitleData(text, maxBytes: limit)
            XCTAssertLessThanOrEqual(data.count, limit, "limit=\(limit) 时超限了")
            XCTAssertNotNil(String(data: data, encoding: .utf8), "limit=\(limit) 时切断了多字节序列")
        }
    }

    func testTinyLimitFallsBackWithoutEllipsis() {
        // 上限连一个字符 + 省略号都放不下 → 整字截断、不补省略号（补了会超限）
        let data = PassportHabitProtocol.truncatedTitleData("习习习", maxBytes: 4)
        XCTAssertLessThanOrEqual(data.count, 4)
        XCTAssertFalse(String(data: data, encoding: .utf8)!.hasSuffix("…"))
    }

    func testEmojiTruncationStaysValid() {
        let text = "打卡🏃今天也要加油"
        for limit in 1...40 {
            let data = PassportHabitProtocol.truncatedTitleData(text, maxBytes: limit)
            XCTAssertNotNil(String(data: data, encoding: .utf8), "limit=\(limit) 切坏了 emoji")
        }
    }

    // MARK: - pushPlan

    func testPushPlanClearsThenWritesItems() {
        let items = [PassportHabitItem(title: "冥想", checked: true, streak: 3),
                     PassportHabitItem(title: "早睡", checked: false, streak: 0)]
        let plan = PassportHabitProtocol.pushPlan(items: items)
        XCTAssertEqual(plan.habitFrames.count, 3, "1 条清空帧 + 2 条习惯帧")
        XCTAssertEqual(plan.itemCount, 2)
        // 第 0 条必须是清空帧：否则设备会残留上一轮的第 3/4 条
        XCTAssertEqual(plan.habitFrames[0], PassportHabitProtocol.clearFrame())
        XCTAssertEqual(plan.habitFrames[1][plan.habitFrames[1].startIndex + 3], 0x01)
        XCTAssertEqual(plan.habitFrames[2][plan.habitFrames[2].startIndex + 3], 0x00)
    }

    func testPushPlanCapsAtFour() {
        let items = (0..<10).map { PassportHabitItem(title: "习惯\($0)", checked: false) }
        let plan = PassportHabitProtocol.pushPlan(items: items)
        XCTAssertEqual(plan.itemCount, 4, "设备只有 4 个槽位")
        XCTAssertEqual(plan.habitFrames.count, 5)
    }

    func testPushPlanEmptyStillClears() {
        // 没有任何习惯时，也必须发一条清空帧把设备上的旧列表抹掉
        let plan = PassportHabitProtocol.pushPlan(items: [])
        XCTAssertEqual(plan.habitFrames.count, 1)
        XCTAssertEqual(plan.itemCount, 0)
        XCTAssertEqual(plan.habitFrames[0], PassportHabitProtocol.clearFrame())
    }

    func testHabitFramesNeverMistakenForOtherFrameTypes() {
        // 固件靠首字节区分：4 字节校时 / 0x02 待办 / 0x03 状态 / 0x04 习惯。
        // 习惯帧必须长度 >= 5 且首字节 0x04，否则会被当成校时帧或其它。
        let plan = PassportHabitProtocol.pushPlan(items: [PassportHabitItem(title: "x", checked: true)])
        for frame in plan.habitFrames {
            XCTAssertGreaterThanOrEqual(frame.count, 5, "长度必须 >= 5")
            XCTAssertEqual(frame[frame.startIndex + 0], 0x04)
            XCTAssertNotEqual(frame[frame.startIndex + 0], 0x02)
            XCTAssertNotEqual(frame[frame.startIndex + 0], 0x03)
        }
    }

    // MARK: - 映射（服务端返回 → items）

    private func json(_ obj: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: obj)
    }

    func testMapFromBadgeSlots() throws {
        let data = try json([
            "ok": true,
            "schema_version": "local-habits-v1",
            "date": "2026-10-13",
            "total": 2,
            "badge_slots": [
                ["index": 1, "total": 2, "flags": 1, "title": "冥想", "checked_today": true, "streak": 3],
                ["index": 2, "total": 2, "flags": 0, "title": "早睡", "checked_today": false, "streak": 0],
            ],
        ])
        let items = try PassportHabitMapper.items(fromHabitsResponse: data)
        XCTAssertEqual(items.count, 2)
        XCTAssertEqual(items[0].title, "冥想")
        XCTAssertTrue(items[0].checked, "flags bit0=1 应映射为已打卡")
        XCTAssertEqual(items[0].streak, 3)
        XCTAssertFalse(items[1].checked)
    }

    func testMapIgnoresReservedFlagBits() throws {
        // 固件约定保留位必须被忽略：flags=0x03（bit0+bit1）仍应视为「已打卡」
        let data = try json(["badge_slots": [["title": "冥想", "flags": 3]]])
        let items = try PassportHabitMapper.items(fromHabitsResponse: data)
        XCTAssertEqual(items.count, 1)
        XCTAssertTrue(items[0].checked)
    }

    func testMapFallsBackToHabitsArray() throws {
        let data = try json([
            "habits": [
                ["title": "冥想", "status": "active", "checked_today": true, "streak": 2],
                ["title": "已删的习惯", "status": "dismissed", "checked_today": false],
            ],
        ])
        let items = try PassportHabitMapper.items(fromHabitsResponse: data)
        XCTAssertEqual(items.count, 1, "dismissed 的不能下发")
        XCTAssertEqual(items[0].title, "冥想")
    }

    func testMapSkipsEmptyTitles() throws {
        let data = try json(["badge_slots": [["title": "   ", "flags": 0], ["title": "有效", "flags": 0]]])
        let items = try PassportHabitMapper.items(fromHabitsResponse: data)
        XCTAssertEqual(items.count, 1)
        XCTAssertEqual(items[0].title, "有效")
    }

    func testMapEmptyPayloadReturnsEmpty() throws {
        XCTAssertEqual(try PassportHabitMapper.items(fromHabitsResponse: try json([:])).count, 0)
        XCTAssertEqual(try PassportHabitMapper.items(fromHabitsResponse: try json(["badge_slots": []])).count, 0)
    }

    // MARK: - 端到端（真实服务端 payload → 字节级帧）

    /// 用 2026-10-13 03:48 服务端 `/api/habits` 的**真实返回**跑全程：
    /// payload → items → pushPlan → 逐帧核对字节。
    /// 这份 payload 是与固件对接的唯一事实来源，写死在这里当契约测试。
    func testRealServerPayloadProducesFrames() throws {
        let raw = """
        {"ok":true,"schema_version":"local-habits-v1","backend":"local","date":"2026-10-13",
         "total":3,"checked_count":1,"habits":[],"badge_slots":[
          {"index":1,"total":3,"flags":0,"title":"早睡","habit_key":"habit-b38424f8566b","checked_today":false,"streak":0},
          {"index":2,"total":3,"flags":1,"title":"英语学习","habit_key":"habit-f1387b6db583","checked_today":true,"streak":1},
          {"index":3,"total":3,"flags":0,"title":"不抽烟","habit_key":"habit-aa7dddc9d547","checked_today":false,"streak":0}]}
        """
        let items = try PassportHabitMapper.items(fromHabitsResponse: Data(raw.utf8))
        XCTAssertEqual(items.map(\.title), ["早睡", "英语学习", "不抽烟"])
        XCTAssertEqual(items.map(\.checked), [false, true, false])
        XCTAssertEqual(items[1].streak, 1)

        let plan = PassportHabitProtocol.pushPlan(items: items)
        XCTAssertEqual(plan.itemCount, 3)
        XCTAssertEqual(plan.habitFrames.count, 4, "1 条清空帧 + 3 条习惯帧")

        // 清空帧
        XCTAssertEqual(Array(plan.habitFrames[0].prefix(5)), [0x04, 0, 0, 0, 0])

        // 逐条：index / flags
        let idx = { (i: Int) in plan.habitFrames[i][plan.habitFrames[i].startIndex + 1] }
        let flags = { (i: Int) in plan.habitFrames[i][plan.habitFrames[i].startIndex + 3] }
        let total = { (i: Int) in plan.habitFrames[i][plan.habitFrames[i].startIndex + 2] }
        let len = { (i: Int) in plan.habitFrames[i][plan.habitFrames[i].startIndex + 4] }

        XCTAssertEqual(idx(1), 0); XCTAssertEqual(flags(1), 0x00)   // 早睡 未打卡
        XCTAssertEqual(idx(2), 1); XCTAssertEqual(flags(2), 0x01)   // 英语学习 已打卡
        XCTAssertEqual(idx(3), 2); XCTAssertEqual(flags(3), 0x00)   // 不抽烟 未打卡
        for i in 1...3 { XCTAssertEqual(total(i), 3, "每条 total 都写本轮总数") }

        // 标题字节数：「英语学习」4 汉字 = 12 字节
        XCTAssertEqual(len(2), 12)
        XCTAssertEqual(plan.habitFrames[2].count, 5 + 12)
        // 字节层面复核标题真的是 UTF-8「英语学习」
        let titleBytes = plan.habitFrames[2].dropFirst(5)
        XCTAssertEqual(String(data: Data(titleBytes), encoding: .utf8), "英语学习")
    }

    /// 服务端真返回一个习惯都没有时，仍必须发清空帧（把设备上残留的清掉）。
    func testRealEmptyServerPayloadStillClearsDevice() throws {
        let raw = """
        {"ok":true,"schema_version":"local-habits-v1","date":"2026-10-13","total":0,"checked_count":0,
         "habits":[],"badge_slots":[]}
        """
        let items = try PassportHabitMapper.items(fromHabitsResponse: Data(raw.utf8))
        XCTAssertTrue(items.isEmpty)
        let plan = PassportHabitProtocol.pushPlan(items: items)
        XCTAssertEqual(plan.habitFrames.count, 1)
        XCTAssertEqual(plan.itemCount, 0)
    }
}

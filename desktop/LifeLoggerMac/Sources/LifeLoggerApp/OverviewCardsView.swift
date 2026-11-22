import LifeLoggerCore
import SwiftUI

public struct OverviewCardsView: View {
    @ObservedObject var store: LifeLoggerStore

    public init(store: LifeLoggerStore) {
        self.store = store
    }

    public var body: some View {
        HStack(alignment: .top, spacing: 14) {
            expenseCard
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            nutritionCard
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            healthCard
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        }
        .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: - 1. 消费卡片

    private var expenseCard: some View {
        let ledger = store.daily?.expenseLedger
        let transactions = ledger?.transactions ?? []
        let pendingCount = ledger?.pendingCount ?? (ledger?.pendingItems?.count ?? 0)

        return OverviewCardSurface(
            title: "消费",
            icon: "yensign.circle.fill",
            accentColor: .orange
        ) {
            if pendingCount > 0 {
                Text("\(pendingCount) 待确认")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.orange)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(Color.orange.opacity(0.14), in: Capsule())
            } else if !transactions.isEmpty {
                Text("\(transactions.count) 笔")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
            }
        } content: {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(store.totalSpendText)
                        .font(.system(size: 26, weight: .bold, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(.primary)
                    Text("今日总计")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                }

                Divider()
                    .opacity(0.5)

                if transactions.isEmpty {
                    let pendingItems = ledger?.pendingItems ?? []
                    if pendingItems.isEmpty {
                        emptyPlaceholder("今日暂无已记录消费")
                    } else {
                        VStack(alignment: .leading, spacing: 5) {
                            ForEach(pendingItems.prefix(4)) { pending in
                                HStack(spacing: 6) {
                                    if let clock = pending.clock, !clock.isEmpty {
                                        Text(clock)
                                            .font(.system(size: 11, design: .monospaced))
                                            .foregroundStyle(.secondary)
                                    }
                                    Text(pending.item ?? pending.title ?? "待确认消费")
                                        .font(.system(size: 12, weight: .medium))
                                        .lineLimit(1)
                                    Spacer(minLength: 4)
                                    Text(pending.pendingReason ?? "金额待补")
                                        .font(.system(size: 11, weight: .medium))
                                        .foregroundStyle(.orange)
                                }
                            }
                        }
                    }
                } else {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(transactions.prefix(5)) { tx in
                            HStack(spacing: 6) {
                                if let clock = tx.clock, !clock.isEmpty {
                                    Text(clock)
                                        .font(.system(size: 11, design: .monospaced))
                                        .monospacedDigit()
                                        .foregroundStyle(.secondary)
                                }
                                Text(tx.item ?? tx.title ?? "消费项")
                                    .font(.system(size: 12, weight: .medium))
                                    .foregroundStyle(.primary)
                                    .lineLimit(1)
                                Spacer(minLength: 6)
                                Text(formatAmount(tx.amount))
                                    .font(.system(size: 12, weight: .semibold, design: .monospaced))
                                    .monospacedDigit()
                                    .foregroundStyle(.primary)
                            }
                        }
                        if transactions.count > 5 {
                            Text("还有 \(transactions.count - 5) 笔消费记录…")
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    // MARK: - 2. 饮食卡片

    private var nutritionCard: some View {
        let mealGroups = orderedMealGroups(from: store.daily?.nutritionLedger)

        return OverviewCardSurface(
            title: "饮食",
            icon: "fork.knife.circle.fill",
            accentColor: .green
        ) {
            Text(store.macrosText)
                .font(.system(size: 11, weight: .medium, design: .monospaced))
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .lineLimit(1)
        } content: {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(store.caloriesText)
                        .font(.system(size: 26, weight: .bold, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(.primary)
                    Text("热量摄入")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                }

                Divider()
                    .opacity(0.5)

                if mealGroups.isEmpty {
                    emptyPlaceholder("今日暂无饮食记录")
                } else {
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(mealGroups, id: \.key) { group in
                            HStack(alignment: .top, spacing: 8) {
                                Text(group.label)
                                    .font(.system(size: 11, weight: .semibold))
                                    .foregroundStyle(.green)
                                    .padding(.horizontal, 6)
                                    .padding(.vertical, 2)
                                    .background(Color.green.opacity(0.12), in: RoundedRectangle(cornerRadius: 4))

                                Text(group.itemsSummary)
                                    .font(.system(size: 12, weight: .medium))
                                    .foregroundStyle(.primary)
                                    .lineLimit(2)

                                Spacer(minLength: 4)

                                if let totalKcal = group.totalKcal {
                                    Text("\(Int(totalKcal.rounded())) kcal")
                                        .font(.system(size: 11, weight: .medium, design: .monospaced))
                                        .monospacedDigit()
                                        .foregroundStyle(.secondary)
                                } else {
                                    Text("未知")
                                        .font(.system(size: 11))
                                        .foregroundStyle(.tertiary)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    // MARK: - 3. 运动 / 健康卡片

    private var healthCard: some View {
        OverviewCardSurface(
            title: "运动 / 健康",
            icon: "heart.circle.fill",
            accentColor: .pink
        ) {
            if let spikes = store.healthSummary?.stressSpikesCount, spikes > 0 {
                Text("压力峰值 \(spikes)")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.pink)
            } else {
                Text("生理指标")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
            }
        } content: {
            VStack(alignment: .leading, spacing: 10) {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(store.stepsText)
                        .font(.system(size: 26, weight: .bold, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(.primary)
                    Text("今日步数")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                }

                Divider()
                    .opacity(0.5)

                VStack(alignment: .leading, spacing: 6) {
                    healthMetricRow(
                        icon: "waveform.path.ecg",
                        color: .red,
                        label: "心率",
                        value: store.heartRateText
                    )
                    healthMetricRow(
                        icon: "bed.double.fill",
                        color: .indigo,
                        label: "睡眠",
                        value: store.sleepText
                    )
                    healthMetricRow(
                        icon: "figure.run",
                        color: .teal,
                        label: "训练",
                        value: store.workoutText
                    )
                    if let smoking = store.smokingText, !smoking.isEmpty {
                        healthMetricRow(
                            icon: "flame.fill",
                            color: .orange,
                            label: "抽烟",
                            value: smoking
                        )
                    } else {
                        healthMetricRow(
                            icon: "flame",
                            color: .secondary,
                            label: "抽烟",
                            value: "0 支"
                        )
                    }
                }
            }
        }
    }

    // MARK: - Helpers

    private func healthMetricRow(icon: String, color: Color, label: String, value: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(color)
                .frame(width: 16, alignment: .center)
            Text(label)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.secondary)
            Spacer(minLength: 6)
            Text(value)
                .font(.system(size: 12, weight: .semibold, design: .monospaced))
                .monospacedDigit()
                .foregroundStyle(.primary)
                .lineLimit(1)
        }
    }

    private func emptyPlaceholder(_ text: String) -> some View {
        Text(text)
            .font(.system(size: 12))
            .foregroundStyle(.tertiary)
            .padding(.vertical, 6)
    }

    private func formatAmount(_ amount: Double?) -> String {
        guard let amount else { return "¥--" }
        return LifeLoggerStore.formatCurrencyAmount(amount)
    }

    private struct MealGroupDisplay {
        let key: String
        let label: String
        let itemsSummary: String
        let totalKcal: Double?
    }

    private func orderedMealGroups(from ledger: LLNutritionLedger?) -> [MealGroupDisplay] {
        guard let ledger else { return [] }

        if let meals = ledger.meals, !meals.isEmpty {
            let nonEmptyMeals = meals.filter { !$0.value.isEmpty }
            if !nonEmptyMeals.isEmpty {
                return buildMealGroupDisplays(from: nonEmptyMeals, ledger: ledger)
            }
        }

        if let records = ledger.records, !records.isEmpty {
            let consumed = records.filter { $0.isConsumed != false }
            let sourceRecords = consumed.isEmpty ? records : consumed
            var grouped: [String: [LLFoodRecord]] = [:]
            for rec in sourceRecords {
                let rawMeal = rec.meal?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() ?? "unknown"
                let normKey = normalizedMealKey(rawMeal)
                grouped[normKey, default: []].append(rec)
            }
            return buildMealGroupDisplays(from: grouped, ledger: ledger)
        }

        return []
    }

    private func buildMealGroupDisplays(from meals: [String: [LLFoodRecord]], ledger: LLNutritionLedger) -> [MealGroupDisplay] {
        let order = ["breakfast", "早餐", "lunch", "午餐", "dinner", "晚餐", "snacks", "snack", "加餐", "supplements", "unknown", "other", "饮食", "其他"]
        let sortedKeys = meals.keys.sorted { a, b in
            let ia = order.firstIndex(of: a.lowercased()) ?? Int.max
            let ib = order.firstIndex(of: b.lowercased()) ?? Int.max
            if ia != ib { return ia < ib }
            return a < b
        }

        return sortedKeys.compactMap { key in
            guard let records = meals[key], !records.isEmpty else { return nil }
            let names = records.map { rec -> String in
                let rawName = rec.displayName
                let name = rawName.isEmpty ? "餐食" : rawName
                if let weight = rec.weightG, weight > 0 {
                    return "\(name) \(Int(weight.rounded()))g"
                }
                return name
            }
            let totalKcal = serverMealTotalKcal(for: key, mealGroupCount: sortedKeys.count, ledger: ledger)
            return MealGroupDisplay(
                key: key,
                label: localizedMealLabel(key),
                itemsSummary: names.joined(separator: "、"),
                totalKcal: totalKcal
            )
        }
    }

    private func serverMealTotalKcal(for key: String, mealGroupCount: Int, ledger: LLNutritionLedger) -> Double? {
        if let mealTotals = ledger.mealTotals {
            if let kcal = mealTotals[key]?.caloriesKcal {
                return kcal
            }
            if key == "snack", let kcal = mealTotals["snacks"]?.caloriesKcal {
                return kcal
            }
            if key == "snacks", let kcal = mealTotals["snack"]?.caloriesKcal {
                return kcal
            }
            return nil
        }
        if mealGroupCount == 1 {
            return ledger.totals?.caloriesKcal
        }
        return nil
    }

    private func normalizedMealKey(_ raw: String) -> String {
        switch raw.lowercased() {
        case "breakfast", "早餐": return "breakfast"
        case "lunch", "午餐": return "lunch"
        case "dinner", "晚餐": return "dinner"
        case "snack", "snacks", "加餐": return "snack"
        default: return "unknown"
        }
    }

    private func localizedMealLabel(_ key: String) -> String {
        switch key.lowercased() {
        case "breakfast", "早餐": return "早餐"
        case "lunch", "午餐": return "午餐"
        case "dinner", "晚餐": return "晚餐"
        case "snack", "snacks", "加餐": return "加餐"
        case "drink", "drinks", "饮品": return "饮品"
        case "unknown", "other", "其他", "": return "饮食"
        default: return key
        }
    }
}

private struct OverviewCardSurface<Trailing: View, Content: View>: View {
    let title: String
    let icon: String
    let accentColor: Color
    @ViewBuilder let trailing: () -> Trailing
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.system(size: 14, weight: .semibold))
                    .foregroundStyle(accentColor)
                Text(title)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.primary)
                Spacer(minLength: 4)
                trailing()
            }

            content()

            Spacer(minLength: 0)
        }
        .padding(14)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color(nsColor: .controlBackgroundColor))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(Color.primary.opacity(0.08), lineWidth: 1)
        )
    }
}

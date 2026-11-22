import Foundation

public struct LLTimelineResponse: Codable, Sendable {
    public let date: String
    public let status: String?
    public let activities: [LLActivity]
    public let unassociatedItems: [LLActivityItem]?
    public let coverage: LLCoverage?
    public let counts: LLActivityCounts?

    enum CodingKeys: String, CodingKey {
        case date
        case status
        case activities
        case unassociatedItems = "unassociated_items"
        case coverage
        case counts
    }

    public init(
        date: String,
        status: String? = "ready",
        activities: [LLActivity] = [],
        unassociatedItems: [LLActivityItem]? = nil,
        coverage: LLCoverage? = nil,
        counts: LLActivityCounts? = nil
    ) {
        self.date = date
        self.status = status
        self.activities = activities
        self.unassociatedItems = unassociatedItems
        self.coverage = coverage
        self.counts = counts
    }
}

public struct LLCoverage: Codable, Sendable {
    public let through: String?
    public let sourceCount: Int?

    enum CodingKeys: String, CodingKey {
        case through
        case sourceCount = "source_count"
    }

    public init(through: String? = nil, sourceCount: Int? = nil) {
        self.through = through
        self.sourceCount = sourceCount
    }
}

public struct LLActivityCounts: Codable, Sendable {
    public let total: Int?
    public let liveCount: Int?
    public let mediaCount: Int?
    public let unknownCount: Int?

    enum CodingKeys: String, CodingKey {
        case total
        case liveCount = "live_count"
        case mediaCount = "media_count"
        case unknownCount = "unknown_count"
    }

    public init(total: Int? = nil, liveCount: Int? = nil, mediaCount: Int? = nil, unknownCount: Int? = nil) {
        self.total = total
        self.liveCount = liveCount
        self.mediaCount = mediaCount
        self.unknownCount = unknownCount
    }
}

public struct LLActivity: Codable, Identifiable, Sendable {
    public let activityId: String
    public let title: String
    public let summary: String?
    public let status: String?
    public let lifecycle: String?
    public let category: String?
    public let speechTarget: String?
    public let occurrenceStart: String?
    public let occurrenceEnd: String?
    public let spokenAt: String?
    public let spokenEnd: String?
    public let startAt: String?
    public let endAt: String?
    public let reportedAt: String?
    public let userCorrected: Bool?
    public let looksLikeFragment: Bool?
    public let fragmentReason: String?
    public let items: [LLActivityItem]
    public let inputContext: LLInputContext?

    public var id: String { activityId }

    enum CodingKeys: String, CodingKey {
        case activityId = "activity_id"
        case title
        case summary
        case status
        case lifecycle
        case category
        case speechTarget = "speech_target"
        case occurrenceStart = "occurrence_start"
        case occurrenceEnd = "occurrence_end"
        case spokenAt = "spoken_at"
        case spokenEnd = "spoken_end"
        case startAt = "start_at"
        case endAt = "end_at"
        case reportedAt = "reported_at"
        case userCorrected = "user_corrected"
        case looksLikeFragment = "looks_like_fragment"
        case fragmentReason = "fragment_reason"
        case items
        case inputContext = "input_context"
    }

    public init(
        activityId: String,
        title: String,
        summary: String? = nil,
        status: String? = nil,
        lifecycle: String? = "live",
        category: String? = nil,
        speechTarget: String? = nil,
        occurrenceStart: String? = nil,
        occurrenceEnd: String? = nil,
        spokenAt: String? = nil,
        spokenEnd: String? = nil,
        startAt: String? = nil,
        endAt: String? = nil,
        reportedAt: String? = nil,
        userCorrected: Bool? = nil,
        looksLikeFragment: Bool? = nil,
        fragmentReason: String? = nil,
        items: [LLActivityItem] = [],
        inputContext: LLInputContext? = nil
    ) {
        self.activityId = activityId
        self.title = title
        self.summary = summary
        self.status = status
        self.lifecycle = lifecycle
        self.category = category
        self.speechTarget = speechTarget
        self.occurrenceStart = occurrenceStart
        self.occurrenceEnd = occurrenceEnd
        self.spokenAt = spokenAt
        self.spokenEnd = spokenEnd
        self.startAt = startAt
        self.endAt = endAt
        self.reportedAt = reportedAt
        self.userCorrected = userCorrected
        self.looksLikeFragment = looksLikeFragment
        self.fragmentReason = fragmentReason
        self.items = items
        self.inputContext = inputContext
    }
}

public struct LLInputContext: Codable, Sendable {
    public let appLabel: String?
    public let targetBundle: String?
    public let kind: String?
    public let workApp: Bool?

    enum CodingKeys: String, CodingKey {
        case appLabel = "app_label"
        case targetBundle = "target_bundle"
        case kind
        case workApp = "work_app"
    }

    public init(appLabel: String? = nil, targetBundle: String? = nil, kind: String? = nil, workApp: Bool? = nil) {
        self.appLabel = appLabel
        self.targetBundle = targetBundle
        self.kind = kind
        self.workApp = workApp
    }
}

public struct LLActivityItem: Codable, Identifiable, Sendable {
    public let itemId: String?
    public let action: String?
    public let status: String?
    public let factType: String?
    public let amount: Double?
    public let currency: String?
    public let paymentStatus: String?
    public let spokenAt: String?
    public let occurredAt: String?
    public let reason: String?
    public let expense: LLExpenseDetail?
    public let evidence: [LLEvidence]?
    public let inputContext: LLInputContext?

    public var id: String {
        itemId ?? "\(action ?? "item")-\(spokenAt ?? "")"
    }

    enum CodingKeys: String, CodingKey {
        case itemId = "item_id"
        case action
        case status
        case factType = "fact_type"
        case amount
        case currency
        case paymentStatus = "payment_status"
        case spokenAt = "spoken_at"
        case occurredAt = "occurred_at"
        case reason
        case expense
        case evidence
        case inputContext = "input_context"
    }

    public init(
        itemId: String? = nil,
        action: String? = nil,
        status: String? = nil,
        factType: String? = nil,
        amount: Double? = nil,
        currency: String? = nil,
        paymentStatus: String? = nil,
        spokenAt: String? = nil,
        occurredAt: String? = nil,
        reason: String? = nil,
        expense: LLExpenseDetail? = nil,
        evidence: [LLEvidence]? = nil,
        inputContext: LLInputContext? = nil
    ) {
        self.itemId = itemId
        self.action = action
        self.status = status
        self.factType = factType
        self.amount = amount
        self.currency = currency
        self.paymentStatus = paymentStatus
        self.spokenAt = spokenAt
        self.occurredAt = occurredAt
        self.reason = reason
        self.expense = expense
        self.evidence = evidence
        self.inputContext = inputContext
    }
}

public struct LLExpenseDetail: Codable, Sendable {
    public let amount: Double?
    public let currency: String?
    public let item: String?
    public let paymentStatus: String?

    enum CodingKeys: String, CodingKey {
        case amount
        case currency
        case item
        case paymentStatus = "payment_status"
    }

    public init(amount: Double? = nil, currency: String? = nil, item: String? = nil, paymentStatus: String? = nil) {
        self.amount = amount
        self.currency = currency
        self.item = item
        self.paymentStatus = paymentStatus
    }
}

public struct LLEvidence: Codable, Sendable {
    public let sourceId: String?
    public let segmentId: String?
    public let quote: String?
    public let speaker: String?
    public let sourceLabel: String?
    public let inputContext: LLInputContext?

    enum CodingKeys: String, CodingKey {
        case sourceId = "source_id"
        case segmentId = "segment_id"
        case quote
        case speaker
        case sourceLabel = "source_label"
        case inputContext = "input_context"
    }

    public init(
        sourceId: String? = nil,
        segmentId: String? = nil,
        quote: String? = nil,
        speaker: String? = nil,
        sourceLabel: String? = nil,
        inputContext: LLInputContext? = nil
    ) {
        self.sourceId = sourceId
        self.segmentId = segmentId
        self.quote = quote
        self.speaker = speaker
        self.sourceLabel = sourceLabel
        self.inputContext = inputContext
    }
}

public struct LLDailyResponse: Codable, Sendable {
    public let date: String
    public let expenseLedger: LLExpenseLedger?
    public let nutritionLedger: LLNutritionLedger?
    public let fitnessLedger: LLFitnessLedger?
    public let smokingLedger: LLSmokingLedger?
    public let lastRecordingEndAt: String?
    public let lastSegmentReceivedAt: String?

    enum CodingKeys: String, CodingKey {
        case date
        case expenseLedger = "expense_ledger"
        case nutritionLedger = "nutrition_ledger"
        case fitnessLedger = "fitness_ledger"
        case smokingLedger = "smoking_ledger"
        case lastRecordingEndAt = "last_recording_end_at"
        case lastSegmentReceivedAt = "last_segment_received_at"
    }

    public init(
        date: String,
        expenseLedger: LLExpenseLedger? = nil,
        nutritionLedger: LLNutritionLedger? = nil,
        fitnessLedger: LLFitnessLedger? = nil,
        smokingLedger: LLSmokingLedger? = nil,
        lastRecordingEndAt: String? = nil,
        lastSegmentReceivedAt: String? = nil
    ) {
        self.date = date
        self.expenseLedger = expenseLedger
        self.nutritionLedger = nutritionLedger
        self.fitnessLedger = fitnessLedger
        self.smokingLedger = smokingLedger
        self.lastRecordingEndAt = lastRecordingEndAt
        self.lastSegmentReceivedAt = lastSegmentReceivedAt
    }
}

public struct LLExpenseLedger: Codable, Sendable {
    public let totalAmount: Double?
    public let pendingCount: Int?
    public let transactionCount: Int?
    public let transactions: [LLExpenseTransaction]?
    public let pendingItems: [LLExpensePendingItem]?

    enum CodingKeys: String, CodingKey {
        case totalAmount = "total_amount"
        case pendingCount = "pending_count"
        case transactionCount = "transaction_count"
        case transactions
        case pendingItems = "pending_items"
    }

    public init(
        totalAmount: Double? = nil,
        pendingCount: Int? = nil,
        transactionCount: Int? = nil,
        transactions: [LLExpenseTransaction]? = nil,
        pendingItems: [LLExpensePendingItem]? = nil
    ) {
        self.totalAmount = totalAmount
        self.pendingCount = pendingCount
        self.transactionCount = transactionCount
        self.transactions = transactions
        self.pendingItems = pendingItems
    }
}

public struct LLExpenseTransaction: Codable, Identifiable, Sendable {
    public let eventId: String?
    public let clock: String?
    public let title: String?
    public let item: String?
    public let amount: Double?
    public let currency: String?
    public let paymentContext: String?
    public let quote: String?

    public var id: String {
        eventId ?? "\(clock ?? "")-\(item ?? title ?? "tx")-\(amount ?? 0)"
    }

    enum CodingKeys: String, CodingKey {
        case eventId = "event_id"
        case clock
        case title
        case item
        case amount
        case currency
        case paymentContext = "payment_context"
        case quote
    }

    public init(
        eventId: String? = nil,
        clock: String? = nil,
        title: String? = nil,
        item: String? = nil,
        amount: Double? = nil,
        currency: String? = nil,
        paymentContext: String? = nil,
        quote: String? = nil
    ) {
        self.eventId = eventId
        self.clock = clock
        self.title = title
        self.item = item
        self.amount = amount
        self.currency = currency
        self.paymentContext = paymentContext
        self.quote = quote
    }
}

public struct LLExpensePendingItem: Codable, Identifiable, Sendable {
    public let eventId: String?
    public let clock: String?
    public let title: String?
    public let item: String?
    public let pendingReason: String?
    public let quote: String?

    public var id: String {
        eventId ?? "\(clock ?? "")-\(item ?? title ?? "pending")"
    }

    enum CodingKeys: String, CodingKey {
        case eventId = "event_id"
        case clock
        case title
        case item
        case pendingReason = "pending_reason"
        case quote
    }
}

public struct LLNutritionLedger: Codable, Sendable {
    public let records: [LLFoodRecord]?
    public let recordCount: Int?
    public let consumedRecordCount: Int?
    public let unweighedCandidateKcal: Double?
    public let totals: LLNutritionTotals?
    public let meals: [String: [LLFoodRecord]]?
    public let mealTotals: [String: LLNutritionTotals]?
    public let hasRecords: Bool?

    enum CodingKeys: String, CodingKey {
        case records
        case foodRecords = "food_records"
        case recordCount = "record_count"
        case consumedRecordCount = "consumed_record_count"
        case unweighedCandidateKcal = "unweighed_candidate_kcal"
        case totals
        case meals
        case mealTotals = "meal_totals"
        case hasRecords = "has_records"
    }

    public init(
        records: [LLFoodRecord]? = nil,
        recordCount: Int? = nil,
        consumedRecordCount: Int? = nil,
        unweighedCandidateKcal: Double? = nil,
        totals: LLNutritionTotals? = nil,
        meals: [String: [LLFoodRecord]]? = nil,
        mealTotals: [String: LLNutritionTotals]? = nil,
        hasRecords: Bool? = nil
    ) {
        self.records = records
        self.recordCount = recordCount
        self.consumedRecordCount = consumedRecordCount
        self.unweighedCandidateKcal = unweighedCandidateKcal
        self.totals = totals
        self.meals = meals
        self.mealTotals = mealTotals
        self.hasRecords = hasRecords
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let decodedMeals = try container.decodeIfPresent([String: [LLFoodRecord]].self, forKey: .meals)
        self.meals = decodedMeals
        if let directRecords = try container.decodeIfPresent([LLFoodRecord].self, forKey: .records) {
            self.records = directRecords
        } else if let foodRecords = try container.decodeIfPresent([LLFoodRecord].self, forKey: .foodRecords) {
            self.records = foodRecords
        } else if let decodedMeals {
            let flattened = decodedMeals.values.flatMap { $0 }
            self.records = flattened.isEmpty ? nil : flattened
        } else {
            self.records = nil
        }
        self.recordCount = try container.decodeIfPresent(Int.self, forKey: .recordCount)
        self.consumedRecordCount = try container.decodeIfPresent(Int.self, forKey: .consumedRecordCount)
        self.unweighedCandidateKcal = try container.decodeIfPresent(Double.self, forKey: .unweighedCandidateKcal)
        self.totals = try container.decodeIfPresent(LLNutritionTotals.self, forKey: .totals)
        self.mealTotals = try container.decodeIfPresent([String: LLNutritionTotals].self, forKey: .mealTotals)
        self.hasRecords = try container.decodeIfPresent(Bool.self, forKey: .hasRecords)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(records, forKey: .records)
        try container.encodeIfPresent(recordCount, forKey: .recordCount)
        try container.encodeIfPresent(consumedRecordCount, forKey: .consumedRecordCount)
        try container.encodeIfPresent(unweighedCandidateKcal, forKey: .unweighedCandidateKcal)
        try container.encodeIfPresent(totals, forKey: .totals)
        try container.encodeIfPresent(meals, forKey: .meals)
        try container.encodeIfPresent(mealTotals, forKey: .mealTotals)
        try container.encodeIfPresent(hasRecords, forKey: .hasRecords)
    }
}

public struct LLNutritionTotals: Codable, Sendable {
    public let caloriesKcal: Double?
    public let carbohydratesG: Double?
    public let proteinG: Double?
    public let fatG: Double?
    public let sodiumMg: Double?

    public var carbsG: Double? { carbohydratesG }

    enum CodingKeys: String, CodingKey {
        case caloriesKcal = "calories_kcal"
        case carbohydratesG = "carbohydrates_g"
        case carbsG = "carbs_g"
        case proteinG = "protein_g"
        case fatG = "fat_g"
        case sodiumMg = "sodium_mg"
    }

    public init(
        caloriesKcal: Double? = nil,
        carbohydratesG: Double? = nil,
        carbsG: Double? = nil,
        proteinG: Double? = nil,
        fatG: Double? = nil,
        sodiumMg: Double? = nil
    ) {
        self.caloriesKcal = caloriesKcal
        self.carbohydratesG = carbohydratesG ?? carbsG
        self.proteinG = proteinG
        self.fatG = fatG
        self.sodiumMg = sodiumMg
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.caloriesKcal = try container.decodeIfPresent(Double.self, forKey: .caloriesKcal)
        self.carbohydratesG = try container.decodeIfPresent(Double.self, forKey: .carbohydratesG)
            ?? container.decodeIfPresent(Double.self, forKey: .carbsG)
        self.proteinG = try container.decodeIfPresent(Double.self, forKey: .proteinG)
        self.fatG = try container.decodeIfPresent(Double.self, forKey: .fatG)
        self.sodiumMg = try container.decodeIfPresent(Double.self, forKey: .sodiumMg)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(caloriesKcal, forKey: .caloriesKcal)
        try container.encodeIfPresent(carbohydratesG, forKey: .carbohydratesG)
        try container.encodeIfPresent(proteinG, forKey: .proteinG)
        try container.encodeIfPresent(fatG, forKey: .fatG)
        try container.encodeIfPresent(sodiumMg, forKey: .sodiumMg)
    }
}

public struct LLPortionEstimateCandidate: Codable, Sendable {
    public let grams: Double?
    public let caloriesKcal: Double?
    public let nutrients: LLNutritionTotals?

    enum CodingKeys: String, CodingKey {
        case grams
        case caloriesKcal = "calories_kcal"
        case nutrients
    }

    public init(
        grams: Double? = nil,
        caloriesKcal: Double? = nil,
        nutrients: LLNutritionTotals? = nil
    ) {
        self.grams = grams
        self.caloriesKcal = caloriesKcal
        self.nutrients = nutrients
    }
}

public struct LLFoodRecord: Codable, Identifiable, Sendable {
    public let eventId: String?
    public let food: String?
    public let item: String?
    public let title: String?
    public let weightG: Double?
    public let caloriesKcal: Double?
    public let carbohydratesG: Double?
    public let proteinG: Double?
    public let fatG: Double?
    public let sodiumMg: Double?
    public let nutrients: LLNutritionTotals?
    public let recordType: String?
    public let isConsumed: Bool?
    public let meal: String?
    public let quote: String?
    public let portionEstimateCandidate: LLPortionEstimateCandidate?

    public var id: String {
        eventId ?? "\(meal ?? "")-\(displayName)-\(weightG ?? 0)"
    }

    public var carbsG: Double? {
        carbohydratesG ?? nutrients?.carbohydratesG
    }

    enum CodingKeys: String, CodingKey {
        case eventId = "event_id"
        case food
        case item
        case title
        case weightG = "weight_g"
        case caloriesKcal = "calories_kcal"
        case carbohydratesG = "carbohydrates_g"
        case carbsG = "carbs_g"
        case proteinG = "protein_g"
        case fatG = "fat_g"
        case sodiumMg = "sodium_mg"
        case nutrients
        case recordType = "record_type"
        case isConsumed = "is_consumed"
        case meal
        case quote
        case portionEstimateCandidate = "portion_estimate_candidate"
    }

    public init(
        eventId: String? = nil,
        food: String? = nil,
        item: String? = nil,
        title: String? = nil,
        weightG: Double? = nil,
        caloriesKcal: Double? = nil,
        carbohydratesG: Double? = nil,
        carbsG: Double? = nil,
        proteinG: Double? = nil,
        fatG: Double? = nil,
        sodiumMg: Double? = nil,
        nutrients: LLNutritionTotals? = nil,
        recordType: String? = nil,
        isConsumed: Bool? = nil,
        meal: String? = nil,
        quote: String? = nil,
        portionEstimateCandidate: LLPortionEstimateCandidate? = nil
    ) {
        self.eventId = eventId
        self.food = food
        self.item = item
        self.title = title
        self.weightG = weightG
        self.caloriesKcal = caloriesKcal
        self.carbohydratesG = carbohydratesG ?? carbsG ?? nutrients?.carbohydratesG
        self.proteinG = proteinG ?? nutrients?.proteinG
        self.fatG = fatG ?? nutrients?.fatG
        self.sodiumMg = sodiumMg ?? nutrients?.sodiumMg
        self.nutrients = nutrients
        self.recordType = recordType
        self.isConsumed = isConsumed
        self.meal = meal
        self.quote = quote
        self.portionEstimateCandidate = portionEstimateCandidate
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        self.eventId = try container.decodeIfPresent(String.self, forKey: .eventId)
        self.food = try container.decodeIfPresent(String.self, forKey: .food)
        self.item = try container.decodeIfPresent(String.self, forKey: .item)
        self.title = try container.decodeIfPresent(String.self, forKey: .title)
        self.weightG = try container.decodeIfPresent(Double.self, forKey: .weightG)
        self.caloriesKcal = try container.decodeIfPresent(Double.self, forKey: .caloriesKcal)
        let decodedNutrients = try container.decodeIfPresent(LLNutritionTotals.self, forKey: .nutrients)
        self.nutrients = decodedNutrients
        self.carbohydratesG = try container.decodeIfPresent(Double.self, forKey: .carbohydratesG)
            ?? container.decodeIfPresent(Double.self, forKey: .carbsG)
            ?? decodedNutrients?.carbohydratesG
        self.proteinG = try container.decodeIfPresent(Double.self, forKey: .proteinG)
            ?? decodedNutrients?.proteinG
        self.fatG = try container.decodeIfPresent(Double.self, forKey: .fatG)
            ?? decodedNutrients?.fatG
        self.sodiumMg = try container.decodeIfPresent(Double.self, forKey: .sodiumMg)
            ?? decodedNutrients?.sodiumMg
        self.recordType = try container.decodeIfPresent(String.self, forKey: .recordType)
        self.isConsumed = try container.decodeIfPresent(Bool.self, forKey: .isConsumed)
        self.meal = try container.decodeIfPresent(String.self, forKey: .meal)
        self.quote = try container.decodeIfPresent(String.self, forKey: .quote)
        self.portionEstimateCandidate = try container.decodeIfPresent(LLPortionEstimateCandidate.self, forKey: .portionEstimateCandidate)
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encodeIfPresent(eventId, forKey: .eventId)
        try container.encodeIfPresent(food, forKey: .food)
        try container.encodeIfPresent(item, forKey: .item)
        try container.encodeIfPresent(title, forKey: .title)
        try container.encodeIfPresent(weightG, forKey: .weightG)
        try container.encodeIfPresent(caloriesKcal, forKey: .caloriesKcal)
        try container.encodeIfPresent(carbohydratesG, forKey: .carbohydratesG)
        try container.encodeIfPresent(proteinG, forKey: .proteinG)
        try container.encodeIfPresent(fatG, forKey: .fatG)
        try container.encodeIfPresent(sodiumMg, forKey: .sodiumMg)
        try container.encodeIfPresent(nutrients, forKey: .nutrients)
        try container.encodeIfPresent(recordType, forKey: .recordType)
        try container.encodeIfPresent(isConsumed, forKey: .isConsumed)
        try container.encodeIfPresent(meal, forKey: .meal)
        try container.encodeIfPresent(quote, forKey: .quote)
        try container.encodeIfPresent(portionEstimateCandidate, forKey: .portionEstimateCandidate)
    }

    public var displayName: String {
        (food ?? item ?? title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    public var effectiveCaloriesKcal: Double? {
        caloriesKcal ?? portionEstimateCandidate?.caloriesKcal
    }

    public var effectiveWeightG: Double? {
        weightG ?? portionEstimateCandidate?.grams
    }
}

public struct LLFitnessLedger: Codable, Sendable {
    public let summary: String?
    public let totals: LLFitnessTotals?

    public init(summary: String? = nil, totals: LLFitnessTotals? = nil) {
        self.summary = summary
        self.totals = totals
    }
}

public struct LLFitnessTotals: Codable, Sendable {
    public let sessionCount: Int?
    public let workoutCount: Int?

    enum CodingKeys: String, CodingKey {
        case sessionCount = "session_count"
        case workoutCount = "workout_count"
    }
}

public struct LLSmokingLedger: Codable, Sendable {
    public let totalCount: Int?
    public let recordCount: Int?

    enum CodingKeys: String, CodingKey {
        case totalCount = "total_count"
        case recordCount = "record_count"
    }
}

public struct LLHealthSummaryResponse: Codable, Sendable {
    public let ok: Bool?
    public let summary: LLHealthSummary?
    public let lastRecordingEndAt: String?
    public let lastSegmentReceivedAt: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case summary
        case lastRecordingEndAt = "last_recording_end_at"
        case lastSegmentReceivedAt = "last_segment_received_at"
    }
}

public struct LLHealthSummary: Codable, Sendable {
    public let date: String?
    public let avgHeartRate: Double?
    public let minHeartRate: Double?
    public let maxHeartRate: Double?
    public let totalSteps: Int?
    public let sleepMinutes: Int?
    public let deepSleepMinutes: Int?
    public let stressSpikesCount: Int?
    public let lastRecordingEndAt: String?
    public let lastSegmentReceivedAt: String?

    enum CodingKeys: String, CodingKey {
        case date
        case avgHeartRate = "avg_heart_rate"
        case minHeartRate = "min_heart_rate"
        case maxHeartRate = "max_heart_rate"
        case totalSteps = "total_steps"
        case sleepMinutes = "sleep_minutes"
        case deepSleepMinutes = "deep_sleep_minutes"
        case stressSpikesCount = "stress_spikes_count"
        case lastRecordingEndAt = "last_recording_end_at"
        case lastSegmentReceivedAt = "last_segment_received_at"
    }

    public init(
        date: String? = nil,
        avgHeartRate: Double? = nil,
        minHeartRate: Double? = nil,
        maxHeartRate: Double? = nil,
        totalSteps: Int? = nil,
        sleepMinutes: Int? = nil,
        deepSleepMinutes: Int? = nil,
        stressSpikesCount: Int? = nil,
        lastRecordingEndAt: String? = nil,
        lastSegmentReceivedAt: String? = nil
    ) {
        self.date = date
        self.avgHeartRate = avgHeartRate
        self.minHeartRate = minHeartRate
        self.maxHeartRate = maxHeartRate
        self.totalSteps = totalSteps
        self.sleepMinutes = sleepMinutes
        self.deepSleepMinutes = deepSleepMinutes
        self.stressSpikesCount = stressSpikesCount
        self.lastRecordingEndAt = lastRecordingEndAt
        self.lastSegmentReceivedAt = lastSegmentReceivedAt
    }
}

public struct LLVersionResponse: Codable, Sendable {
    public let ok: Bool?
    public let appVersion: String?
    public let releaseNote: String?
    public let codeFingerprint: String?
    public let gitCommit: String?
    public let deployedAt: String?
    public let deployState: String?

    enum CodingKeys: String, CodingKey {
        case ok
        case appVersion = "app_version"
        case releaseNote = "release_note"
        case codeFingerprint = "code_fingerprint"
        case gitCommit = "git_commit"
        case deployedAt = "deployed_at"
        case deployState = "deploy_state"
    }
}

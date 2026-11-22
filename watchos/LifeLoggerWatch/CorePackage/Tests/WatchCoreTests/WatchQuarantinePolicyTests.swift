import Foundation
import XCTest
@testable import WatchCore

/// Staged audio that could not be read is quarantined as `<name>.m4a.unreadable` and, by
/// design, the bytes stay on the Watch. Nothing ever deleted them, so they accumulated and
/// kept their session directory alive forever. These tests pin the "visible + cleanable"
/// contract: the app can count them, size them, and remove them on demand.
final class WatchQuarantinePolicyTests: XCTestCase {
    private var stagingRoot: URL!

    override func setUpWithError() throws {
        stagingRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("watch-quarantine-tests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: stagingRoot, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: stagingRoot)
    }

    @discardableResult
    private func writeStagedFile(
        sessionID: String,
        name: String,
        bytes: Int = 128
    ) throws -> URL {
        let directory = stagingRoot.appendingPathComponent(sessionID, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let url = directory.appendingPathComponent(name)
        try Data(repeating: 0x41, count: bytes).write(to: url)
        return url
    }

    // MARK: - Listing

    /// Only the quarantined files are listed: a healthy staged `.m4a` is owned by recovery,
    /// and the ledger/manifest files in the same directory must never show up as rubbish.
    func testScanListsOnlyQuarantinedFilesWithTheirSizes() throws {
        try writeStagedFile(sessionID: "session-a", name: "003.m4a.unreadable", bytes: 2048)
        try writeStagedFile(sessionID: "session-a", name: "004.m4a", bytes: 4096)
        try writeStagedFile(sessionID: "session-a", name: "manifest.json", bytes: 32)
        try writeStagedFile(sessionID: "session-b", name: "001.m4a.unreadable", bytes: 1024)

        let files = WatchQuarantinePolicy.scan(stagingRoot: stagingRoot)

        XCTAssertEqual(files.map(\.path), ["session-a/003.m4a.unreadable", "session-b/001.m4a.unreadable"])
        XCTAssertEqual(files.map(\.byteCount), [2048, 1024])
        XCTAssertEqual(WatchQuarantinePolicy.totalBytes(files), 3072)
    }

    /// A `<seq>.m4a.unreadable` file knows which segment it was, so the screen can say
    /// "第 4 段" instead of a hash of a filename.
    func testQuarantinedFileExposesItsOriginalNameAndSequence() throws {
        try writeStagedFile(sessionID: "session-a", name: "segment-000003.m4a.unreadable")

        let file = try XCTUnwrap(WatchQuarantinePolicy.scan(stagingRoot: stagingRoot).first)

        XCTAssertEqual(file.displayName, "segment-000003.m4a")
        XCTAssertEqual(file.sequence, 3)
        XCTAssertEqual(file.sessionID, "session-a")
        XCTAssertEqual(file.path, "session-a/segment-000003.m4a.unreadable")
    }

    /// Nothing staged, nothing to show: the entry point must stay hidden rather than
    /// render an empty screen nobody can act on.
    func testScanToleratesMissingAndEmptyStagingRoot() throws {
        XCTAssertTrue(WatchQuarantinePolicy.scan(stagingRoot: stagingRoot).isEmpty)

        let missing = stagingRoot.appendingPathComponent("does-not-exist", isDirectory: true)
        XCTAssertTrue(WatchQuarantinePolicy.scan(stagingRoot: missing).isEmpty)
    }

    /// A file whose name carries no segment number is still rubbish that occupies the Watch:
    /// show it with no sequence to claim, never invent one.
    func testScanKeepsNamesWithoutASegmentNumber() throws {
        try writeStagedFile(sessionID: "session-a", name: "copy of segment.m4a.unreadable")

        let file = try XCTUnwrap(WatchQuarantinePolicy.scan(stagingRoot: stagingRoot).first)

        XCTAssertEqual(file.displayName, "copy of segment.m4a")
        XCTAssertNil(file.sequence)
    }

    // MARK: - Presentation

    func testStatusLineStaysHiddenWhenThereIsNothingQuarantined() {
        XCTAssertNil(WatchQuarantinePolicy.statusLine(count: 0, totalBytes: 0))
    }

    /// The line has to carry the two facts the user acts on: how many, and how much space.
    func testStatusLineReportsCountAndSpace() {
        let line = WatchQuarantinePolicy.statusLine(count: 3, totalBytes: 4_600_000)

        XCTAssertEqual(line, "3 个无法读取的录音片段 · 4.6 MB")
        XCTAssertEqual(WatchQuarantinePolicy.formattedSize(4_600_000), "4.6 MB")
    }

    func testFormattedSizeUsesReadableUnits() {
        XCTAssertEqual(WatchQuarantinePolicy.formattedSize(0), "0 B")
        XCTAssertEqual(WatchQuarantinePolicy.formattedSize(1024), "1 KB")
        XCTAssertEqual(WatchQuarantinePolicy.formattedSize(1_048_576), "1 MB")
        XCTAssertEqual(WatchQuarantinePolicy.formattedSize(1_500_000_000), "1.5 GB")
    }

    // MARK: - Removal

    /// Deleting one file must not take a healthy neighbour with it, and the session
    /// directory it was the last tenant of must not survive as an empty husk.
    func testDeletingOneQuarantinedFileKeepsHealthySiblingsAndPrunesEmptyDirectory() throws {
        try writeStagedFile(sessionID: "session-a", name: "003.m4a.unreadable", bytes: 2048)
        try writeStagedFile(sessionID: "session-a", name: "004.m4a", bytes: 4096)
        let file = try XCTUnwrap(WatchQuarantinePolicy.scan(stagingRoot: stagingRoot).first)

        let freed = try WatchQuarantinePolicy.delete(file, stagingRoot: stagingRoot)

        XCTAssertEqual(freed, 2048)
        XCTAssertTrue(WatchQuarantinePolicy.scan(stagingRoot: stagingRoot).isEmpty)
        let sibling = stagingRoot
            .appendingPathComponent("session-a/004.m4a")
        XCTAssertTrue(FileManager.default.fileExists(atPath: sibling.path))
        XCTAssertFalse(
            FileManager.default.fileExists(
                atPath: stagingRoot.appendingPathComponent("session-a/003.m4a.unreadable").path
            )
        )
    }

    func testDeletingKeepsSessionDirectoryWhenOtherFilesRemain() throws {
        try writeStagedFile(sessionID: "session-a", name: "003.m4a.unreadable")
        try writeStagedFile(sessionID: "session-a", name: "manifest.json")
        let file = try XCTUnwrap(WatchQuarantinePolicy.scan(stagingRoot: stagingRoot).first)

        _ = try WatchQuarantinePolicy.delete(file, stagingRoot: stagingRoot)

        XCTAssertTrue(
            FileManager.default.fileExists(
                atPath: stagingRoot.appendingPathComponent("session-a/manifest.json").path
            )
        )
    }

    /// "全部清理" must report what it actually removed — a file the OS already swept away
    /// counts as a failure, not as freed bytes.
    func testDeleteAllReportsFreedBytesAndFailures() throws {
        try writeStagedFile(sessionID: "session-a", name: "001.m4a.unreadable", bytes: 1024)
        try writeStagedFile(sessionID: "session-b", name: "002.m4a.unreadable", bytes: 512)
        var files = WatchQuarantinePolicy.scan(stagingRoot: stagingRoot)
        XCTAssertEqual(files.count, 2)
        files.append(
            WatchQuarantinedFile(
                sessionID: "session-c",
                fileName: "003.m4a.unreadable",
                byteCount: 99,
                quarantinedAt: nil
            )
        )

        let result = WatchQuarantinePolicy.deleteAll(files, stagingRoot: stagingRoot)

        XCTAssertEqual(result.deletedCount, 2)
        XCTAssertEqual(result.failedCount, 1)
        XCTAssertEqual(result.bytesFreed, 1536)
        XCTAssertTrue(WatchQuarantinePolicy.scan(stagingRoot: stagingRoot).isEmpty)
    }

    /// The bytes freed are measured on disk, not trusted from a stale listing.
    func testDeleteReportsBytesMeasuredAtRemovalTime() throws {
        let url = try writeStagedFile(sessionID: "session-a", name: "001.m4a.unreadable", bytes: 10)
        let stale = WatchQuarantinedFile(
            sessionID: "session-a",
            fileName: "001.m4a.unreadable",
            byteCount: 999_999,
            quarantinedAt: nil
        )

        let freed = try WatchQuarantinePolicy.delete(stale, stagingRoot: stagingRoot)

        XCTAssertEqual(freed, 10)
        XCTAssertFalse(FileManager.default.fileExists(atPath: url.path))
    }
}

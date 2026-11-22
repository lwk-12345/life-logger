import Foundation
import XCTest
@testable import WatchCore

final class WatchUploadCredentialTests: XCTestCase {
    func testShipsAKeyAndNamesTheHeaderTheMacChecks() {
        XCTAssertFalse(WatchUploadCredential.embedded.isEmpty)
        XCTAssertEqual(WatchUploadCredential.headerName, "X-LifeLogger-Watch-Key")
    }

    /// A deployment can rotate the key through defaults without rebuilding the source.
    func testStoredKeyOverridesTheEmbeddedOne() {
        let defaults = UserDefaults(suiteName: "watch-upload-credential-tests")!
        defaults.removePersistentDomain(forName: "watch-upload-credential-tests")

        XCTAssertEqual(WatchUploadCredential.current(defaults: defaults), WatchUploadCredential.embedded)

        defaults.set("rotated-key", forKey: "lifeLogger.watch.uploadKey")
        XCTAssertEqual(WatchUploadCredential.current(defaults: defaults), "rotated-key")

        // An empty stored value must not silently disable the credential.
        defaults.set("", forKey: "lifeLogger.watch.uploadKey")
        XCTAssertEqual(WatchUploadCredential.current(defaults: defaults), WatchUploadCredential.embedded)
    }

    func testComparisonRejectsWrongShortAndMissingValues() {
        XCTAssertTrue(WatchUploadCredential.matches(WatchUploadCredential.embedded))
        XCTAssertFalse(WatchUploadCredential.matches(nil))
        XCTAssertFalse(WatchUploadCredential.matches(""))
        XCTAssertFalse(WatchUploadCredential.matches("lifelogger-watch-upload-key-v2"))
        // Same length, different content: the constant-time loop must still reject it.
        let sameLength = String(repeating: "x", count: WatchUploadCredential.embedded.count)
        XCTAssertFalse(WatchUploadCredential.matches(sameLength))
    }
}

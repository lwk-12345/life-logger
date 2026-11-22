import Foundation
import XCTest
@testable import WatchCore

final class WatchHealthResponsePolicyTests: XCTestCase {
    func testExplicitlyEnabledMacHealthResponseIsAccepted() {
        XCTAssertEqual(
            WatchHealthResponsePolicy.classify(
                statusCode: 200,
                body: Data(#"{"watch_upload_enabled":true}"#.utf8)
            ),
            .ready
        )
    }

    func testExplicitlyDisabledWatchUploadIsNotReportedAsUnreachable() {
        XCTAssertEqual(
            WatchHealthResponsePolicy.classify(
                statusCode: 200,
                body: Data(#"{"watch_upload_enabled":false}"#.utf8)
            ),
            .directAccessDisabled
        )
    }

    func testMissingHealthFieldIsAnInvalidResponseNotDisabledAccess() {
        XCTAssertEqual(
            WatchHealthResponsePolicy.classify(statusCode: 200, body: Data("{}".utf8)),
            .invalidResponse
        )
    }

    func testMistypedHealthFieldIsAnInvalidResponse() {
        XCTAssertEqual(
            WatchHealthResponsePolicy.classify(
                statusCode: 200,
                body: Data(#"{"watch_upload_enabled":"true"}"#.utf8)
            ),
            .invalidResponse
        )
    }

    func testNumericHealthFieldIsNotAcceptedAsBoolean() {
        for value in ["1", "0"] {
            XCTAssertEqual(
                WatchHealthResponsePolicy.classify(
                    statusCode: 200,
                    body: Data("{\"watch_upload_enabled\":\(value)}".utf8)
                ),
                .invalidResponse
            )
        }
    }

    func testMalformedHealthJSONIsAnInvalidResponse() {
        XCTAssertEqual(
            WatchHealthResponsePolicy.classify(statusCode: 200, body: Data("{".utf8)),
            .invalidResponse
        )
    }

    func testUnauthorizedHTTPResponseMeansMacWasReachedButAccessDenied() {
        XCTAssertEqual(
            WatchHealthResponsePolicy.classify(statusCode: 401, body: Data()),
            .unauthorized
        )
    }

    func testOtherHTTPFailurePreservesItsStatusCode() {
        XCTAssertEqual(
            WatchHealthResponsePolicy.classify(statusCode: 503, body: Data()),
            .serverRejected(statusCode: 503)
        )
    }
}

import XCTest
@testable import WatchCore

final class WatchMacEndpointTests: XCTestCase {
    func testAcceptsCurrentTrustedMacEndpoint() throws {
        let endpoint = try XCTUnwrap(
            WatchMacEndpoint.validatedBaseURL("http://172.20.10.3:8765")
        )
        XCTAssertEqual(endpoint.absoluteString, "http://172.20.10.3:8765")

        let mDNSEndpoint = try XCTUnwrap(
            WatchMacEndpoint.validatedBaseURL("http://lwkdeMacBook-Pro-1020.local:8765")
        )
        XCTAssertEqual(mDNSEndpoint.absoluteString, "http://lwkdeMacBook-Pro-1020.local:8765")

        let localhostEndpoint = try XCTUnwrap(
            WatchMacEndpoint.validatedBaseURL("http://localhost:8765")
        )
        XCTAssertEqual(localhostEndpoint.absoluteString, "http://localhost:8765")
    }

    func testRejectsNonPrivateOrCredentialBearingEndpoints() {
        XCTAssertNil(WatchMacEndpoint.validatedBaseURL("https://172.20.10.3:8765"))
        XCTAssertNil(WatchMacEndpoint.validatedBaseURL("http://8.8.8.8:8765"))
        XCTAssertNil(WatchMacEndpoint.validatedBaseURL("http://172.20.10.3:8080"))
        XCTAssertNil(WatchMacEndpoint.validatedBaseURL("http://172.20.10.3:8765/?token=secret"))
        XCTAssertNil(WatchMacEndpoint.validatedBaseURL("http://user@172.20.10.3:8765"))
    }
}

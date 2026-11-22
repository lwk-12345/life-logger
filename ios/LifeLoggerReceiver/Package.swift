// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "LifeLoggerReceiver",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "ReceiverCore", targets: ["ReceiverCore"])
    ],
    targets: [
        .target(name: "ReceiverCore", path: "Sources/ReceiverCore"),
        .testTarget(
            name: "ReceiverCoreTests",
            dependencies: ["ReceiverCore"],
            path: "Tests/ReceiverCoreTests"
        )
    ]
)

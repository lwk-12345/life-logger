// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "LifeLoggerMac",
    platforms: [
        .macOS(.v13)
    ],
    products: [
        .library(
            name: "LifeLoggerCore",
            targets: ["LifeLoggerCore"]
        ),
        .executable(
            name: "life-logger-mac",
            targets: ["LifeLoggerApp"]
        )
    ],
    targets: [
        .target(
            name: "LifeLoggerCore",
            path: "Sources/LifeLoggerCore"
        ),
        .executableTarget(
            name: "LifeLoggerApp",
            dependencies: ["LifeLoggerCore"],
            path: "Sources/LifeLoggerApp"
        ),
        .testTarget(
            name: "LifeLoggerCoreTests",
            dependencies: ["LifeLoggerCore"],
            path: "Tests/LifeLoggerCoreTests"
        )
    ]
)

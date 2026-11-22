// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "AirPassportReceiver",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .library(name: "AirPassportReceiver", targets: ["AirPassportReceiver"]),
        .executable(name: "air-passport-bridge", targets: ["BridgeCLI"]),
        .executable(name: "air-passport-menu", targets: ["AirPassportMenu"])
    ],
    targets: [
        .target(
            name: "PassportBridgeC",
            publicHeadersPath: "include"
        ),
        .target(
            name: "AirPassportReceiver",
            dependencies: ["PassportBridgeC"]
        ),
        .executableTarget(
            name: "BridgeCLI",
            dependencies: ["AirPassportReceiver"]
        ),
        .executableTarget(
            name: "AirPassportMenu",
            dependencies: ["AirPassportReceiver"]
        ),
        .testTarget(
            name: "AirPassportReceiverTests",
            dependencies: ["AirPassportReceiver", "PassportBridgeC"]
        )
    ]
)

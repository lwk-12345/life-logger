// swift-tools-version: 6.0
import PackageDescription

// Compile production capture AND app sync sources for native audio/coordinator tests.
let package = Package(
    name: "WatchContinuousCaptureTests",
    platforms: [.macOS(.v15)],
    targets: [
        .target(
            name: "CaptureUnderTest", path: ".",
            exclude: [
                "build", "CaptureTests", "SyncTests", "LifeLoggerWatch.xcodeproj",
                "CorePackage/Tests", "CorePackage/Package.swift", "README.md",
                "REPAIR-20260925.md", "project.yml", "project.yml.bak-20260925-201105",
                "App/ExtensionInfo.plist", "App/WatchAppInfo.plist", "App/LifeLoggerWatch.entitlements",
                "App/LifeLoggerWatchApp.swift", "App/WatchRecordingCoordinator.swift",
                "App/RecorderHomeView.swift", "App/MacSyncSettingsView.swift",
                "App/WatchHealthSnapshotManager.swift", "App/WatchLocationSnapshotManager.swift",
                "App/WatchMeterModel.swift", "App/WatchNowPlayingSession.swift"
            ],
            sources: [
                "CorePackage/Sources/WatchCore", "App/WatchContinuousAudioCapture.swift",
                "App/MacWatchSessionTransport.swift", "App/WatchCompletionQueue.swift",
                "App/WatchCaptureEventLog.swift"
            ]
        ),
        .testTarget(name: "CaptureUnderTestTests", dependencies: ["CaptureUnderTest"], path: "CaptureTests"),
        .testTarget(name: "SyncUnderTestTests", dependencies: ["CaptureUnderTest"], path: "SyncTests")
    ]
)

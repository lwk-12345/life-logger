# Life Logger Watch MVP

Standalone watchOS target for the Apple Watch Series 5 recording validation. It has no iOS companion target.

## Current slice

- SwiftUI Watch UI with start/stop recording and a local upload-queue view.
- Microphone permission and watchOS audio background mode are declared.
- AAC/M4A capture is configured as approximately 60-second files.
- `SegmentLedger` atomically stores session manifests and SHA-256 segment records; it deletes a local segment only after a matching Mac ACK is explicitly passed to the ledger.
- On app startup, valid finalized M4A files left in recorder staging are recovered into the durable ledger before queue sync; unreadable or checksum-conflicting staging files are retained and surfaced instead of silently discarded.
- Mac has a Watch-specific session/segment API and can assemble completed sessions into one Life Logger job. Tokenless access is limited to Watch upload routes and only enabled when the server binds to a specific trusted private IP; all other APIs still require the Life Logger token.
- The test build uses the current Mac address `http://172.20.10.3:8765`, checks `/api/watch-health` automatically, and has no URL or token input on Watch. This fixed address is for the current hotspot test and must change if the Mac receives a different address.
- v27 defers all synchronization while capture is open, including interruption recovery. Starting capture cancels outstanding network tasks; idle sync and queue actions retain unacknowledged audio for later retry.
- The current sender uses request-bound URLSession calls so it can validate the Mac's response before deleting audio. Transfers are not yet guaranteed to continue after app suspension/termination; if real-watch tests require that, add delegate-backed background URLSession restoration and WatchKit background-task handling.

## Build and tests

From this directory:

```sh
xcodegen generate --spec project.yml --project-root .
xcodebuild -project LifeLoggerWatch.xcodeproj -target LifeLoggerWatch -sdk watchos CODE_SIGNING_ALLOWED=NO build
swift test --package-path CorePackage
```

The signed zero-input build was installed and launched on the physical Series 5. Staging recovery is covered by host-side ledger tests but has not been exercised on an Apple Watch. Background microphone capture, segment-boundary continuity, local-network permission, same-hotspot reachability, real segment upload/ACK, upload behavior while recording, and battery life still need physical testing.

The installed signed app bundle is at `build/SignedDerivedData/Build/Products/Debug-watchos/LifeLoggerWatch.app`; the unsigned compile-only bundle remains at `build/Debug-watchos/LifeLoggerWatch.app`.

## v27 capture recovery and diagnostics

Every segment reasserts AVAudioSession activation. Rotation startup failures retry with
0.5/1/2/4/8-second backoff for up to 90 seconds before saving the session.
Interruptions retry at 2/4/8 seconds, switching to 30 seconds after two minutes.
These are process-local tasks: watchOS suspension can delay retries; they do not guarantee
execution while the OS has suspended the app. Foreground reconciliation remains a fallback.
Remote media commands are disabled; use the app's protected stop button.

首页 → 录音事件 shows the newest capture, interruption, recovery and scene events.
The device retains a current and rotated log, each approximately 192 KiB. Retrieve them:

```sh
xcrun devicectl device copy from --device YOUR_WATCH_DEVICE_ID --domain-type appDataContainer --domain-identifier com.lifelogger.watch --source "Library/Application Support/LifeLogger/capture-events.log" --destination capture-events.log
```

Completion currently uses a foreground URLSession, not guaranteed background delivery.
Missing sequences remain explicitly unresolved, since the Mac requires all segment indices.
The existing stop/recreate recorder still introduces a segment-boundary audio gap; v27
hardens recovery and does not replace capture with continuous AVAudioEngine streaming.

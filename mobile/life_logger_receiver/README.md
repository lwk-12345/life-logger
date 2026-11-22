# Life Logger Receiver (Flutter MVP)

Passport sends audio directly to the phone over the same Wi-Fi network. The phone validates and saves each chunk before returning HTTP success, then relays saved chunks, event markers, and the end marker to the existing Mac receiver. Mac remains responsible for ASR, diarization/context, classification, LLM organization, and the existing OpenClaw flow.

## Wire protocol

- HTTP listens on `0.0.0.0:8766`: `GET /health`, `POST /v1/audio/chunk`, and `POST /v1/audio/end`.
- UDP listens on `0.0.0.0:8767` for `LL_DISCOVER_V1 <8-hex nonce>` and replies to that sender with `LL_RECEIVER_V1 <same nonce> 8766`.
- Audio bodies are raw mono `s16le`; the expected Passport rate is 16 kHz. The receiver checks the `crc32:xxxxxxxx` header, chunk index, duplicate consistency, and missing/out-of-order chunks.
- Accepted PCM chunks are written and flushed before the phone returns HTTP 200. Chunks and JSONL manifests are retained in the app's private support directory. Complete sessions are assembled into `recording.wav`.

## Mac outbox

Configure the Mac's existing receiver URL in the app. It tries the direct LAN URL first and tries the configured Tailscale URL after a network/timeout failure. It posts ordered audio chunks, event markers, then the end marker to the existing Mac endpoints. Each outbox item remains pending until that endpoint returns 2xx; the receiver's current HTTP 200 is the application's ACK boundary. The phone does not delete source chunks after ACK.

The Mac endpoint is currently unauthenticated HTTP. CRC32 detects accidental corruption; it does not authenticate the sender or protect confidentiality. LAN transfers are plaintext. Tailscale protects the off-LAN path when both devices are connected to the intended tailnet, but the Mac endpoint still has no application-level authentication. The existing Mac service also does not deduplicate event markers, so a lost event response followed by retry can create a duplicate event on Mac.

## iOS discovery and background limits

The current Passport firmware sends IPv4 broadcast discovery to `255.255.255.255:8767`. iOS requires `com.apple.developer.networking.multicast` to receive UDP broadcast, and Apple requires approval for this entitlement. The entitlement is declared in `ios/Runner/Runner.entitlements`; declaring it does not grant approval. Without the approved entitlement, an unmodified Passport has no Bonjour or provisioned-endpoint fallback and cannot reliably discover this iPhone receiver. Do not change the Passport firmware as a workaround.

The app also declares `NSLocalNetworkUsageDescription`. Local Network permission and the multicast entitlement are separate requirements. Simulator builds do not verify Local Network privacy behavior; use a real iPhone for that check.

iOS reception is foreground-only. When the app enters the background, the Dart receiver stops and restarts on return to the foreground. This MVP does not claim locked-screen reception or an hour-long background stream. Android starts a visible `connectedDevice` foreground service while receiving; OEM battery policies, process termination, and real-device behavior still need verification.

## Buffer and deployment notes

At 16 kHz, mono, 16-bit PCM, the stream is 32,000 bytes/second. Eight MiB would hold about 4 minutes 22 seconds only if all of it were available for raw audio; firmware, filesystem, and protocol use reduce that. Five to ten minutes requires compression, a lower rate, or more usable storage. This receiver does not change the firmware buffer.

Android 17 targets require runtime local-network permission for LAN sockets; the platform adapter requests `ACCESS_LOCAL_NETWORK` on API 37 and later. The Android foreground service starts from the visible app and displays an ongoing notification. A device with Tailscale must be signed in and able to reach the Mac's existing port 8766 endpoint.

## Build and test

With Flutter installed, the intended checks are:

```sh
flutter pub get
flutter test
flutter analyze
flutter build ios --no-codesign
flutter build apk --debug
```

This checkout currently has no Flutter/Dart SDK, Android SDK, ADB, or Gradle executable, and no iOS simulator devices. Therefore these commands have not been run. The source and adapter files in this directory still need Flutter's generated iOS/Android project scaffolding before platform builds can run. A real iPhone build additionally needs a signing team/profile that carries Apple's approved multicast entitlement. Test LAN permission, discovery, background behavior, and Tailscale transfer on physical devices.

## Official platform references

- [Apple TN3151: Choosing the right networking API](https://developer.apple.com/documentation/technotes/tn3151-choosing-the-right-networking-api)
- [Apple multicast networking entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.developer.networking.multicast)
- [Apple TN3179: Understanding local network privacy](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy)
- [Apple background execution modes](https://developer.apple.com/documentation/xcode/configuring-background-execution-modes)
- [Android local network permission](https://developer.android.com/privacy-and-security/local-network-permission)
- [Android foreground service types](https://developer.android.com/develop/background-work/services/fgs/service-types)

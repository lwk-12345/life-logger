# Life Logger

[English](README.md) | [简体中文](README_CN.md)

**A local-first, privacy-respecting personal life logging system powered by Apple Watch continuous audio capture, local ASR + speaker verification, context-aware LLM activity extraction (v3), and multi-platform dashboards (Web, native macOS, Apple Watch, iOS).**

Life Logger turns all-day ambient voice capture into structured, verifiable daily ledgers — **Expenses**, **Nutrition & Macros**, **Fitness Workouts**, **Health & Sleep**, and a clean **Daily Life Waterfall Timeline** — while separating personal life from computer/AI dictation and background media.

---

## Key Features

### 1. Apple Watch Companion App (`watchos/LifeLoggerWatch`)
- **All-day segmented audio capture**: 16 kHz mono AAC (`48 kbps`) continuous background recording with resilient local spooling and automatic chunked upload to your Mac over LAN/Wi-Fi.
- **HealthKit integration**: Automatically attaches heart rate, step count, and sleep duration snapshots alongside uploaded sessions.
- **Auto-renewing signing helper**: `scripts/build_signed_watch_app.sh` supports automatic Xcode `-allowProvisioningUpdates` for personal Apple developer profiles.

### 2. Local-First Processing Pipeline (`src/life_logger/`)
- **Speech-to-Text & Speaker Attribution**: Pluggable ASR pipeline (MOSS / FunASR / Whisper / MLX) with CAM++ voiceprint verification (`self` vs. `other` vs. `media`) and automatic decoding-loop repetition protection.
- **Context-Aware Activity Extraction v3 (`activity_prompt_v3.py`)**:
  - Processes rolling conversation windows instead of fragile single-sentence rules.
  - Strictly distinguishes **personal life events**, **computer/IDE/AI dictation** (folded into `编程·工作`), and **background TV/video/podcasts** (folded into `媒体内容`), while keeping **WeChat / communication speech** visible in your life timeline.
  - Handles real-world speech corrections (e.g., changing your mind mid-purchase or reporting an item's price later the same day).
- **Single Source of Truth Ledgers (`day_facts.py`)**:
  - **Expense Ledger**: Separates confirmed payments from unconfirmed mentions/quotes; never invents amounts or merges distinct purchases silently.
  - **Nutrition & Macro Ledger**: Parses spoken meal weights, raw rice conversion, and aloud nutrition-facts labels (`kJ/kcal`, `NRV%`); supports inline weight/macro editing and custom brand-library persistence; **never guesses calories when portion weight is missing** (marks as pending for review instead).
  - **Fitness & Health Ledger**: Tracks structured workout sets/reps/weights with recap-first reconciliation, plus Watch HealthKit sleep/heart rate/steps.
  - **Judgment Queue (`/api/judgment-queue`)**: Ambiguous items (unweighed meals, unconfirmed prices, possible duplicates) go into an interactive review queue rather than silently corrupting your ledger.

### 3. Multi-Platform Interfaces
- **Web Waterfall v2 (`web/waterfall-v2.html` & `web/index.html`)**:
  - Clean 3-column daily overview (**Expense**, **Nutrition**, **Health & Fitness**) + hour-aligned vertical timeline with dual recording/scene indicators.
  - **Visual Macro & Calorie Target Bars**: Anchor your nutrition goals to your own **body weight** and **daily resting expenditure (kcal)** with one-click presets for **Cut**, **Maintain/Recomp**, and **Lean Bulk**, plus inline meal weight and per-100g label editing.
  - **Noise-free `我的生活` View**: Separates personal life events from computer-directed speech (`编程·工作`), media (`媒体内容`), and unverified sources (`来源待确认`).
- **Native macOS Desktop & Menu Bar App (`desktop/LifeLoggerMac`)**: SwiftUI desktop viewer and menu bar widget for quick daily checks.
- **iOS / Mobile & Hardware Receivers (`ios/LifeLoggerReceiver`, `mobile/life_logger_receiver`, `desktop/AirPassportReceiver`)**: Companion sync receivers for mobile and BLE hardware recorders.

---

## Architecture Overview

```
┌───────────────────────────┐     LAN / Wi-Fi Upload      ┌────────────────────────────────────┐
│  Apple Watch App (watchOS)│ ──────────────────────────► │   Mac Local Web + Worker Server    │
│  • 16kHz 48kbps AAC chunks│   (Audio + HealthKit + GPS) │   • watch_stream / watch_upload    │
│  • Local spool & retry    │                             │   • ASR + CAM++ Voiceprint         │
└───────────────────────────┘                             │   • Activity Extraction v3 (LLM)   │
                                                          │   • Single-Source Ledgers (4x)     │
┌───────────────────────────┐     HTTP / JSON API         └─────────────────┬──────────────────┘
│  Web UI (Waterfall v2)    │ ◄─────────────────────────────────────────────┤
│  macOS App (LifeLoggerMac)│                                               │
│  iOS / Mobile Receivers   │ ◄─────────────────────────────────────────────┘
└───────────────────────────┘
```

All personal audio, transcripts, credentials, and daily ledgers live exclusively on your local Mac inside `output/` and `local-config.json` (both excluded by `.gitignore`).

---

## Repository Structure

```text
life-logger/
├── src/life_logger/             # Core Python backend, ASR, v3 activity extractor, ledgers, HTTP server
├── web/                         # Web UI (Waterfall v2 timeline, overview cards, review drawer)
├── watchos/LifeLoggerWatch/     # Apple Watch SwiftUI recorder & HealthKit sync app
├── desktop/LifeLoggerMac/       # Native macOS SwiftUI desktop & menu bar app
├── desktop/AirPassportReceiver/ # macOS BLE receiver for external hardware recorders
├── ios/LifeLoggerReceiver/      # iOS Swift package receiver & sync outbox
├── mobile/life_logger_receiver/ # Flutter mobile receiver companion
├── config/                      # v3 activity prompts & example user profile
├── scripts/                     # Build, watch signing, verification, and ops scripts
├── deploy/                      # launchd templates for macOS background services
├── docs/                        # Architecture notes, ADRs, runbooks
└── tests/                       # Python (pytest) & Frontend (node --test) test suites
```

---

## Quick Start (macOS)

### Prerequisites
- macOS (Apple Silicon recommended for local ASR)
- Python 3.11+ (`uv` or `venv`)
- Node.js 20+ (for frontend tests)
- Xcode 15+ & `xcodegen` (only required if building the Apple Watch or macOS apps)

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/lwk-12345/life-logger.git
cd life-logger

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure Locally

```bash
cp local-config.example.json local-config.json
cp config/user-profile.example.md config/user-profile.md
```

Edit `local-config.json` to point to your local ASR/VAD model paths and your chosen LLM endpoint (OpenAI-compatible / DeepSeek / Ollama local LLM).

### 3. Start the Local Web Server

```bash
python3 -m life_logger.local_web --host 127.0.0.1 --port 8765
```

Open **http://127.0.0.1:8765** in your browser (or launch `web/waterfall-v2.html` directly through the local server).

> Want to preview the UI immediately without audio hardware? See [`docs/demo-mode.md`](docs/demo-mode.md).

### 4. Build & Install the Apple Watch App (Optional)

1. Set your own Apple Development Team ID in `watchos/LifeLoggerWatch/project.yml` (`DEVELOPMENT_TEAM`) or export `APPLE_TEAM_ID`.
2. Build and sign with HealthKit entitlements:

```bash
export APPLE_TEAM_ID="YOUR_10_CHAR_TEAM_ID"
bash scripts/build_signed_watch_app.sh
```

3. Install onto your paired Apple Watch (ensure Watch and Mac are on the same Wi-Fi, or iPhone is connected via USB):

```bash
xcrun devicectl list devices
xcrun devicectl device install app --device YOUR_WATCH_DEVICE_ID \
  watchos/LifeLoggerWatch/build/SignedDerivedData/Build/Products/Debug-watchos/LifeLoggerWatch.app
```

### 5. Build the Native macOS App (Optional)

```bash
cd desktop/LifeLoggerMac
swift build
swift test
```

---

## Running Tests

```bash
# Frontend semantics & UI tests
node --test tests/waterfall-v2-semantics.test.mjs

# Backend Python tests
.venv/bin/python -m pytest -q tests/test_food_nutrition.py tests/test_expense_policy.py

# WatchCore Swift package tests
cd watchos/LifeLoggerWatch/CorePackage && swift test
```

---

## Privacy & Local-First Principles

- **Zero mandatory cloud storage**: Audio files, transcripts, voiceprints, and daily ledgers stay on your own Mac (`output/`).
- **Strict `.gitignore` guards**: `local-config.json`, `config/user-profile.md`, `output/`, `*secrets*.json`, `*.mobileprovision`, and `golden/` are all ignored by default.
- **Explicit uncertainty over hallucination**: Missing meal weights, unverified purchase prices, and ambiguous speakers are surfaced honestly for confirmation instead of silently guessing `0` or fabricating facts.

---

## License

Released under the [MIT License](LICENSE).

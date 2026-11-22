# Project: Life Logger Ambient Intelligence Backend Extensions

## Architecture
The Life Logger ambient intelligence backend is extended with a robust multi-module architecture:
1. **Audio Ingest & Pre-filtering**:
   - `watch_live.py` and `scripts/attribute_funasr_transcript.py` calculate sliding-frame energy/RMS. Silent frames (floor -45 dBFS, speech -38 dBFS, <250ms duration) bypass FunASR and downstream LLM inference, immediately generating `_NoSpeechPreview()`.
   - `watch_stream.py` captures `X-LifeLogger-SSID` and `X-LifeLogger-BSSID` headers, invoking `ambient_anchors.py` to map ambient location (Home, Office, Transit, Outside).
   - `speaker_registry.py` provides multi-tier speaker voiceprint profiles (Primary Speaker, Frequent Contacts, Raw Clusters) extending single-template CAM++ attribution.
2. **Intent Pool & Multi-Domain Schema**:
   - `intent_pool.py` manages persistent `active-intents.json`, registering unresolved planned utterances (e.g. 20:35 "要去买瓶水") with lifecycle `pending -> resolved | dismissed | expired`.
   - `domain.py` decouples life tracking into four explicit schemas: `Finance`, `Diet`, `Todos`, `Insights`.
3. **Natural Correction & Hotword Loop**:
   - `natural_correction.py` implements hotword priority detection ("记一笔", "记录一下", "记账", "记个待办") elevating events to `capture_mode: "active_hotword"` / high confidence.
   - DeepSeek parser classifies intent into `ADD` (supplement with `source_type: "user_supplement"` and verbatim quote), `UPDATE` (modify), or `DELETE` (reject/dismiss), with strict amount grounding.
   - `local_web.py` exposes `POST /api/events/natural-correction` and triggers `build_daily_summary` recomputation.
4. **OpenClaw WeChat Evening Bill Alignment Bridge**:
   - `openclaw_bridge.py` builds `evening-bill-alignment-v1` payload via `build_evening_alignment_payload`, dispatches reconciliation cards to local daemon (`http://127.0.0.1:8791`), and handles WeChat auto-resolution callback `POST /api/openclaw/reconciliation-callback`.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| F1 | Hotword Elevation | utterances containing "记一笔", "记录一下", "记账", "记个待办" elevate to `capture_mode: "active_hotword"` / high confidence | M3 | R1 |
| F2 | Natural Correction API | `POST /api/events/natural-correction` supporting spoken/text correction with ADD/UPDATE/DELETE | M3 | R1 |
| F3 | DeepSeek Tri-Intent Parsing | Classifies intent into ADD/UPDATE/DELETE with strict amount grounding (`_deepseek_amount_is_grounded`) | M3 | R1 |
| F4 | User Supplement Grounding | Supplements saved via `save_manual_event` with `source_type: "user_supplement"` and verbatim quote | M3 | R1 |
| F5 | Store & Summary Recomputation | Integrates with `save_event_correction`/`save_manual_event` and recomputes `daily_summary.json` | M3 | R1 |
| F6 | Ingest RMS Silence VAD | Sliding-window RMS pre-screening (-45 dBFS floor, -38 dBFS speech threshold, 250ms duration) | M1 | R2 |
| F7 | ASR/LLM Silence Bypass | Silent audio chunks bypass Apple Silicon MPS FunASR and downstream LLM inference | M1 | R2 |
| F8 | Persistent Active Intent Pool | Persistent `active-intents.json` pool registering planned intents across chunks (e.g. 20:35 "要去买水") | M2 | R3 |
| F9 | Cross-Batch Intent Resolution | Automatically resolves pending intent when supplement or subsequent recording supplies outcome (e.g. 4.9元水) | M2 | R3 |
| F10 | Multi-Domain Schema Decoupling | Decouples life tracking into Finance, Diet, Todos, and Insights | M2 | R3 |
| F11 | Evening Alignment Payload Builder | `build_evening_alignment_payload` aggregates confirmed expenses, candidate expenses, and active intents | M4 | R4 |
| F12 | OpenClaw Daemon Dispatch | Dispatches interactive card to local daemon `http://127.0.0.1:8791` for WeChat forwarding | M4 | R4 |
| F13 | WeChat Callback Auto-Resolution | Receives user WeChat responses to auto-resolve open bills via callback endpoint | M4 | R4 |
| F14 | Network & Environment Anchors | Ingest captures Wi-Fi SSID/BSSID and maps ambient context (Home, Office, Transit, Outside) | M1 | R5 |
| F15 | Resident Speaker Voiceprints | Speaker profile registry for primary speaker, frequent contacts (friends/family), and raw clusters | M1 | R5 |
| F16 | Historical 2026-10-02 Benchmark | 3 confirmed transactions (140, 16, 9 CNY) intact; 20:35 intent registered; 4.90 CNY supplement closes water purchase | M5 | Benchmark |
| F17 | Zero Regression on Existing Suites | All 398 existing backend tests (`tests/test_local_web.py`, `tests/test_contextual_pipeline.py`, etc.) pass cleanly | M5 | Regression |
| F18 | E2E Testing Suite (Tiers 1-4) | Comprehensive opaque-box test suite for F1-F17 published via `TEST_READY.md` | E2E Track | Testing |
| F19 | Adversarial Coverage Hardening | Tier 5 adversarial testing with Challenger loop | M6 | Testing |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| E2E | E2E Testing Track | Requirement-driven opaque-box test suite (Tiers 1-4) & `TEST_READY.md` | Survey | PLANNED |
| M1 | Audio VAD Pre-filter & Ambient Anchors | Features F6, F7, F14, F15: VAD pre-screening, FunASR MPS bypass, network anchors, speaker profiles | Survey | PLANNED |
| M2 | Active Intent Pool & Multi-Domain Decoupling | Features F8, F9, F10: persistent `active-intents.json`, lifecycle matching, domain schemas | Survey | PLANNED |
| M3 | Natural Correction & Hotword Priority | Features F1, F2, F3, F4, F5: `POST /api/events/natural-correction`, hotword detection, summary recomputation | M2 | PLANNED |
| M4 | OpenClaw Evening Bill Alignment Bridge | Features F11, F12, F13: `build_evening_alignment_payload`, daemon dispatch, WeChat callback | M2, M3 | PLANNED |
| M5 | 2026-10-02 Benchmark & Regression Verification | Features F16, F17: Historical benchmark verification, zero regressions on existing suites | M1, M2, M3, M4 | PLANNED |
| M6 | Final Milestone: 100% E2E Pass & Hardening | Features F18, F19: Phase 1 pass 100% E2E tests, Phase 2 adversarial coverage hardening | E2E, M5 | PLANNED |

## Interface Contracts

### 1. Ingest VAD (`src/life_logger/audio_vad.py`)
```python
def calculate_audio_energy(audio_path_or_bytes: str | bytes, sample_rate: int = 16000) -> dict:
    """Returns {is_speech: bool, mean_rms_dbfs: float, speech_frames: int, duration_sec: float}"""

def should_skip_transcription(audio_path: str, silence_threshold_dbfs: float = -45.0, speech_threshold_dbfs: float = -38.0) -> bool:
    """Determines whether audio segment can bypass FunASR/MPS transcription."""
```

### 2. Ambient Anchors & Speaker Registry (`src/life_logger/ambient_anchors.py`, `speaker_registry.py`)
```python
def classify_ambient_location(ssid: str | None, bssid: str | None) -> str:
    """Returns 'Home' | 'Office' | 'Transit' | 'Outside'."""

class ResidentSpeakerRegistry:
    def identify_speaker(self, embedding_or_feature: Any) -> dict:
        """Returns {speaker_id: str, label: 'primary' | 'contact' | 'cluster', name: str | None, confidence: float}"""
```

### 3. Active Intent Pool (`src/life_logger/intent_pool.py`)
```python
class ActiveIntentPool:
    def register_intent(self, text: str, domain: str, target_entity: str, timestamp: str, source_segment_id: str | None = None, metadata: dict | None = None) -> dict:
        """Registers intent with status 'pending' into active-intents.json."""
        
    def find_matching_intents(self, domain: str, target_entity: str | None = None) -> list[dict]:
        """Finds unresolved matching intents."""
        
    def resolve_intent(self, intent_id: str, resolved_by_event_id: str, resolution_note: str | None = None) -> dict:
        """Transitions intent to 'resolved'."""
```

### 4. Natural Correction API (`src/life_logger/natural_correction.py`, `local_web.py`)
```python
# POST /api/events/natural-correction
# Request: {"text": str, "date": str, "job_id": str | None, "artifact_path": str | None}
# Response: {"status": "ok", "action": "ADD" | "UPDATE" | "DELETE", "event": dict | None, "intent_resolution": dict | None, "capture_mode": "active_hotword" | "standard"}
```

### 5. OpenClaw Bridge Alignment (`src/life_logger/openclaw_bridge.py`)
```python
def build_evening_alignment_payload(date_str: str, confirmed_events: list[dict], candidate_events: list[dict], active_intents: list[dict]) -> dict:
    """Returns evening-bill-alignment-v1 structured schema."""

class OpenClawBridgeClient:
    def dispatch_evening_alignment(self, payload: dict) -> dict:
        """Dispatches card to http://127.0.0.1:8791/v1/reconciliation/evening-alignment."""
```

## Code Layout
- `src/life_logger/audio_vad.py`: Audio energy & VAD calculation (M1 exclusive)
- `src/life_logger/ambient_anchors.py`: Network/Wi-Fi ambient classification (M1 exclusive)
- `src/life_logger/speaker_registry.py`: Resident speaker profile registry (M1 exclusive)
- `src/life_logger/watch_live.py`: Audio ingest hooks for VAD and headers (M1 exclusive)
- `src/life_logger/intent_pool.py`: `active-intents.json` pool management (M2 exclusive)
- `src/life_logger/domain.py`: Multi-domain schemas (Finance, Diet, Todos, Insights) (M2 exclusive)
- `src/life_logger/contextual_extraction.py`: Planned intent extraction into pool (M2 exclusive)
- `src/life_logger/natural_correction.py`: Hotword detection and DeepSeek tri-intent parser (M3 exclusive)
- `src/life_logger/local_web.py`: Routing for natural correction and callback endpoints (M3, M4)
- `src/life_logger/openclaw_bridge.py`: Evening alignment payload and dispatch (M4 exclusive)
- `tests/test_audio_vad.py`: Unit tests for VAD pre-screening (M1 / E2E)
- `tests/test_ambient_anchors.py`: Unit tests for network and speaker profiles (M1 / E2E)
- `tests/test_intent_pool.py`: Unit tests for active intent pool and domain schemas (M2 / E2E)
- `tests/test_natural_correction.py`: Unit tests for hotword and correction API (M3 / E2E)
- `tests/test_openclaw_evening_alignment.py`: Unit tests for evening bridge (M4 / E2E)
- `tests/test_historical_20261002_benchmark.py`: Integration test for 2026-10-02 benchmark (M5 / E2E)
- `tests/e2e/`: Opaque-box E2E test suite (E2E Track)

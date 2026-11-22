# Laya decision provider

Life Logger can use either TypeSafe Jev or the open-source Laya System-1 model as
the optional typed-decision enhancement after ASR and speaker attribution.

## Why Laya is a separate local service

Laya depends on PyTorch/Transformers and downloads model weights on first use.
Life Logger therefore does **not** install it into the Fun-ASR worker virtual
environment. The decision layer talks to Laya through its official
Jev-compatible localhost HTTP endpoint:

- health: `GET /health`
- inference: `POST /v1/systemone`

This keeps ASR dependencies isolated and lets Jev and Laya share the same
Life Logger decision contract.

## Install and run

Use a separate virtual environment:

~~~bash
python3 -m venv .venv-laya
.venv-laya/bin/pip install "laya[serve]"
~~~

For Chinese/multilingual Life Logger transcripts, preload the multilingual
checkpoint:

~~~bash
LAYA_HOST=127.0.0.1 \
LAYA_PORT=8000 \
LAYA_DEVICE=auto \
LAYA_PRELOAD=1 \
LAYA_MODELS=multilingual \
.venv-laya/bin/laya-serve
~~~

The first startup may download model weights. Verify it independently:

~~~bash
curl -fsS http://127.0.0.1:8000/health
~~~

Expected shape includes `status: "ok"`, the loaded checkpoint list and device.

Life Logger intentionally accepts only `localhost` / `127.0.0.1` Laya
endpoints. The transcript decision text does not need to leave the machine.

## Life Logger configuration

Optional `local-config.json` section:

~~~json
{
  "laya": {
    "base_url": "http://127.0.0.1:8000",
    "model": "multilingual"
  }
}
~~~

The Web settings page exposes a **Decision Provider** selector:

- TypeSafe Jev
- Laya · local open source

The historical `use_jev` processing flag is retained as the compatibility
switch for “decision enhancement enabled”; new jobs additionally persist
`decision_provider: "jev" | "laya"`.

CLI example:

~~~bash
PYTHONPATH=. ~/.local/bin/uv run life-logger process input/recording.m4a \
  --use-jev \
  --decision-provider laya \
  --jev-min-confidence 0.72
~~~

The confidence option keeps its historical name for compatibility. Jev and Laya
confidence values are not assumed to be calibrated identically; tune the
threshold on real Life Logger review data before relying on automatic
acceptance.

## Runtime fallback

Selecting **Laya** remains the persisted user preference. Life Logger resolves the
actual provider when each job starts:

1. if local Laya `/health` is ready, use Laya;
2. if Laya is unavailable but Jev has a usable SDK + API key, keep the requested
   provider as `laya` but run that job with `effective_provider=jev`;
3. if Laya was healthy at startup but fails during the decision stage, retry that
   decision stage once with Jev when Jev is available;
4. if neither provider is ready, fail explicitly instead of pretending the
   enhancement succeeded.

The Web job status stores this separately as `decision_runtime`, including
`requested_provider`, `effective_provider`, `fallback` and `fallback_reason`.
The settings preference is never silently rewritten from Laya to Jev.

The settings page also exposes the same state before starting a job: when Laya is
down and Jev is ready it shows **Laya 未启动 · 将回退 Jev**; when both are down it
shows that neither provider is ready.


## Evidence contract

Laya remains a bounded decision layer. It receives the existing
`self-transcript.json` text and answers two typed choice questions:

1. Life Logger category
2. event state (completed / ongoing / planned / idea / hypothetical / negated /
   question / not_event)

It does not perform ASR and does not generate free-form event text.

Accepted Laya events retain:

- original transcript quote
- recording/segment IDs
- start/end offsets
- `decision_source: "laya_local"`
- `decision_provider: "laya"`

Generic decisions are written to `decision-decisions.json`. Existing Jev jobs
and the legacy `jev-decisions.json` artifact name remain readable.

## Smoke checks

Check the real local service without running a recording:

~~~bash
curl -fsS http://127.0.0.1:8000/health
~~~

You can also inspect both providers using the same code path as the Web settings
page:

~~~bash
PYTHONPATH=src:. ~/.local/bin/uv run python - <<'PY'
import json
from pathlib import Path
from scripts.local_jobs import inspect_jev_environment, inspect_laya_environment

config_path = Path("local-config.json")
config = json.loads(config_path.read_text()) if config_path.exists() else {}
print(json.dumps({
    "laya": inspect_laya_environment(config),
    "jev": inspect_jev_environment(config),
}, ensure_ascii=False, indent=2))
PY
~~~

A stopped Laya service should report `ready: false`; this is not itself a job
failure if Jev is configured and therefore available as the fallback.

## Local validation

The repository test suite includes a real localhost HTTP fixture using the same
`/health` and `/v1/systemone` JSON shapes. It validates request fields,
provider health probing and a full evidence-backed LifeEvent enrichment without
inventing production recordings.

~~~bash
PYTHONPATH=. ~/.local/bin/uv run pytest tests/test_decision_provider.py -q
~~~

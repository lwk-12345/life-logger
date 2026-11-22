# P5-C end-to-end acceptance checklist

Use this checklist from the repository root. The goal is to verify one local-first
path across doctor, Web/mobile, capture, and push without exposing Life Logger to
the public Internet.

## A. Quality gates

- [ ] `PYTHONPATH=. uv run pytest` passes.
- [ ] `uv run ruff check .` reports zero errors.
- [ ] `node --check web/app.js` passes.
- [ ] `node --check web/viewer.js` passes.

## B. Doctor and local Web

- [ ] Start/restart `com.life-logger.local-web` or run the local Web launcher.
- [ ] `uv run life-logger doctor --json` reports `ok=true`.
- [ ] With **设置 → 移动访问 → 局域网访问** off, the service listens only on
      `127.0.0.1:8765`.
- [ ] Anonymous `GET /api/health` returns 401.
- [ ] With the local token, `GET /api/mobile-capture` returns
      `transport=same-origin-job-upload`, `requires_token=true`, and
      `public_upload=false`.

## C. Mobile narrow-screen path

- [ ] Open the paired Life Logger URL on a phone or browser viewport ≤620px.
- [ ] **今日简报** is readable with no horizontal scroll.
- [ ] **去待复核** opens the review queue and one existing review can be confirmed.
- [ ] **发送今日简报** reports webhook success, local outbox fallback, or the
      explicit “not configured” state.
- [ ] **运行系统自检** reports success inline; if doctor has a failure, the UI
      opens **设置 → 系统自检**.
- [ ] Under **导入与处理**, **手机本地捕获** is visible when enabled.
- [ ] Record/select one small audio file. With auto-start off it appears in the
      existing selected-file list and does not start until confirmation.
- [ ] After starting, the file enters the ordinary Life Logger job pipeline; no
      new public URL or third-party upload destination is created.
- [ ] Toggle **显示捕获入口** / **选完后自动开始处理**, refresh the page, and
      confirm the settings persist in `output/local-web/preferences.json`.

## D. PWA browser surface

- [ ] `web/manifest.webmanifest` uses a same-origin `/` start URL/scope.
- [ ] Mobile access still uses the existing authenticated Web service; there is no
      separate native-shell build step in the self-use workflow.

## E. Daily brief outbox isolation

- [ ] Generate a dated brief with `life-logger brief --outbox <local-dir>`.
- [ ] The dated `daily-brief-v1` JSON is written to the chosen local directory.
- [ ] The outbox payload contains summary/events metadata only; it contains no raw
      audio bytes/path.
- [ ] Removing or pausing the external Agent that watches the directory does not
      affect ASR, mobile capture, review, or the local Web service.
- [ ] If `--webhook` is used, it points to an endpoint you already operate; the
      outbox remains enabled as the auditable fallback.

## F. Final network boundary

- [ ] LAN/private-network access remains disabled by default after a fresh
      preferences file.
- [ ] If remote phone access is needed, use only the explicit LAN switch or a
      validated RFC1918/Tailscale bind.
- [ ] Do not port-forward `8765`; do not add cloud ASR/LLM/upload dependencies to
      complete this checklist.

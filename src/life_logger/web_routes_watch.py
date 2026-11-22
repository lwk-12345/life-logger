"""从 _Handler 机械提取的路由，分支体逐字不变。local_web 的名字在调用时才导入，避免循环依赖。"""
from __future__ import annotations

from datetime import timedelta
from datetime import timezone
from http import HTTPStatus
from life_logger.decision_history import read_kev_section
from life_logger.passport_stream import CAPTURE_SOURCE
from life_logger.passport_stream import list_passport_sessions
from life_logger.passport_sync import deck_to_packets_hex
from life_logger.passport_sync import extract_passport_cards
from life_logger.web_common import _read_contract
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import unquote
from zoneinfo import ZoneInfo
import json
import hmac
import secrets


class _WatchRoutesMixin:
    def _get_watch_health(self, parsed):
        # [2026-10-14 fix · codex P1] Discovery hardening.
        # This probe used to be fully credential-free on purpose (old Watch builds
        # must still learn "a Mac is here"). But the Watch selects its upload target
        # from this response (watch_upload_enabled), and any LAN host could have
        # answered it. Now watch_upload_enabled=true ONLY when the probe carries the
        # embedded watch key (X-LifeLogger-Watch-Key) matching this Mac's value:
        # spoofed hosts never see the key, so discovery cannot pick them. Legacy
        # builds without the header still get ok:true — they can tell the Mac exists
        # — but won't select it for upload, which is the compatible compromise.
        supplied_key = str(self.headers.get("X-LifeLogger-Watch-Key") or "")
        expected_key = str(getattr(self.app, "watch_upload_key", "") or "")
        key_ok = bool(
            expected_key
            and supplied_key
            and secrets.compare_digest(supplied_key.encode("utf-8", errors="replace"), expected_key.encode("utf-8"))
        )
        # 【2026-10-22 P-31】字段语义写清：`watch_upload_enabled` 不是
        # "这台 Mac 能不能收上传"的全局状态，而是**握手能力位**——
        # 只有当探活请求带了本机嵌入的 `X-LifeLogger-Watch-Key` 且匹配时为 true，
        # 目的是防止局域网里任何一台机器冒充 Mac 被手表选中。
        # 所以 `curl /api/watch-health`（不带 key）拿到 false 是**预期行为**，
        # 不代表上传被关闭。下面把三个分量一并暴露，避免再次误读。
        upload_capable = bool(self.app.watch_local_access_enabled)
        # V67 起探活不发密钥：手表发随机数，这里用密钥算 HMAC 证明自己是这台 Mac。
        # 旧版手表仍带 X-LifeLogger-Watch-Key，继续走上面的判断。
        challenge = str(self.headers.get("X-LifeLogger-Watch-Challenge") or "")[:128]
        challenge_response = (
            hmac.new(expected_key.encode("utf-8"), challenge.encode("utf-8"), "sha256").hexdigest()
            if challenge and expected_key
            else None
        )
        self._send_json(
            {
                "ok": True,
                "watch_upload_enabled": bool(upload_capable and (key_ok or challenge_response)),
                "watch_challenge_response": challenge_response,
                "watch_upload_semantics": "handshake_capability_bit",
                "watch_upload_note": (
                    "true 仅在请求带匹配的 X-LifeLogger-Watch-Key 时返回；"
                    "不带该头的探活（如 curl）一律 false，但不代表上传被关闭"
                ),
                "watch_local_access_enabled": upload_capable,
                "watch_key_present": bool(supplied_key),
                "watch_key_matched": bool(key_ok),
            }
        )
        return

    def _get_watch_live_events(self, parsed):
        from life_logger.local_web import (
            datetime,
        )

        try:
            query_params = parse_qs(parsed.query)
            target_date = (query_params.get("date") or [None])[0]
            results = []
            bj_tz = timezone(timedelta(hours=8))
            for manifest_path in self.app.watch_session_store.root.glob("*/*/manifest.json"):
                sess_dir = manifest_path.parent
                dev_id = sess_dir.parent.name
                sess_id = sess_dir.name
                try:
                    m = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                events_path = sess_dir / "events.json"
                live_events = []
                if events_path.is_file():
                    try:
                        raw_events = json.loads(events_path.read_text(encoding="utf-8"))
                        if isinstance(raw_events, list):
                            live_events = raw_events
                    except Exception:
                        pass

                from life_logger.watch_formal_asr import is_hallucinated_asr_noise
                formal_asr_dir = sess_dir / "formal-asr" / "segments"
                self_transcripts = []
                if formal_asr_dir.is_dir():
                    for st_path in sorted(formal_asr_dir.glob("*/state.json")):
                        try:
                            st = json.loads(st_path.read_text(encoding="utf-8"))
                            if st.get("status") == "completed":
                                for r in st.get("rows", []):
                                    if isinstance(r, dict) and r.get("text"):
                                        txt = str(r.get("text") or "").strip()
                                        if not txt or is_hallucinated_asr_noise(txt):
                                            continue
                                        src = str(r.get("source_label") or r.get("speaker") or "")
                                        if src in {"self", ""}:
                                            self_transcripts.append(r)
                        except Exception:
                            pass

                if not live_events and not self_transcripts:
                    continue

                sa = str(m.get("started_at") or "")
                sess_date = sa[:10]
                try:
                    sess_dt = datetime.fromisoformat(sa).astimezone(bj_tz)
                    sess_date = sess_dt.strftime("%Y-%m-%d")
                except Exception:
                    pass

                event_dates = {sess_date}
                for ev in live_events:
                    t = (
                        ev.get("time", {}).get("absolute")
                        or ev.get("evidence", {}).get("reported_at")
                        or ev.get("evidence", {}).get("occurrence_time_evidence")
                    )
                    if t:
                        try:
                            edt = datetime.fromisoformat(str(t)).astimezone(bj_tz)
                            event_dates.add(edt.strftime("%Y-%m-%d"))
                        except Exception:
                            pass

                if target_date and target_date not in event_dates:
                    continue

                results.append({
                    "device_id": dev_id,
                    "session_id": sess_id,
                    "started_at": sa,
                    "session_date": sess_date,
                    "event_dates": sorted(list(event_dates)),
                    "state": m.get("state", "receiving"),
                    "events": live_events,
                    "self_transcripts": self_transcripts,
                })
            self._send_json(
                {
                    "ok": True,
                    "sessions": results,
                    "read_contract": _read_contract(
                        "diagnostic_raw",
                        "raw",
                        canonical=False,
                        date_basis="raw session/event reported time, not canonical occurrence date",
                        notice="Diagnostic live-session payload; use /api/stats, /api/daily or /api/daily-brief for canonical accounting.",
                    ),
                }
            )
        except Exception as ex:
            import traceback
            traceback.print_exc()
            self._send_json({"ok": False, "error": str(ex)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        return

    def _get_watch_reception(self, parsed):
        from life_logger.local_web import (
            _nonnegative_int,
            datetime,
        )

        all_sessions = self.app.watch_session_store.list_session_summaries(limit=None)
        sessions = all_sessions[:50]
        # [2026-10-23 P0] 31 条起就走轻量快路径，避免 31–40 的断崖；
        # total/returned/truncated 明确告诉上层有没有截断。
        if len(all_sessions) > 30:
            payload, cached = self.app.control_snapshot_meta(
                "watch-reception-fast",
                lambda: self.app._fast_watch_reception_payload(sessions),
                ttl=5.0,
                wait_timeout=1.0,
            )
            payload = {
                **payload,
                "total_sessions": len(all_sessions),
                "returned_sessions": len(sessions),
                "truncated": len(all_sessions) > len(sessions),
                "cached": cached,
            }
            self._send_json(payload)
            return
        recent_asr_summaries: dict[tuple[str, str], dict[str, Any] | None] = {}
        for item in sessions:
            # Watch recordings have one formal ASR trail; never surface the old
            # provisional segment-preview payload in the product API.
            item.pop("preview", None)
            try:
                segment_asr = self.app.watch_formal_asr.summary_for_session(
                    str(item.get("device_id") or ""),
                    str(item.get("session_id") or ""),
                )
            except (KeyError, ValueError, OSError, TypeError):
                segment_asr = None
            recent_asr_summaries[
                (str(item.get("device_id") or ""), str(item.get("session_id") or ""))
            ] = segment_asr
            segment_asr_checkpoint_count = 0
            if isinstance(segment_asr, dict):
                checkpoint_count = segment_asr.get("checkpoint_count")
                if isinstance(checkpoint_count, int) and not isinstance(checkpoint_count, bool):
                    segment_asr_checkpoint_count = max(0, checkpoint_count)
                counts = segment_asr.get("counts")
                if isinstance(counts, dict) and sum(
                    value for value in counts.values()
                    if isinstance(value, int) and not isinstance(value, bool)
                ):
                    item["formal_asr"] = {
                        "status": str(segment_asr.get("status") or "processing"),
                        "counts": {
                            key: max(0, value)
                            for key, value in counts.items()
                            if key in {"pending", "processing", "completed", "no_speech", "failed"}
                            and isinstance(value, int)
                            and not isinstance(value, bool)
                        },
                        "transcript_count": max(0, int(segment_asr.get("transcript_count") or 0)),
                        "latest_transcript_rows": segment_asr.get("transcripts", [])[-8:],
                        "current_segment": segment_asr.get("current_segment"),
                        "latest_completed_segment": segment_asr.get("latest_completed_segment"),
                        "queued_until": segment_asr.get("queued_until"),
                        "total_segments": segment_asr.get("total_segments"),
                    }
                    retry = segment_asr.get("automatic_retry")
                    if isinstance(retry, dict):
                        failed_segments = retry.get("failed_segments")
                        waiting_segments = retry.get("waiting_segments")
                        next_retry_in = retry.get("next_retry_in_seconds")
                        if (
                            isinstance(failed_segments, int)
                            and not isinstance(failed_segments, bool)
                            and failed_segments > 0
                            and isinstance(waiting_segments, int)
                            and not isinstance(waiting_segments, bool)
                            and (
                                next_retry_in is None
                                or (
                                    isinstance(next_retry_in, int)
                                    and not isinstance(next_retry_in, bool)
                                )
                            )
                        ):
                            item["formal_asr"]["automatic_retry"] = {
                                "failed_segments": min(10_000, failed_segments),
                                "waiting_segments": min(
                                    failed_segments, max(0, waiting_segments)
                                ),
                                "next_retry_in_seconds": (
                                    min(1800, max(0, next_retry_in))
                                    if isinstance(next_retry_in, int)
                                    else None
                                ),
                            }
            # A completion job is reserved before formal ASR import begins.
            # Surface that stable ID while the session is still waiting for
            # its final segments so the user can follow the same task.
            job_id = str(
                item.get("job_id") or item.get("completion_job_id") or ""
            )
            if not job_id:
                item["formal_job"] = None
                continue
            try:
                job = self.app.store.read(job_id)
                formal_job = {
                    "job_id": job_id,
                    "status": str(job.get("status") or "unknown"),
                    "stage": str(job.get("current_stage") or ""),
                }
                # Keep the reception API limited to a safe artifact locator.
                # The transcript body stays behind the existing viewer route,
                # and the path must come from this Job's result manifest.
                if formal_job["status"] == "completed":
                    job_results = job.get("results")
                    if isinstance(job_results, list):
                        for result_path in job_results:
                            if not isinstance(result_path, str) or "\\" in result_path:
                                continue
                            raw_parts = result_path.split("/")
                            path_parts = Path(result_path).parts
                            if (
                                len(path_parts) < 4
                                or raw_parts != list(path_parts)
                                or path_parts[0] != "files"
                                or not path_parts[1]
                                or path_parts[2] != "result"
                                or path_parts[-1] != "transcript.json"
                                or any(part in {"", ".", ".."} for part in path_parts)
                                or Path(result_path).is_absolute()
                            ):
                                continue
                            job_dir = self.app.store.job_path(job_id).resolve()
                            artifact = (job_dir / result_path).resolve()
                            if job_dir in artifact.parents and artifact.is_file():
                                formal_job["transcript_artifact_path"] = result_path
                                break
                job_files = [
                    file_item
                    for file_item in (job.get("files") or [])
                    if isinstance(file_item, dict)
                ]
                no_speech_files = [
                    file_item
                    for file_item in job_files
                    if file_item.get("outcome") == "no_speech"
                ]
                if no_speech_files:
                    formal_job["no_speech_count"] = len(no_speech_files)
                    formal_job["file_count"] = len(job_files)
                    all_files_have_no_speech = (
                        bool(job_files) and len(no_speech_files) == len(job_files)
                    )
                    formal_job["no_speech_reason"] = (
                        "所有文件均无语音；原音频已保留，跳过声源归属与事件提取。"
                        if all_files_have_no_speech
                        else (
                            "部分文件无语音；这些文件的原音频已保留并跳过后处理，"
                            "其余文件未因无语音而跳过，处理状态见任务详情。"
                        )
                    )
                retry = job.get("automatic_retry")
                if (
                    job.get("status") == "failed"
                    and isinstance(retry, dict)
                    and isinstance(retry.get("attempts"), int)
                ):
                    retry_summary = {
                        "status": "waiting",
                        "attempts": min(1_000_000, max(0, retry["attempts"])),
                        "reason": "ASR 资源繁忙，等待自动重试",
                    }
                    next_attempt_at = retry.get("next_attempt_at")
                    if isinstance(next_attempt_at, str):
                        try:
                            retry_summary["next_attempt_at"] = datetime.fromisoformat(
                                next_attempt_at
                            ).isoformat()
                        except ValueError:
                            pass
                    formal_job["automatic_retry"] = retry_summary
                context_retry = job.get("context_auto_retry")
                if isinstance(context_retry, dict) and context_retry.get("status") in {
                    "scheduled", "running", "completed", "needs_review", "stopped",
                    "manual_override",
                }:
                    context_retry_summary: dict[str, Any] = {
                        "status": context_retry["status"],
                        "attempts": min(
                            1_000_000,
                            _nonnegative_int(context_retry.get("attempts")),
                        ),
                        "max_attempts": min(
                            1_000_000,
                            _nonnegative_int(context_retry.get("max_attempts")),
                        ),
                    }
                    next_attempt_at = context_retry.get("next_attempt_at")
                    if isinstance(next_attempt_at, str):
                        try:
                            context_retry_summary["next_attempt_at"] = (
                                datetime.fromisoformat(next_attempt_at).isoformat()
                            )
                        except ValueError:
                            pass
                    formal_job["context_auto_retry"] = context_retry_summary
                if isinstance(job.get("elapsed_seconds"), (int, float)):
                    formal_job["elapsed_seconds"] = job["elapsed_seconds"]
                extraction_files = sorted(
                    self.app.store.job_path(job_id).glob(
                        "files/*/result/context-extraction.json"
                    )
                )
                extraction_summaries = []
                for extraction_path in extraction_files:
                    try:
                        extraction = json.loads(
                            extraction_path.read_text(encoding="utf-8")
                        )
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(extraction, dict):
                        continue
                    status = extraction.get("status")
                    kev_result = read_kev_section(extraction)
                    counts = extraction.get("counts")
                    if not isinstance(status, str) or status not in {
                        "completed", "degraded", "failed"
                    }:
                        continue
                    extraction_summaries.append(
                        {
                            "status": status,
                            "kev_status": (
                                kev_result.get("status")
                                if isinstance(kev_result, dict)
                                and isinstance(kev_result.get("status"), str)
                                and kev_result.get("status")
                                in {"completed", "degraded", "failed"}
                                else None
                            ),
                            "counts": {
                                key: max(0, counts.get(key, 0))
                                if isinstance(counts, dict)
                                and isinstance(counts.get(key, 0), int)
                                and not isinstance(counts.get(key, 0), bool)
                                else 0
                                for key in (
                                    "formal_events",
                                    "pending_events",
                                    "pending_sources",
                                )
                            },
                        }
                    )
                if extraction_summaries:
                    status_order = {"completed": 0, "degraded": 1, "failed": 2}
                    kev_statuses = [
                        summary["kev_status"]
                        for summary in extraction_summaries
                        if summary["kev_status"] is not None
                    ]
                    formal_job["context_extraction"] = {
                        "status": max(
                            (summary["status"] for summary in extraction_summaries),
                            key=status_order.__getitem__,
                        ),
                        "kev_status": (
                            max(kev_statuses, key=status_order.__getitem__)
                            if kev_statuses
                            else "unknown"
                        ),
                        "counts": {
                            key: sum(
                                summary["counts"][key]
                                for summary in extraction_summaries
                            )
                            for key in (
                                "formal_events",
                                "pending_events",
                                "pending_sources",
                            )
                        },
                    }
                if formal_job["status"] in {
                    "queued", "running", "partial", "degraded",
                    "completed", "failed", "interrupted"
                }:
                    checkpoints = sorted(
                        self.app.store.job_path(job_id).glob(
                            "files/*/asr/checkpoint.json"
                        ),
                        key=lambda path: path.stat().st_mtime,
                    )
                    if checkpoints:
                        try:
                            checkpoint = json.loads(
                                checkpoints[-1].read_text(encoding="utf-8")
                            )
                        except (OSError, json.JSONDecodeError):
                            checkpoint = {}
                        if not isinstance(checkpoint, dict):
                            checkpoint = {}
                        completed = checkpoint.get("completed_windows")
                        total = checkpoint.get("total_windows")
                        if isinstance(completed, int) and isinstance(total, int):
                            formal_job["asr_progress"] = {
                                "completed_windows": max(0, completed),
                                "total_windows": max(0, total),
                                "unit": str(checkpoint.get("progress_unit") or "windows"),
                            }
                        transcripts = checkpoint.get("transcripts")
                        if isinstance(transcripts, list):
                            formal_job["transcript_count"] = len(transcripts)
                            rows = []
                            for transcript in transcripts[-8:]:
                                if not isinstance(transcript, dict):
                                    continue
                                row = {}
                                for key in (
                                    "start_offset_seconds",
                                    "end_offset_seconds",
                                ):
                                    value = transcript.get(key)
                                    if isinstance(value, (int, float)):
                                        row[key] = value
                                text = transcript.get("text")
                                if isinstance(text, str):
                                    row["text"] = text[:1000]
                                if row:
                                    rows.append(row)
                            formal_job["latest_transcript_rows"] = rows
                # Legacy Watch sessions were processed as one full-session Job and
                # have no per-segment ASR checkpoints. Do not present their synthetic
                # all-pending segment summary as live progress after that Job is terminal.
                if (
                    item.get("state") == "complete"
                    and formal_job["status"]
                    in {"completed", "degraded", "partial", "failed", "interrupted"}
                    and segment_asr_checkpoint_count == 0
                ):
                    item.pop("formal_asr", None)
                item["formal_job"] = formal_job
            except (KeyError, ValueError, OSError):
                item["formal_job"] = {"status": "unavailable"}
        # Keep the detail window unchanged; only the full-history aggregate is cached.
        status_summary = self.server._watch_reception_status_summary(recent_asr_summaries)
        from life_logger.watch_stream import DEVICE_STATUS_FRESHNESS_SECONDS
        self._send_json({
            "sessions": sessions,
            "device_statuses": self.app.watch_session_store.list_device_statuses(),
            # 新鲜度口径只在 watch_stream 里定义一次，避免两端各有一套。
            "device_status_freshness_seconds": DEVICE_STATUS_FRESHNESS_SECONDS,
            "status_summary": status_summary,
            "total_sessions": len(all_sessions),
            "returned_sessions": len(sessions),
            "truncated": len(all_sessions) > len(sessions),
            "cached": False,
        })
        return

    def _get_passport_sessions(self, parsed):
        sessions = list_passport_sessions(
            self.app.store.root / "passport-stream"
        )
        for item in sessions:
            if item.get("complete") and item.get("recording_available"):
                item["audio_url"] = (
                    "/api/passport-sessions/audio?"
                    f"device={quote(str(item.get('device') or ''))}&"
                    f"session={quote(str(item.get('session') or ''))}"
                )
        receiver_status = dict(self.app.passport_receiver_status)
        if self.app.passport_receiver_runtime is not None:
            auto_start = receiver_status.get("auto_start")
            receiver_status.update(self.app.passport_receiver_runtime.status())
            if auto_start is not None:
                receiver_status["auto_start"] = auto_start
        self._send_json(
            {
                "capture_source": CAPTURE_SOURCE,
                "device_label": "AI Passport",
                "receiver": receiver_status,
                "sessions": sessions,
            }
        )
        return

    def _get_passport_cards(self, parsed):
        from life_logger.local_web import (
            datetime,
        )

        query = parse_qs(parsed.query)
        date = unquote(query.get("date", [""])[0]) or None
        fmt = unquote(query.get("format", ["json"])[0]).lower()
        openclaw_tasks = None
        try:
            from life_logger.openclaw_bridge import OpenClawBridgeClient
            oc_client = OpenClawBridgeClient()
            if oc_client.is_healthy():
                raw_tasks = oc_client.list_tasks(status="pending")
                target_dt = date or datetime.now(tz=ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
                openclaw_tasks = [
                    t for t in raw_tasks
                    if target_dt in str(t.get("created_at", "")) or target_dt in str(t.get("updated_at", ""))
                ] or raw_tasks[:3]
        except Exception:
            openclaw_tasks = None
        deck = extract_passport_cards(
            self.app.store.root,
            date=date,
            device_status=self.app.passport_receiver_status,
            openclaw_tasks=openclaw_tasks,
        )
        if fmt == "lvgl":
            self._send_json(deck.to_lvgl_deck())
        elif fmt == "packets":
            self._send_json(
                {
                    "deck_id": deck.deck_id,
                    "version_hash": deck.version_hash,
                    "packets_hex": deck_to_packets_hex(deck),
                }
            )
        else:
            self._send_json(deck.to_dict())
        return

    def _post_watch_vip_command(self, parsed):
        try:
            payload = self._body_json()
        except Exception:
            self._send_json({"error": "bad_request", "detail": "请求体不是合法 JSON"}, HTTPStatus.BAD_REQUEST)
            return
        if not payload or not isinstance(payload, dict):
            self._send_json({"error": "bad_request", "detail": "请求体不能为空"}, HTTPStatus.BAD_REQUEST)
            return
        text = str(payload.get("text") or "").strip()
        audio_path = payload.get("audio_path")
        # 这个路由给快捷指令用，同网免令牌。本机文件路径只允许本机调用，否则同网设备能让服务转写任意文件。
        if audio_path and str(self.client_address[0]) not in {"127.0.0.1", "::1"}:
            self._send_json({"error": "forbidden", "detail": "audio_path 只允许本机调用"}, HTTPStatus.FORBIDDEN)
            return
        if not text and not audio_path and not payload.get("pin_id"):
            self._send_json({"error": "bad_request", "detail": "缺少 text、audio_path 或 pin_id"}, HTTPStatus.BAD_REQUEST)
            return
        from life_logger.vip_fast_track import execute_vip_command, transcribe_vip_snippet
        if not text and audio_path:
            text = transcribe_vip_snippet(audio_path)
        res = execute_vip_command(
            text=text,
            timestamp=payload.get("timestamp"),
            pin_id=payload.get("pin_id"),
        )
        if not res.get("ok", True):
            self._send_json(res, HTTPStatus.UNPROCESSABLE_ENTITY)
            return
        self._send_json({"ok": True, **res}, HTTPStatus.OK)
        return

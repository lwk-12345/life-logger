"""Independent processing runtime; no HTTP server is constructed or owned here."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import traceback
import uuid
from zoneinfo import ZoneInfo

from life_logger.runtime_queue import TaskQueue, WorkerLease


def runtime_build_id() -> str:
    package = Path(__file__).resolve().parent
    files = ['processing_runtime.py','day_views.py','runtime_queue.py',
             'local_web.py','event_view.py','expense_policy.py','daily_brief.py','daily_cognitive_session.py']
    digest = hashlib.sha256()
    for name in files:
        digest.update(name.encode())
        digest.update((package/name).read_bytes())
    return digest.hexdigest()


class ProcessingHost:
    """Migration adapter for existing coordinators, without ThreadingHTTPServer.

    Reuse the current orchestration functions rather than fork their policies.
    This object has no socket, request handler, or serve_forever lifecycle.
    """

    def __init__(self, root: Path, config_path: Path, queue: TaskQueue):
        from types import MethodType
        from life_logger.local_web import JobStore, LocalWebServer
        from life_logger.local_reminders import LocalReminderStore
        from life_logger.local_habits import LocalHabitStore
        from life_logger.watch_stream import WatchSessionStore
        from life_logger.watch_formal_asr import WatchFormalASRProcessor
        self.store = JobStore(root, recover=False, process_safe=True, dispatch_queue=queue)
        self.config_path = Path(config_path).resolve()
        self.queue = queue
        self._watch_live_stop = threading.Event()
        self._auto_ingest_stop = threading.Event()
        self._reminder_stop = threading.Event()
        self._watch_formal_scan_lock = threading.Lock()
        self._watch_formal_scan_thread = None
        self._last_formal_asr_invalidate_ts = 0.0
        self._recovery_thread = None
        self.watch_session_store = WatchSessionStore(self.store.root/'watch-sessions', process_safe=True,
                                                     command_dispatcher=self.enqueue_vip)
        self.watch_formal_asr = WatchFormalASRProcessor(self.watch_session_store, self.config_path, self._watch_live_stop)
        self.reminders = LocalReminderStore(self.store.root)
        self.habits = LocalHabitStore(self.store.root)
        from life_logger.activity_timeline_runtime import ActivityTimelineRuntime
        from life_logger.input_context_runtime import InputContextRuntimeManager
        self.activity_timeline = ActivityTimelineRuntime(self.store.root, self.config_path)
        self._activity_timeline_stop = threading.Event()
        self._activity_timeline_last_tick = None
        try:
            self.input_context_manager = InputContextRuntimeManager(self.watch_session_store.root)
        except Exception as error:
            print(f'[processing-worker] 前台采集初始化跳过: {error}', flush=True)
            self.input_context_manager = None
        self._input_context_started = False
        methods = ('_activity_timeline_loop','_watch_live_loop','_schedule_watch_formal_asr_scan','_run_watch_formal_asr_scan',
                   'run_auto_ingest_scan','_auto_ingest_loop','_reminder_loop','run_reminder_dispatch',
                   'sync_local_reminders','_reminder_tasks','_reminder_tasks_with_source','_openclaw_reminder_tasks',
                   '_reminder_event_lookup','filter_reminders_dictation','dashboard_dictation_filter',
                   'apply_dashboard_dictation_filter','build_daily_brief','build_agent_brief')
        for name in methods:
            setattr(self, name, MethodType(getattr(LocalWebServer, name), self))
        self._warm_vip = LocalWebServer._warm_vip_transcriber
        self.threads = []

    def invalidate_event_reads(self, reason):
        """Worker 没有结果缓存；写代际文件后 Web 下次读取自行失效。"""
        from life_logger.local_web import invalidate_read_caches
        invalidate_read_caches(self.store.root, reason)

    def enqueue_vip(self, payload):
        key = str(payload.get('pin_id') or hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest())
        return self.queue.enqueue('vip', 'watch-pin:'+key, payload, priority=0, reuse_finished=True)

    def _start_timeline_and_capture(self):
        """时间轴和前台采集按配置开关启动，与 combined 模式的条件一致。"""
        try:
            config = self.activity_timeline.load_config()
        except Exception as error:
            print(f'[processing-worker] 读取配置失败，时间轴和采集未启动: {error}', flush=True)
            return
        timeline = config.get('activity_timeline') if isinstance(config, dict) else None
        if isinstance(timeline, dict) and timeline.get('enabled') is True:
            thread = threading.Thread(target=self._activity_timeline_loop, name='worker-activity-timeline', daemon=True)
            thread.start()
            self.threads.append(thread)
        capture = config.get('input_context_capture') if isinstance(config, dict) else None
        if self.input_context_manager is not None and isinstance(capture, dict) and capture.get('enabled') is True:
            try:
                self.input_context_manager.start()
                self._input_context_started = True
            except Exception as error:
                print(f'[processing-worker] 前台采集启动失败: {error}', flush=True)

    def start(self, *, warm_vip=True):
        from life_logger.local_web import _env_flag
        self._start_timeline_and_capture()
        for disabled, name, target in [
            ('LIFE_LOGGER_DISABLE_WATCH_LOOP','worker-watch-coordinator',self._watch_live_loop),
            ('LIFE_LOGGER_DISABLE_AUTO_INGEST','worker-auto-ingest',self._auto_ingest_loop),
            ('LIFE_LOGGER_DISABLE_REMINDERS','worker-reminders',self._reminder_loop),
            ('LIFE_LOGGER_DISABLE_VIP_WARMUP','worker-vip-warmup',self._warm_vip),
        ]:
            if name == 'worker-vip-warmup' and not warm_vip:
                continue
            if not _env_flag(disabled):
                thread = threading.Thread(target=target, name=name, daemon=True)
                thread.start()
                self.threads.append(thread)

    def close(self):
        for event in (self._watch_live_stop,self._auto_ingest_stop,self._reminder_stop,self._activity_timeline_stop):
            event.set()
        if self._input_context_started and self.input_context_manager is not None:
            try:
                self.input_context_manager.stop()
            except Exception as error:
                print(f'[processing-worker] 前台采集停止失败: {error}', flush=True)
            self._input_context_started = False
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1)


class ProcessingWorker:
    QUEUE_RETRY_SECONDS=1
    def __init__(self, root: Path, config_path: Path, *, background: bool = True, handlers=None):
        self.root = Path(root).resolve()
        self.config_path = Path(config_path).resolve()
        self.queue = TaskQueue(self.root)
        self.lease = WorkerLease(self.root)
        self.owner = uuid.uuid4().hex
        self.build = runtime_build_id()
        self.host = ProcessingHost(self.root,self.config_path,self.queue)
        self.background = background
        self.handlers = handlers or {}
        self.stop = threading.Event()

    def refresh_catalog(self):
        # Web 直接读源文件，不需要读模型，只要通知它缓存失效。
        self._bump_read_epoch('worker-task')
        return None

    def _bump_read_epoch(self, reason: str) -> None:
        """产物落盘后通知 Web 读进程：跨进程失效，不靠 10s TTL。

        这里是 worker 进程，`invalidate_scan_cache()` 只能清自己；必须写
        store root 下的代际文件，Web 侧下次扫描前会读到并失效。
        通知失败绝不能影响任务本身（写只读目录等场景）。
        """
        try:
            from life_logger.event_view import bump_scan_epoch

            bump_scan_epoch(self.root, reason)
        except Exception as error:  # pragma: no cover - 通知失败不影响业务
            print(f"[life-logger] bump_scan_epoch 失败({reason}): {error}", flush=True)

    def _execute(self, task):
        kind, payload = task['kind'], task['payload']
        if kind in self.handlers:
            return self.handlers[kind](payload)
        if kind=='pipeline':
            store=self.host.store;job=payload['job_id']
            with store._lock:
                state=store._read_unlocked(job)
                dispatch=state.get('processing_dispatch') or {}
                if dispatch.get('task_id') not in {None,task['id']}:
                    return {'status':'superseded'}
                store._active[job]=threading.current_thread()
            store._worker(job,store.job_path(job),Path(payload['config_path']),
                          Path(payload['secrets_path']) if payload.get('secrets_path') else None)
            self.refresh_catalog()
            self._bump_read_epoch(f'pipeline:{job}')
            return {'job_id':job,'status':store.read(job).get('status')}
        if kind=='context':
            store=self.host.store;job=payload['job_id']
            with store._lock:
                state=store._read_unlocked(job);recovery=state.get('context_recovery') or {}
                if recovery.get('recovery_id') != payload['recovery_id']:
                    # Repair the outbox window after enqueue but before status publication.
                    if recovery.get('status') in {'queued','running'}:
                        return {'status':'superseded'}
                    state['context_recovery']=payload['recovery']
                    if payload.get('auto_retry') is not None:
                        state['context_auto_retry']=payload['auto_retry']
                    store._save_unlocked(state)
                store._context_recovery_active[job]=threading.current_thread()
            store._context_recovery_worker(job,payload['recovery_id'],payload['targets'],Path(payload['config_path']),
                                          Path(payload['secrets_path']) if payload.get('secrets_path') else None)
            self.refresh_catalog()
            return {'job_id':job,'context_recovery':store.read(job).get('context_recovery')}
        if kind in {'synthesis','chat'}:
            from life_logger.daily_cognitive_session import generate_daily_cognitive_synthesis, ask_daily_cognitive_session
            brief=self.host.build_daily_brief(date=payload['date'],allow_empty=True)
            events=list(brief.get('events',[]))
            if kind=='synthesis':
                from life_logger.day_facts import activities_for_day
                result=generate_daily_cognitive_synthesis(payload['date'],events,force=bool(payload.get('force')),
                    cache_dir=self.root/'daily-synthesis',activities=None if events else activities_for_day(payload['date']))
                self.refresh_catalog()
                return result
            synthesis=None;file=self.root/'daily-synthesis'/f"synthesis-{payload['date']}.json"
            if file.is_file():
                try:synthesis=json.loads(file.read_text())
                except (OSError,ValueError):pass
            return ask_daily_cognitive_session(payload['date'],payload['question'],events,synthesis,history=payload.get('history'))
        if kind=='ingest-scan':
            result=self.host.run_auto_ingest_scan();self.refresh_catalog();return result
        if kind=='recover-jobs':
            ids=self.host.store.interrupted_job_ids()
            for identifier in ids:
                self.host.store.start_job(identifier,self.config_path,secrets_path=self.root/'secrets.json')
            return {'scheduled':ids,'count':len(ids)}
        if kind=='memory-ask':
            from life_logger.memory_rag import MemoryRAGService
            answer=MemoryRAGService(self.root,self.config_path).ask(str(payload.get('query') or payload.get('question') or '').strip())
            return {'ok':True,'result':answer.to_dict()}
        if kind=='natural-correction':
            from life_logger.processing_actions import natural_correction
            result=natural_correction(self.host,payload);self.refresh_catalog();return result
        if kind=='vip':
            from life_logger.vip_fast_track import transcribe_vip_snippet, execute_vip_command
            text=str(payload.get('text') or '').strip()
            if not text and payload.get('audio_path'):
                text=transcribe_vip_snippet(payload['audio_path'])
            result=execute_vip_command(text=text,timestamp=payload.get('timestamp'),pin_id=payload.get('pin_id'))
            if payload.get('device_id'):
                self.host.watch_session_store.enqueue_haptic(payload['device_id'],'success' if result.get('ok',True) else 'failure')
            self.refresh_catalog()
            return result
        raise ValueError('unknown worker task kind')

    def consume_once(self, kinds):
        task=self.queue.claim(self.owner,kinds=tuple(kinds))
        if task is None:
            return False
        try:
            result=self._execute(task)
            self.queue.finish(task['id'],self.owner,result=result)
        except Exception as error:
            self.queue.finish(task['id'],self.owner,error=f'{type(error).__name__}: {error}')
            traceback.print_exc()
        return True

    def _consume_loop(self,kinds):
        while not self.stop.is_set():
            try:
                idle=not self.consume_once(kinds)
            except sqlite3.OperationalError:
                traceback.print_exc()
                self.stop.wait(self.QUEUE_RETRY_SECONDS)
                continue
            if idle:
                self.stop.wait(0.1)

    def resume_after_boot(self,job_ids):
        # 只续跑本次启动刚标记中断的任务。
        for identifier in job_ids:
            try:
                self.host.store.start_job(identifier,self.config_path,secrets_path=self.root/'secrets.json')
            except Exception:
                traceback.print_exc()

    def beat(self):
        try:
            self.queue.heartbeat(self.owner,self.build)
        except sqlite3.OperationalError:
            traceback.print_exc()

    def serve(self):
        with self.lease:
            self.queue.recover()
            # Pending dispatches have not started and must remain queued on worker boot.
            with self.host.store._lock:
                with self.queue.connection() as db:
                    pending={json.loads(row['payload']).get('job_id') for row in db.execute("SELECT payload FROM tasks WHERE status='queued' AND kind IN ('pipeline','context')")}
                recovered=self.host.store.recover_interrupted(exclude_job_ids=pending)
            self.queue.heartbeat(self.owner,self.build)
            self.resume_after_boot(recovered)
            if self.background:
                self.host.start(warm_vip=False)
            channels=[('pipeline','context','ingest-scan','recover-jobs'),('pipeline','context','ingest-scan','recover-jobs'),('synthesis','chat','memory-ask','natural-correction'),('vip',)]
            threads=[threading.Thread(target=self._consume_loop,args=(kinds,),name='processing-'+str(n),daemon=True)
                     for n,kinds in enumerate(channels)]
            for thread in threads:thread.start()
            try:
                while not self.stop.is_set():
                    self.beat()
                    self.stop.wait(2)
            finally:
                self.stop.set();self.host.close()
                self.queue.offline(self.owner)
                for thread in threads:thread.join(timeout=1)

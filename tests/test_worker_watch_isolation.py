from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import threading
from types import SimpleNamespace

from life_logger.watch_stream import WatchSessionStore


def start(store, session):
    store.start_session(device_id='watch-test',session_id=session,started_at='2026-10-21T10:00:00+08:00')


def put(store, session, sequence=0):
    body=b'retained-audio'
    return store.receive_segment(device_id='watch-test',session_id=session,sequence=sequence,
        start_offset_ms=sequence*10000,duration_ms=10000,
        checksum='sha256:'+hashlib.sha256(body).hexdigest(),body=body)


def test_worker_haptic_is_visible_to_separate_web_store(tmp_path):
    worker=WatchSessionStore(tmp_path,process_safe=True)
    web=WatchSessionStore(tmp_path,process_safe=True)
    worker.enqueue_haptic('watch-test','success')
    assert web.pop_haptic('watch-test')=='success'
    assert worker.pop_haptic('watch-test') is None


def test_independent_stores_do_not_lose_concurrent_segment_manifest_updates(tmp_path):
    first=WatchSessionStore(tmp_path,process_safe=True);second=WatchSessionStore(tmp_path,process_safe=True)
    start(first,'recording')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(put,first if n%2 else second,'recording',n) for n in range(20)]
        for future in futures:assert future.result(timeout=4)['ok']
    assert sorted(row['sequence'] for row in first.load_session('watch-test','recording')['segments'])==list(range(20))


def test_slow_assembly_does_not_block_upload_and_preserves_concurrent_metadata(tmp_path,monkeypatch):
    worker=WatchSessionStore(tmp_path,process_safe=True);web=WatchSessionStore(tmp_path,process_safe=True)
    start(worker,'sealed');put(worker,'sealed')
    worker.complete_session(device_id='watch-test',session_id='sealed',expected_segments=1,ended_at='2026-10-21T10:00:10+08:00')
    start(web,'still-recording')
    entered,release=threading.Event(),threading.Event()
    def ffmpeg(command,**options):
        entered.set();assert release.wait(3)
        (Path(options['cwd'])/command[-1]).write_bytes(b'assembled-audio')
        return SimpleNamespace(returncode=0,stderr='')
    monkeypatch.setattr('life_logger.watch_stream.shutil.which',lambda _: '/test/ffmpeg')
    monkeypatch.setattr('life_logger.watch_stream.subprocess.run',ffmpeg)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assembly=pool.submit(worker.assemble_recording,'watch-test','sealed')
        try:
            assert entered.wait(1)
            def receive():
                web.mark_completion_job('watch-test','sealed','job-kept')
                return put(web,'still-recording')
            upload=pool.submit(receive)
            assert upload.result(timeout=0.5)['ok']
        finally:
            release.set()
        assert assembly.result(timeout=2).is_file()
    assert web.load_session('watch-test','sealed')['completion_job_id']=='job-kept'

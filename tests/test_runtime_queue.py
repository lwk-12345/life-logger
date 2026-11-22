from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys

import pytest

from life_logger.runtime_queue import DispatchConflict, ProcessRLock, TaskQueue, WorkerLease

ROOT = Path(__file__).resolve().parents[1]


def test_request_id_retry_reuses_finished_result_instead_of_spending_twice(tmp_path):
    queue=TaskQueue(tmp_path)
    task=queue.enqueue('synthesis','request:one',{'date':'2026-10-21'})
    queue.claim('worker',kinds=('synthesis',));queue.finish(task['id'],'worker',result={'done':True})
    retried=queue.enqueue('synthesis','request:one',{'date':'2026-10-21'},reuse_finished=True)
    assert retried['id']==task['id'] and retried['result']=={'done':True}


def test_dispatch_is_durable_and_active_duplicate_is_not_run_twice(tmp_path):
    first = TaskQueue(tmp_path).enqueue('pipeline', 'pipeline:job-one', {'job_id': 'job-one'})
    reopened = TaskQueue(tmp_path)
    duplicate = reopened.enqueue('pipeline', 'pipeline:job-one', {'job_id': 'job-one'})
    assert duplicate['id'] == first['id']
    assert duplicate['status'] == 'queued'
    with pytest.raises(DispatchConflict):
        reopened.enqueue('pipeline', 'pipeline:job-one', {'job_id': 'job-two'})


def test_independent_connections_claim_only_one_owner_and_completion_is_fenced(tmp_path):
    queue = TaskQueue(tmp_path)
    task = queue.enqueue('projection', 'projection:one', {'date': '2026-10-21'})
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda n: TaskQueue(tmp_path).claim(str(n), kinds=('projection',)), range(8)))
    claimed = [value for value in claims if value is not None]
    assert len(claimed) == 1
    assert not queue.finish(task['id'], 'wrong-owner', result={'wrong': True})
    assert queue.finish(task['id'], claimed[0]['owner'], result={'amount': 26.8})
    assert queue.get(task['id'])['result'] == {'amount': 26.8}


def test_recovery_requeues_only_safe_reads_and_never_blindly_repeats_model_work(tmp_path):
    queue = TaskQueue(tmp_path)
    safe = queue.enqueue('projection', 'projection:one', {})
    unsafe = queue.enqueue('synthesis', 'synthesis:one', {})
    queue.claim('old', kinds=('projection',))
    queue.claim('old', kinds=('synthesis',))
    assert queue.recover() == {'requeued_reads': 1, 'interrupted_processing': 1}
    assert queue.get(unsafe['id'])['status'] == 'interrupted'
    new = queue.claim('new', kinds=('projection',))
    assert new['id'] == safe['id']
    assert not queue.finish(safe['id'], 'old', result={})
    assert queue.finish(safe['id'], 'new', result={})


def test_worker_lease_is_exclusive_and_released_by_process_exit(tmp_path):
    with WorkerLease(tmp_path):
        second = WorkerLease(tmp_path)
        assert not second.acquire()
    assert second.acquire()
    second.close()


def test_dead_worker_is_not_reported_online_just_because_heartbeat_is_fresh(tmp_path):
    TaskQueue(tmp_path)
    code = 'from life_logger.runtime_queue import TaskQueue; import sys; TaskQueue(sys.argv[1]).heartbeat("departed", "test")'
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
    subprocess.run([sys.executable, '-B', '-c', code, str(tmp_path)], env=env, check=True, timeout=5)
    assert TaskQueue(tmp_path).health()['online'] is False


def test_process_lock_is_reentrant_and_serializes_separate_stores(tmp_path):
    lock = ProcessRLock(tmp_path / 'store.lock')
    with lock:
        with lock:
            other = ProcessRLock(tmp_path / 'store.lock')
            assert not other.acquire(blocking=False)
    assert other.acquire(timeout=0.1)
    other.release()

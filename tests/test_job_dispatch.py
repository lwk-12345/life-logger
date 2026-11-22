import io

from life_logger.local_web import JobStore
from life_logger.runtime_queue import TaskQueue


def create_input(store):
    job=store.create_job()
    store.add_file(job,filename='retained.m4a',relative_path='retained.m4a',stream=io.BytesIO(b'retained-input'))
    return job


def test_web_start_enqueues_without_starting_model_thread_and_survives_web_reconstruction(tmp_path,monkeypatch):
    root=tmp_path/'state';queue=TaskQueue(root);store=JobStore(root,dispatch_queue=queue)
    called=[];monkeypatch.setattr(store,'_worker',lambda *args: called.append(args))
    job=create_input(store);config=tmp_path/'config.json';config.write_text('{}')
    one=store.start_job(job,config);two=store.start_job(job,config)
    assert not called and not store._active
    assert one['processing_dispatch']['task_id']==two['processing_dispatch']['task_id']
    task=queue.claim('worker',kinds=('pipeline',));assert task['payload']['job_id']==job
    reopened=JobStore(root,dispatch_queue=TaskQueue(root))
    assert reopened.read(job)['status']=='queued'
    assert not reopened.startup_recovery_ids
    assert queue.get(task['id'])['status']=='running'


def test_web_restart_never_marks_worker_owned_running_job_interrupted(tmp_path):
    queue=TaskQueue(tmp_path);worker=JobStore(tmp_path,recover=False,process_safe=True)
    job=create_input(worker)
    with worker._lock:
        state=worker._read_unlocked(job);state.update(status='running',current_stage='asr');worker._save_unlocked(state)
    web=JobStore(tmp_path,dispatch_queue=queue)
    assert web.read(job)['status']=='running'
    assert web.startup_recovery_ids==()

"""Launch independent processing without binding its lifetime to the Web process."""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from life_logger.runtime_queue import ProcessRLock, TaskQueue, WorkerLease


def publish_web_role(root: Path, *, mode: str, port: int) -> None:
    directory=Path(root)/'runtime';directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=directory/'web.json';temporary=directory/f'.web-{os.getpid()}.tmp'
    temporary.write_text(json.dumps({'pid':os.getpid(),'mode':mode,'port':port,'started':time.time()})+'\n')
    temporary.chmod(0o600);os.replace(temporary,target)


def ensure_worker(root: Path, config_path: Path) -> dict:
    root=Path(root).resolve();queue=TaskQueue(root)
    with ProcessRLock(root/'runtime'/'worker-launch.lock'):
        current=queue.health()
        if current['online']:
            return current
        lease=WorkerLease(root)
        if not lease.acquire():
            return {'online':False,'starting':True}
        lease.close()
        project=Path(__file__).resolve().parents[2]
        log=root/'runtime'/'worker.log'
        with log.open('ab') as output:
            process=subprocess.Popen([sys.executable,'-B',str(project/'scripts/processing_worker.py'),
                '--root',str(root),'--config',str(Path(config_path).resolve())],
                cwd=project,stdout=output,stderr=subprocess.STDOUT,close_fds=True,start_new_session=True)
        return {'online':False,'starting':True,'pid':process.pid}


def refuse_legacy_processing(root: Path) -> None:
    """Guard the default live root; never layer a worker onto an old coordinator."""
    from life_logger.local_web import DEFAULT_JOB_ROOT
    root=Path(root).resolve()
    if root != DEFAULT_JOB_ROOT.resolve():
        return
    role=root/'runtime'/'web.json'
    try:
        state=json.loads(role.read_text())
        os.kill(int(state['pid']),0)
        if state.get('mode') in {'external','split'}:
            return
    except (OSError,ValueError,KeyError):
        pass
    # An old Web server does not publish web.json. Probe the wildcard bind so
    # a server listening only on a LAN address is also detected.
    probe=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        probe.bind(('0.0.0.0',8765))
    except OSError as error:
        raise RuntimeError('legacy Web may still own processing; drain/switch it before starting a worker') from error
    finally:
        probe.close()

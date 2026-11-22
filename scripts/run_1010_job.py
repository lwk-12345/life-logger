"""Run the 10-10 afternoon session Job to completion with DeepSeek event extraction."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from life_logger.local_web import JobStore

def main():
    store = JobStore(REPO_ROOT / "output/local-web")
    job_id = "job-20261011-172756-64bf63c9"
    config_path = REPO_ROOT / "local-config.json"
    secrets_path = REPO_ROOT / "output/local-web/secrets.json"
    job_dir = store.job_path(job_id)

    print(f"=== 开始执行 10月10日 下午长录音全量事件提取作业 [{job_id}] ===")
    store.prepare_for_start(job_id)
    try:
        store._worker(job_id, job_dir, config_path, secrets_path)
    except Exception:
        import traceback
        traceback.print_exc()
    print(f"=== 作业 [{job_id}] 执行结束，当前状态: {store.read(job_id).get('status')}, error: {store.read(job_id).get('error')} ===")

if __name__ == "__main__":
    main()

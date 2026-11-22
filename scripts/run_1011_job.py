"""Run the 10-11 session Job to completion with CAM++ on MPS and DeepSeek event extraction."""

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
    job_id = "job-20261011-180926-331be28c"
    config_path = REPO_ROOT / "local-config.json"
    secrets_path = REPO_ROOT / "output/local-web/secrets.json"
    job_dir = store.job_path(job_id)

    print(f"=== 开始执行 10月11日 今日全量事件提取作业 [{job_id}] ===", flush=True)
    store.prepare_for_start(job_id)
    try:
        store._worker(job_id, job_dir, config_path, secrets_path)
    except Exception:
        import traceback
        traceback.print_exc()
    status = store.read(job_id)
    print(f"=== 作业 [{job_id}] 执行结束，当前状态: {status.get('status')}, error: {status.get('error')} ===", flush=True)

if __name__ == "__main__":
    main()

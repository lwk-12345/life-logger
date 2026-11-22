from pathlib import Path
import json
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from life_logger.local_web import JobStore

dev_id = "watch-41aa6b87347f4f0fb5238b559f705c4d"
sess_id = "901BE63F70EF4937B42E836033B1A8F6"
sessions_dir = REPO_ROOT / "output/local-web/watch-sessions"
session_dir = sessions_dir / dev_id / sess_id
manifest_path = session_dir / "manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
config_path = REPO_ROOT / "local-config.json"

store = JobStore(REPO_ROOT / "output/local-web")
print(f"正在为 10月10日 会话 [{sess_id}] 执行最终全量入库、说话人认主与日记生成...")
imported = store.import_watch_recording(
    session_dir,
    {**manifest, "complete": True},
    config_path,
)
print("恭喜！10月10日长录音已全部成功入库并生成生活事实日记！Job ID:", imported.get("job_id"))

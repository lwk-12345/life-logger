#!/usr/bin/env bash
# 发布打 tag：先同步台账，再打附注 tag。
#
# 命名规则：v<YYYY.MM.DD>.<同日序号>-canonical
#   例：当天第一个 → v2026.10.22.1-canonical，第二个 → v2026.10.22.2-canonical
#
# 用法：
#   scripts/release_tag.sh --dry-run "一句话描述"
#   scripts/release_tag.sh "一句话描述"
#
# 约束（P-21）：
#   - **不 push**，只建本地 tag
#   - 台账同步失败就不打 tag（宁可停下，也不要「有 tag 没台账」）
#   - --dry-run 只演练名字与提交摘要，绝不落 tag
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DRY_RUN=0
MSG=""
for arg in "$@"; do
  case "$arg" in
    --dry-run|-n) DRY_RUN=1 ;;
    -h|--help)
      echo "用法: scripts/release_tag.sh [--dry-run] \"<一句话描述>\""
      exit 0
      ;;
    *) MSG="$arg" ;;
  esac
done

if [[ -z "$MSG" ]]; then
  echo "错误：必须给一句话描述，例如：scripts/release_tag.sh \"修好日报回补触发条件\"" >&2
  exit 2
fi

# [2026-11-04 UTC+8] tag 日期按北京时间：UTC-8 的下午之后北京已跨日，直接用 `date` 会把 tag 标成前一天。
TODAY="$(TZ=Asia/Shanghai date +%Y.%m.%d)"
PREFIX="v${TODAY}."

# 同日序号：数一下已存在的同日 tag
SEQ=1
while git rev-parse -q --verify "refs/tags/${PREFIX}${SEQ}-canonical" >/dev/null 2>&1; do
  SEQ=$((SEQ + 1))
done
TAG="${PREFIX}${SEQ}-canonical"
HEAD_SHORT="$(git rev-parse --short=7 HEAD)"
HEAD_FULL="$(git rev-parse HEAD)"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"

echo "──────────────────────────────────────────────"
echo " tag      : $TAG"
echo " 分支     : $BRANCH"
echo " 目标提交 : $HEAD_SHORT ($HEAD_FULL)"
echo " 描述     : $MSG"
echo "──────────────────────────────────────────────"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "[dry-run] 将先执行：python3 scripts/ledger_sync.py"
  echo "[dry-run] 将创建附注 tag：git tag -a '$TAG' -m '<描述>' $HEAD_SHORT"
  echo "[dry-run] 不会 push；本条只是演练，未创建任何 tag"
  echo "[dry-run] 当前该提交已有的 tag："
  git tag --points-at "$HEAD_FULL" | sed 's/^/    /' || true
  echo "[dry-run] HEAD 改动摘要："
  git show --stat --oneline "$HEAD_FULL" | head -12
  exit 0
fi

echo "[1/3] 同步台账…"
python3 scripts/ledger_sync.py
LEDGER_RC=$?
if [[ $LEDGER_RC -ne 0 ]]; then
  echo "台账同步失败（退出码 $LEDGER_RC），已中止打 tag。" >&2
  exit "$LEDGER_RC"
fi

# 台账同步会改 .planning/ 文件；用户要求本轮不 commit，
# 所以只提示存在未提交改动，不自动提交、不阻断打 tag。
if ! git diff --quiet -- .planning/ 2>/dev/null; then
  echo "[提示] .planning/ 有未提交改动（本轮按约束不自动 commit），tag 仍指向 $HEAD_SHORT"
fi

echo "[2/3] 创建附注 tag…"
git tag -a "$TAG" -m "$MSG" "$HEAD_SHORT"

echo "[3/3] 完成。"
echo
echo "tag 名：$TAG"
echo "摘要："
git show --stat --oneline "$TAG" | head -20
echo
echo "（未 push。需要推送时手动执行：git push origin '$TAG'）"

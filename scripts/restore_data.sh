#!/usr/bin/env bash
# Life Logger 数据恢复
#
# 用法：
#   scripts/restore_data.sh <备份文件> --dry-run          # 只看会覆盖什么，不动磁盘
#   scripts/restore_data.sh <备份文件> --target /tmp/x    # 演练：解到临时目录
#   scripts/restore_data.sh <备份文件> --yes              # 真恢复（覆盖原始绝对路径）
#
# 安全设计：
#   · 校验同名 .sha256，不匹配直接拒绝（不猜、不修）
#   · 真恢复前自动把**当前**权威数据打包成 backups/pre-restore-<ts>.tar.gz，可回滚
#   · 恢复目标是 /（因为备份里保留的是绝对路径）时**必须显式 --yes**，防手滑
#   · 只允许恢复到空目录或 /；恢复到半满目录会留下新旧混合，脚本会提示风险
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE=""
TARGET=""
DRY=0
YES=0
for a in "$@"; do
  case "$a" in
    --dry-run|-n) DRY=1 ;;
    --target) shift_target=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
    --target=*) TARGET="${a#--target=}" ;;
    *)
      if [[ "${shift_target:-0}" == "1" ]]; then TARGET="$a"; shift_target=0
      elif [[ -z "$ARCHIVE" ]]; then ARCHIVE="$a"
      else echo "未知参数: $a" >&2; exit 2; fi
      ;;
  esac
done

if [[ -z "$ARCHIVE" ]]; then
  echo "用法: scripts/restore_data.sh <备份文件> [--dry-run] [--target DIR] [--yes]" >&2
  exit 2
fi
if [[ ! -f "$ARCHIVE" ]]; then
  echo "错误：备份文件不存在 $ARCHIVE" >&2
  exit 1
fi

# ── 1) sha256 校验 ─────────────────────────────────────────────────────────
SUM_FILE="${ARCHIVE%.tar.gz}.sha256"
if [[ -f "$SUM_FILE" ]]; then
  EXPECTED="$(awk '{print $1}' "$SUM_FILE")"
  ACTUAL="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
  if [[ "$EXPECTED" != "$ACTUAL" ]]; then
    echo "错误：sha256 不匹配，拒绝恢复" >&2
    echo "  期望: $EXPECTED" >&2
    echo "  实际: $ACTUAL" >&2
    exit 3
  fi
  echo "[restore] sha256 校验通过: $ACTUAL"
else
  echo "[restore] 警告：找不到 $SUM_FILE，跳过校验（不推荐）" >&2
fi

# ── 2) 统计将覆盖的内容 ────────────────────────────────────────────────────
TMPLIST="$(mktemp)"
trap 'rm -f "$TMPLIST"' EXIT
tar -tzf "$ARCHIVE" > "$TMPLIST" 2>/dev/null
FILES="$(grep -vc '/$' "$TMPLIST" || true)"
UNPACKED="$(tar -tvzf "$ARCHIVE" 2>/dev/null | awk '$1 ~ /^-/ {s+=$5} END{printf "%.1f", s/1048576}')"

if [[ -z "$TARGET" ]]; then
  TARGET="/"
fi

echo "──────────────────────────────────────────────"
echo " 备份文件 : $(basename "$ARCHIVE")"
echo " 文件数   : $FILES"
echo " 解包大小 : ${UNPACKED} MB"
echo " 恢复目标 : $TARGET"
echo "──────────────────────────────────────────────"

if [[ "$DRY" == "1" ]]; then
  echo "[dry-run] 将覆盖以下前 10 条路径（共 $FILES 条）："
  grep -v '/$' "$TMPLIST" | head -10 | sed 's/^/        /'
  echo "[dry-run] 未改动磁盘"
  exit 0
fi

if [[ "$TARGET" == "/" && "$YES" != "1" ]]; then
  echo "拒绝执行：恢复到 / 会覆盖线上数据，必须显式加 --yes。" >&2
  echo "  演练请用：scripts/restore_data.sh \"$ARCHIVE\" --target /tmp/drill" >&2
  exit 4
fi

# ── 3) 真恢复前：先给当前数据做一份 ────────────────────────────────────────
if [[ "$TARGET" == "/" ]]; then
  echo "[restore] 先备份当前数据（可回滚）…"
  PRE="$(cd "$ROOT" && ./scripts/backup_data.sh 2>&1 | tee /dev/stderr | awk -F': ' '/文件   :/{print $2}')"
  if [[ -z "$PRE" ]]; then
    echo "错误：恢复前的自动备份失败，已中止（不冒险覆盖）" >&2
    exit 5
  fi
  echo "[restore] 回滚点: $PRE"
fi

# ── 4) 解包 ───────────────────────────────────────────────────────────────
echo "[restore] 解包到 $TARGET …"
tar --no-mac-metadata -xzf "$ARCHIVE" -C "$TARGET" 2> >(grep -v 'xattr\|Ignoring' >&2) || {
  echo "错误：解包失败" >&2
  exit 6
}

echo "[restore] 完成。"
if [[ "$TARGET" == "/" ]]; then
  echo "  回滚命令：scripts/restore_data.sh \"$PRE\" --yes"
else
  echo "  演练目录内容（前 10 条）："
  find "$TARGET" -type f | head -10 | sed 's/^/        /'
fi

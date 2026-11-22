#!/usr/bin/env bash
# Life Logger 数据备份
#
# ⚠️ 异地在 P7 被用户搁置（2026-10-22），默认不启用。
#    `--mirror` / `--icloud` 代码**保留但不再使用**（不接 launchd、不进默认参数）。
#    现状就是「每天 03:00 在本机 backups/ 打一个压缩包」，用户明确不要异地/镜像/iCloud。
#
# 设计取舍（重要）：A0 原始要求是「output/local-web 下除 jobs/ 以外的全部」，实测那有 16.6 GB
# （watch-sessions 13G 音频 + content 3.3G 内容寻址块）。按「保留 7 天 + 4 个周一 ≈ 11 份」算，
# 一天 16.6 GB × 11 ≈ 180 GB —— 不现实，且每天压缩十几 G 音频会让 03:00 的任务跑不完。
#
# 因此：
#   默认模式 = 只备份「不可重建的权威数据」（约 5 MB）
#     · daily-synthesis/            （日报）
#     · *.json 顶层状态             （reminders / quick_captures / airpassport-watch ...）
#     · watch-sessions/**/manifest.json、events.json、decision-comparison.json
#     · acceptance-*.md
#     · local-config.json + output/local-web/secrets.json
#   --full   = 除 jobs/、content/、音频以外的全部（约 7.9 GB），手动/每周用
#
# 用法：
#   scripts/backup_data.sh                 # 默认权威数据（约 5 MB）
#   scripts/backup_data.sh --full           # 大备份（约 7.9 GB，慎用）
#   scripts/backup_data.sh --dry-run        # 只打印将备份什么、将删哪些旧备份
#   scripts/backup_data.sh --prune-only     # 只做保留策略清理
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="$ROOT/output/local-web"
DEST="${BACKUP_DIR:-$ROOT/backups}"   # 可用 BACKUP_DIR 覆盖（便于演练保留策略）
MODE="normal"
DRY=0
PRUNE_ONLY=0
MIRROR=""
ICLOUD=0
want_mirror=0
ICLOUD_DIR="${ICLOUD_DIR_OVERRIDE:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/LifeLoggerBackups}"
for a in "$@"; do
  if [[ "$want_mirror" == "1" ]]; then MIRROR="$a"; want_mirror=0; continue; fi
  case "$a" in
    --full) MODE="full" ;;
    --dry-run|-n) DRY=1 ;;
    --prune-only) PRUNE_ONLY=1 ;;
    --mirror) want_mirror=1 ;;
    --mirror=*) MIRROR="${a#--mirror=}" ;;
    --icloud) ICLOUD=1; MIRROR="$ICLOUD_DIR" ;;
    -h|--help) sed -n '2,34p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "未知参数: $a" >&2; exit 2 ;;
  esac
done
if [[ "$want_mirror" == "1" ]]; then echo "--mirror 需要一个目录参数" >&2; exit 2; fi

if [[ ! -d "$DATA" ]]; then
  echo "错误：找不到数据目录 $DATA" >&2
  exit 1
fi

# ── 并发锁：同一时刻只允许一个备份进程 ────────────────────────────────────
# 2026-10-22 实测事故：手动跑与 launchd 03:00 同时在跑时，后完成的那个
# 会因为「同一天只留最新一份」把另一个刚生成的文件删掉，日志看起来像自己删自己。
mkdir -p "$DEST" 2>/dev/null || true
LOCK="$DEST/.backup.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  echo "[backup] 已有备份进程在跑（$LOCK 存在），本次跳过"
  exit 0
fi
cleanup_lock() { rmdir "$LOCK" 2>/dev/null || true; }
trap 'cleanup_lock; rm -f "${LIST:-}"' EXIT

# 手表录音 30 天滚动备份，含空间提醒（2026-11-04 用户决定）。失败不影响下面的权威数据备份。
if [[ "$DRY" != "1" ]]; then
  python3 "$ROOT/scripts/backup_audio.py" --root "$ROOT" || echo "[audio] 录音备份有失败，详见 backups/audio-30d/status.json" >&2
fi

TS="$(date +%Y%m%d-%H%M%S)"
HOUR="$(date +%Y%m%d-%H)"
ARCHIVE="$DEST/life-logger-$TS.tar.gz"

# ── 保留策略：最近 7 天各留最新一份 + 每周一留最近 4 份 ──────────────────────
prune() {
  [[ -d "$DEST" ]] || return 0
  local del
  del="$(python3 - "$DEST" <<'PY'
import os, re, sys
from collections import defaultdict
from datetime import datetime

dest = sys.argv[1]
files = []
for name in os.listdir(dest):
    m = re.fullmatch(r"life-logger-(\d{8})-(\d{6})\.tar\.gz", name)
    if not m:
        continue
    stamp = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    files.append((stamp, name))
files.sort(reverse=True)

by_day = defaultdict(list)
for stamp, name in files:
    by_day[stamp.strftime("%Y-%m-%d")].append((stamp, name))

keep = set()
# 最近 7 个不同日期，每天留最新一份
for day in sorted(by_day)[-7:]:
    keep.add(by_day[day][0][1])
# 每周一（weekday=0）留最近 4 份
mondays = [d for d in sorted(by_day, reverse=True)
           if datetime.strptime(d, "%Y-%m-%d").weekday() == 0]
for day in mondays[:4]:
    keep.add(by_day[day][0][1])

for stamp, name in files:
    if name not in keep:
        print(os.path.join(dest, name))
PY
)"
  if [[ -z "$del" ]]; then
    echo "[prune] 无需清理（当前 $(find "$DEST" -name 'life-logger-*.tar.gz' | wc -l | tr -d ' ') 份）"
    return 0
  fi
  echo "[prune] 将删除以下 $(echo "$del" | wc -l | tr -d ' ') 份旧备份："
  echo "$del" | sed 's/^/        /'
  if [[ "$DRY" == "1" ]]; then
    echo "[prune] --dry-run：未真正删除"
    return 0
  fi
  echo "$del" | while read -r f; do
    [[ -n "$f" ]] || continue
    rm -f "$f" "${f%.tar.gz}.sha256"
  done
  echo "[prune] 已清理"
}

# ── 异盘/异地镜像：复制当次备份并校验 sha256 ────────────────────────────────
# 参数：$1=镜像目录。iCloud 模式只留最新 3 份（同步大文件慢，不能放 11 份）。
mirror_copy() {
  local dir="$1"
  local keep_policy="${2:-daily}"
  if [[ -z "$dir" ]]; then return 0; fi
  if [[ ! -d "$dir" ]]; then
    if ! mkdir -p "$dir" 2>/dev/null; then
      echo "[mirror] 跳过：无法创建镜像目录 $dir" >&2
      return 1
    fi
  fi
  local base; base="$(basename "$ARCHIVE")"
  echo "[mirror] 复制到 $dir …"
  cp -f "$ARCHIVE" "$dir/$base" || { echo "[mirror] 复制失败" >&2; return 1; }
  cp -f "${ARCHIVE%.tar.gz}.sha256" "$dir/${base%.tar.gz}.sha256" 2>/dev/null || true

  local want got
  want="$(awk '{print $1}' "${ARCHIVE%.tar.gz}.sha256")"
  got="$(shasum -a 256 "$dir/$base" | awk '{print $1}')"
  if [[ "$want" != "$got" ]]; then
    echo "[mirror] ❌ 校验失败！源=$want 副本=$got" >&2
    return 1
  fi
  echo "[mirror] ✅ 校验通过（副本 sha256 与源一致）: $got"

  mirror_prune "$dir" "$keep_policy"
  return 0
}

# 镜像侧清理：newest3 = 只留最新 3 份（iCloud 用）；daily = 沿用本地 7+周一4 规则
mirror_prune() {
  local dir="$1" policy="${2:-daily}"
  [[ -d "$dir" ]] || return 0
  if [[ "$policy" == "newest3" ]]; then
    local old
    # 必须按「文件名里的时间戳」排序，不能用 ls -t（mtime）：
    # 复制到镜像会刷新 mtime，2026-10-22 实测因此误删了最新的那一份。
    old="$(ls "$dir"/life-logger-*.tar.gz 2>/dev/null | sort -r | tail -n +4)"
    if [[ -z "$old" ]]; then
      echo "[mirror] iCloud 保留最新 3 份：当前未超限"
      return 0
    fi
    echo "[mirror] iCloud 保留最新 3 份，将删除 $(echo "$old" | wc -l | tr -d ' ') 份："
    echo "$old" | sed 's/^/        /'
    if [[ "$DRY" != "1" ]]; then
      echo "$old" | while read -r f; do
        [[ -n "$f" ]] || continue
        rm -f "$f" "${f%.tar.gz}.sha256"
      done
    fi
  fi
  return 0
}

if [[ "$PRUNE_ONLY" == "1" ]]; then
  prune
  exit 0
fi

# ── 幂等：同一小时内已有备份就跳过 ─────────────────────────────────────────
if ls "$DEST"/life-logger-"$HOUR"*.tar.gz >/dev/null 2>&1; then
  ARCHIVE="$(ls -t "$DEST"/life-logger-"$HOUR"*.tar.gz | head -1)"
  echo "[backup] 本小时($HOUR)已有备份，跳过生成：$(basename "$ARCHIVE")"
  prune
  # 幂等不等于「跳过镜像」：本地已有、镜像缺时仍要补一次
  if [[ -n "$MIRROR" ]]; then
    if [[ -f "$MIRROR/$(basename "$ARCHIVE")" ]]; then
      echo "[mirror] 镜像已存在同名文件，跳过复制：$MIRROR/$(basename "$ARCHIVE")"
      mirror_prune "$MIRROR" "$( [[ "$ICLOUD" == "1" ]] && echo newest3 || echo daily )"
    else
      if [[ "$DRY" == "1" ]]; then
        echo "[mirror][dry-run] 将补镜像到 $MIRROR"
      else
        mirror_copy "$MIRROR" "$( [[ "$ICLOUD" == "1" ]] && echo newest3 || echo daily )" || true
      fi
    fi
  fi
  exit 0
fi

mkdir -p "$DEST"
LIST="$(mktemp)"

build_list() {
  if [[ "$MODE" == "full" ]]; then
    # 排除：jobs（大且可重建）、content（内容寻址块）、音频、模型批次缓存
    find "$DATA" \
      \( -path "$DATA/jobs" -o -path "$DATA/content" \) -prune -o \
      -type f \
      ! -name '*.m4a' ! -name '*.wav' ! -name '*.mp3' ! -name '*.caf' \
      ! -path '*/.context-extraction-batches/*' \
      ! -path '*/comparison-history/*' \
      -print
  else
    # comparison-history 是 Jev/Clef 每次对照的快照，单个 12~17 MB，纯缓存，
    # 剪掉它 3121 MB → 69.6 MB。live-context 下的当次 decision-comparison.json 保留。
    find "$DATA" \
      \( -path "$DATA/jobs" -o -path "$DATA/content" -o -name 'comparison-history' \) -prune -o \
      -type f \( \
           -name 'manifest.json' \
        -o -name 'events.json' \
        -o -name 'decision-comparison.json' \
        -o -name 'reminders.json' \
        -o -name 'quick_captures.json' \
        -o -path "$DATA/daily-synthesis/*" \
        -o -name 'acceptance-*.md' \
      \) -print
  fi
}

build_list >> "$LIST"
if [[ -f "$ROOT/local-config.json" ]]; then echo "$ROOT/local-config.json" >> "$LIST"; fi
if [[ -f "$DATA/secrets.json" ]]; then echo "$DATA/secrets.json" >> "$LIST"; fi

COUNT="$(wc -l < "$LIST" | tr -d ' ')"
if [[ "$COUNT" == "0" ]]; then
  echo "[backup] 没有可备份文件，退出" >&2
  exit 1
fi
BYTES="$(tr '\n' '\0' < "$LIST" | xargs -0 stat -f '%z' 2>/dev/null | awk '{s+=$1} END{printf "%.1f", s/1048576}')"

echo "──────────────────────────────────────────────"
echo " 模式     : $MODE"
echo " 文件数   : $COUNT"
echo " 原始大小 : ${BYTES} MB"
echo " 目标     : $ARCHIVE"
echo "──────────────────────────────────────────────"

if [[ "$DRY" == "1" ]]; then
  echo "[dry-run] 将打包以上 $COUNT 个文件；未写入磁盘"
  prune
  exit 0
fi

# ── 打包（-T 读文件清单，路径用绝对路径，解包时保留结构）──────────────────
tar --no-mac-metadata -czf "$ARCHIVE" -T "$LIST" 2> >(grep -v 'xattr\|Ignoring' >&2) || {
  echo "[backup] 打包失败" >&2
  rm -f "$ARCHIVE"
  exit 1
}

SIZE="$(du -h "$ARCHIVE" | awk '{print $1}')"
( cd "$DEST" && shasum -a 256 "$(basename "$ARCHIVE")" > "$(basename "$ARCHIVE" .tar.gz).sha256" )
SHA="$(awk '{print $1}' "${ARCHIVE%.tar.gz}.sha256")"

echo "[backup] 完成"
echo "  文件   : backups/$(basename "$ARCHIVE")"
echo "  大小   : $SIZE"
echo "  sha256 : $SHA"
echo "  校验   : backups/$(basename "${ARCHIVE%.tar.gz}.sha256")"

prune

# ── 镜像（异盘/异地）────────────────────────────────────────────────────────
if [[ -n "$MIRROR" ]]; then
  if [[ "$DRY" == "1" ]]; then
    echo "[mirror][dry-run] 将复制到 $MIRROR $( [[ "$ICLOUD" == "1" ]] && echo "（iCloud 模式，保留最新 3 份）" )"
  else
    if [[ "$ICLOUD" == "1" ]]; then
      mirror_copy "$MIRROR" newest3 || echo "[mirror] iCloud 镜像未完成（本地备份仍然有效）" >&2
    else
      mirror_copy "$MIRROR" daily || echo "[mirror] 镜像未完成（本地备份仍然有效）" >&2
    fi
  fi
elif [[ -n "${BACKUP_MIRROR:-}" ]]; then
  if [[ "$DRY" != "1" ]]; then mirror_copy "$BACKUP_MIRROR" daily || true; fi
fi

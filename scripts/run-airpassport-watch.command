#!/bin/zsh
# 记录 AirPassport 的按键说话时间窗（--mark-only）。
#
# AirPassport 现在是 Mac 上的语音输入（dictation），不再是 Life Logger 的录音来源。
# 它会持续监听 ~/AirPassportRecordings/，只把每段说话的起止时刻写进
# output/local-web/dictation-windows.jsonl，不建任务、不跑 ASR。
#
# 这个索引的用途：Apple Watch 在电脑前会把这些话一并录进去，处理 Watch 录音时
# 用这些时间窗把 dictation 句子剔除，避免污染生活事件。
#
# 由 LaunchAgent com.life-logger.airpassport-watch 拉起，也可手动执行。
set -e
repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"
exec python3 scripts/watch_airpassport.py --mark-only --poll-interval 15

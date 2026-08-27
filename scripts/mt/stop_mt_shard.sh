#!/usr/bin/env bash
# Stop one cloud MT rerun shard and clean its leftover OpenJML/Javac workers.

set -u

usage() {
  cat <<'EOF'
用法:
  bash scripts/mt/stop_mt_shard.sh <shard编号> [--wait 秒数]

示例:
  bash scripts/mt/stop_mt_shard.sh 1
  bash scripts/mt/stop_mt_shard.sh 3 --wait 15

说明:
  - shard编号使用 1/2/3/4/5，不需要写 01/02。
  - 脚本只停止当前 shard 的进程，不删除 results_shard_XX.jsonl。
  - 停止后可用 start_mt_shard.sh 通过 --resume 继续跑。
EOF
}

die() {
  echo "错误: $*" >&2
  exit 1
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$PROJECT_ROOT" || exit 1

SHARD="${1:-}"
WAIT_SECONDS=10

if [[ -z "$SHARD" || "$SHARD" == "-h" || "$SHARD" == "--help" ]]; then
  usage
  exit 0
fi
shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --wait)
      [[ $# -ge 2 ]] || die "--wait 缺少秒数"
      WAIT_SECONDS="$2"
      shift 2
      ;;
    *)
      die "未知参数: $1"
      ;;
  esac
done

[[ "$SHARD" =~ ^[1-5]$ ]] || die "shard编号必须是 1/2/3/4/5"
[[ "$WAIT_SECONDS" =~ ^[0-9]+$ && "$WAIT_SECONDS" -gt 0 ]] || die "--wait 必须是正整数"

SHARD_PAD="$(printf "%02d" "$SHARD")"
MAIN_PATTERN="scripts/mt/run_mt_candidates.py.*work_shard_${SHARD_PAD}"
WORK_PATTERN="mt_rerun_stage6/work_shard_${SHARD_PAD}"

show_processes() {
  echo
  echo "当前 shard ${SHARD} 相关进程:"
  local found=0
  if pgrep -af "$MAIN_PATTERN" >/tmp/mt_stop_main.$$ 2>/dev/null; then
    found=1
    echo "[主进程]"
    cat /tmp/mt_stop_main.$$
  fi
  if pgrep -af "$WORK_PATTERN" >/tmp/mt_stop_workers.$$ 2>/dev/null; then
    found=1
    echo "[残留 OpenJML/Javac 子进程]"
    cat /tmp/mt_stop_workers.$$
  fi
  rm -f /tmp/mt_stop_main.$$ /tmp/mt_stop_workers.$$
  if [[ "$found" -eq 0 ]]; then
    echo "无。"
    return 1
  fi
  return 0
}

has_processes() {
  pgrep -f "$MAIN_PATTERN" >/dev/null 2>&1 || pgrep -f "$WORK_PATTERN" >/dev/null 2>&1
}

term_processes() {
  echo "发送 SIGTERM 到 shard ${SHARD} 相关进程..."
  pkill -TERM -f "$MAIN_PATTERN" 2>/dev/null || true
  pkill -TERM -f "$WORK_PATTERN" 2>/dev/null || true
}

kill_processes() {
  echo "发送 SIGKILL 到 shard ${SHARD} 相关进程..."
  pkill -KILL -f "$MAIN_PATTERN" 2>/dev/null || true
  pkill -KILL -f "$WORK_PATTERN" 2>/dev/null || true
}

echo "准备停止 shard ${SHARD} (内部文件编号 ${SHARD_PAD})"
show_processes || {
  echo "无需处理。"
  exit 0
}

term_processes
echo "等待 ${WAIT_SECONDS} 秒..."
sleep "$WAIT_SECONDS"

while has_processes; do
  show_processes || break
  echo
  echo "请选择下一步:"
  echo "  1) 继续等待 ${WAIT_SECONDS} 秒后再检查"
  echo "  2) 再次温和停止(SIGTERM)，然后等待 ${WAIT_SECONDS} 秒"
  echo "  3) 直接强杀(SIGKILL)"
  read -r -p "请输入 1/2/3: " CHOICE
  case "$CHOICE" in
    1)
      echo "继续等待 ${WAIT_SECONDS} 秒..."
      sleep "$WAIT_SECONDS"
      ;;
    2)
      term_processes
      echo "等待 ${WAIT_SECONDS} 秒..."
      sleep "$WAIT_SECONDS"
      ;;
    3)
      kill_processes
      sleep 2
      ;;
    *)
      echo "无效输入，请输入 1/2/3。"
      ;;
  esac
done

echo
if has_processes; then
  echo "仍有残留进程，请重新运行本脚本检查。"
  exit 1
fi

echo "shard ${SHARD} 已停止并清理完成。"
echo "结果文件未删除: output/mt_rerun_stage6/results_shard_${SHARD_PAD}.jsonl"

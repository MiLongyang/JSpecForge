#!/usr/bin/env bash
# Start or resume one cloud MT rerun shard.

set -u

usage() {
  cat <<'EOF'
用法:
  bash scripts/mt/start_mt_shard.sh <shard编号> [选项]

示例:
  bash scripts/mt/start_mt_shard.sh 1
  bash scripts/mt/start_mt_shard.sh 2 --jobs 2 --workers 2
  bash scripts/mt/start_mt_shard.sh 3 --jobs 2 --workers 2 --background

说明:
  - shard编号使用 1/2/3/4/5，不需要写 01/02。
  - 默认使用 --resume，从已有 results_shard_XX.jsonl 继续，不会重跑已完成记录。
  - 不会使用 --overwrite。
  - 默认前台运行，直接打印日志。只有显式加 --background 才后台运行。

可选参数:
  --jobs N       候选级并发数: 同时跑多少条候选记录。
  --workers N    单条候选内部并发数: 同一条候选里同时验证多少个 mutant。
  --max-mutants N
  --major-timeout 秒
  --timeout-per-mutant 秒
  --background   后台运行，输出追加到日志文件。
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
if [[ -z "$SHARD" || "$SHARD" == "-h" || "$SHARD" == "--help" ]]; then
  usage
  exit 0
fi
shift || true

JOBS=""
WORKERS=""
MAX_MUTANTS=50
MAJOR_TIMEOUT=180
TIMEOUT_PER_MUTANT=180
BACKGROUND=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --jobs)
      [[ $# -ge 2 ]] || die "--jobs 缺少数值"
      JOBS="$2"
      shift 2
      ;;
    --workers|--workers-per-candidate)
      [[ $# -ge 2 ]] || die "$1 缺少数值"
      WORKERS="$2"
      shift 2
      ;;
    --max-mutants)
      [[ $# -ge 2 ]] || die "--max-mutants 缺少数值"
      MAX_MUTANTS="$2"
      shift 2
      ;;
    --major-timeout)
      [[ $# -ge 2 ]] || die "--major-timeout 缺少秒数"
      MAJOR_TIMEOUT="$2"
      shift 2
      ;;
    --timeout-per-mutant)
      [[ $# -ge 2 ]] || die "--timeout-per-mutant 缺少秒数"
      TIMEOUT_PER_MUTANT="$2"
      shift 2
      ;;
    --background)
      BACKGROUND=1
      shift
      ;;
    *)
      die "未知参数: $1"
      ;;
  esac
done

[[ "$SHARD" =~ ^[1-5]$ ]] || die "shard编号必须是 1/2/3/4/5"
SHARD_PAD="$(printf "%02d" "$SHARD")"

if [[ -z "$JOBS" ]]; then
  echo "候选级并发数 jobs: 同时跑多少条候选记录。建议 2；机器稳定可试 3。"
  read -r -p "请输入 jobs [默认 2]: " JOBS
  JOBS="${JOBS:-2}"
fi

if [[ -z "$WORKERS" ]]; then
  echo "单候选内部并发 workers: 一条候选内同时验证多少个 mutant。建议 2。"
  read -r -p "请输入 workers [默认 2]: " WORKERS
  WORKERS="${WORKERS:-2}"
fi

for value_name in JOBS WORKERS MAX_MUTANTS MAJOR_TIMEOUT TIMEOUT_PER_MUTANT; do
  value="${!value_name}"
  [[ "$value" =~ ^[0-9]+$ && "$value" -gt 0 ]] || die "${value_name} 必须是正整数，当前为: $value"
done

INPUT="output/mt_rerun_stage5/shards/candidates_shard_${SHARD_PAD}.jsonl"
OUTPUT="output/mt_rerun_stage6/results_shard_${SHARD_PAD}.jsonl"
WORK_DIR="output/mt_rerun_stage6/work_shard_${SHARD_PAD}"
LOG_DIR="output/mt_rerun_stage6/logs"
LOG_FILE="${LOG_DIR}/shard_${SHARD_PAD}_resume.log"
MAIN_PATTERN="scripts/mt/run_mt_candidates.py.*work_shard_${SHARD_PAD}"

[[ -f "$INPUT" ]] || die "找不到输入分片: $INPUT"
mkdir -p "$LOG_DIR"

if pgrep -af "$MAIN_PATTERN" >/tmp/mt_start_existing.$$ 2>/dev/null; then
  echo "shard ${SHARD} 已经有运行中的主进程:"
  cat /tmp/mt_start_existing.$$
  rm -f /tmp/mt_start_existing.$$
  die "请先停止它，或等待它结束。"
fi
rm -f /tmp/mt_start_existing.$$

echo "准备启动 shard ${SHARD} (内部文件编号 ${SHARD_PAD})"
echo "输入: $INPUT"
echo "输出: $OUTPUT"
echo "work-dir: $WORK_DIR"
echo "日志: $LOG_FILE"
echo "参数: max-mutants=$MAX_MUTANTS jobs=$JOBS workers-per-candidate=$WORKERS major-timeout=$MAJOR_TIMEOUT timeout-per-mutant=$TIMEOUT_PER_MUTANT"
echo "模式: --resume"
if [[ "$BACKGROUND" -eq 1 ]]; then
  echo "运行方式: 后台运行，输出追加到日志文件"
else
  echo "运行方式: 前台运行，实时打印日志"
fi

CMD=(
  python scripts/mt/run_mt_candidates.py
  --input "$INPUT"
  --output "$OUTPUT"
  --work-dir "$WORK_DIR"
  --max-mutants "$MAX_MUTANTS"
  --jobs "$JOBS"
  --workers-per-candidate "$WORKERS"
  --major-timeout "$MAJOR_TIMEOUT"
  --timeout-per-mutant "$TIMEOUT_PER_MUTANT"
  --resume
)

if [[ "$BACKGROUND" -eq 1 ]]; then
  nohup "${CMD[@]}" >> "$LOG_FILE" 2>&1 &
  PID=$!
  echo "已后台启动，PID=$PID"
  echo "查看日志:"
  echo "  tail -f $LOG_FILE"
else
  "${CMD[@]}"
fi

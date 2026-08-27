"""汇总第3批次的712条成功任务到一个JSONL文件"""
import json
from pathlib import Path

BASE_DIR = Path("D:/JSpecForge/JSpecForge/linux/redo/2")
OUTPUT = BASE_DIR / "all_success_712.jsonl"

BATCHES = [
    "1-200", "201-400", "401-600", "601-800", "801-1000",
    "1001-1200", "1201-1400", "1401-1600", "1601-1800", "1801-2000", "2001-2124"
]


def main():
    print("汇总所有成功任务...")
    all_success = []

    for batch in BATCHES:
        batch_dir = BASE_DIR / batch / "jsonl"
        for success_file in batch_dir.rglob("success_*.jsonl"):
            for line in success_file.read_text(encoding="utf-8").strip().split("\n"):
                if line:
                    all_success.append(json.loads(line))

    print(f"   收集到 {len(all_success)} 条成功任务")

    # 写出
    with OUTPUT.open("w", encoding="utf-8") as f:
        for task in all_success:
            f.write(json.dumps(task, ensure_ascii=False) + "\n")

    print(f"\n完成！文件已保存: {OUTPUT}")
    print(f"任务数: {len(all_success)}")


if __name__ == "__main__":
    main()

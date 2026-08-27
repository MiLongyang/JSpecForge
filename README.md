# JSpecForge

> 基于大语言模型（LLM）的 Java 方法级 **JML 形式化规约**自动生成与验证流水线。

JSpecForge 读取「自然语言描述 + Java 参考实现」的编程题数据，让 LLM 为每个方法自动编写 [JML](https://www.cs.ucf.edu/~leavens/JML/)（Java Modeling Language）规约，通过**静态（ESC）与动态（RAC+PBT）两条独立验证通道**检验规约正确性，并用**变异测试（MT）**量化规约强度，最终产出一个**带质量标签的、分层的 Java–JML 规约数据集**。

[English README](README_EN.md)

---

## 目录

- [功能特性](#功能特性)
- [系统架构](#系统架构)
- [运行模式](#运行模式)
- [环境与依赖](#环境与依赖)
- [安装与配置](#安装与配置)
- [快速开始](#快速开始)
- [输出产物](#输出产物)
- [项目结构](#项目结构)
- [测试](#测试)

---

## 功能特性

- **LLM 自动生成 JML**：根据自然语言描述与 Java 源码，为方法自动生成 `requires` / `ensures` 等形式化规约；
- **双通道独立验证**：
  - **OJ 通道**：OpenJML **ESC** 静态演绎验证，形式化保证最强；
  - **RP 通道**：OpenJML **RAC** 编译为运行时断言 + **PBT** 基于属性的大量随机输入动态测试，覆盖 ESC 推不出、超时的场景；
- **反馈驱动的闭环迭代**：验证失败的具体证据（语法报错、验证错误、反例输入、前置条件拒绝统计、存活变异体）会精准反馈给 LLM 驱动修订，直到通过或耗尽重试预算；
- **变异测试量化规约强度**：用 Major 生成变异体复验规约，`mutation_score = killed / (killed + survived)`，区分「能验证的弱规约」与「真正强的规约」；
- **分层可信标签**：数据集显式区分 OJ / RP 两种验证来源，下游可按可信层级筛选；
- **工程化能力**：七队列并发、断点恢复、优雅停机、全程过程可追溯。

## 系统架构

系统由**数据库状态 + 七个内存队列**驱动的并发流水线组成：

```text
task_a → oj → rac → input_gen → pbt → mt → final
```

| 队列 | 职责 |
|---|---|
| `task_a` | 调用 LLM 生成 / 修订 JML |
| `oj` | 执行 OpenJML ESC 静态验证并分类 |
| `rac` | 将 JML Java 编译为 RAC 类 |
| `input_gen` | 复制任务级固定输入生成器（**不调用 LLM**） |
| `pbt` | 构建、编译并运行 TestDriver 做动态测试 |
| `mt` | 生成并验证变异体，计算变异分数 |
| `final` | 查询双候选池并写入最终状态 |

单任务核心业务路径：

```text
任务数据（NL 描述 + Java 代码 + 预处理生成器）
   ↓
Task A：LLM 生成/修订带 JML 注解的完整 Java 类
   ↓
JML 提取 + 源码保真校验 + JML 黑名单检查
   ↓
┌─ OJ 通道：ESC 静态验证 ──通过──→ OJ 候选池 ─────────┐
│     └─失败反馈 → Task A 修订                         │
│     └─超时/不支持且 RP 可用 ──→ 转入 RP              │
└─ RP 通道：RAC 编译 → 读取预处理生成器 → PBT 动态测试 │
        └─反例/输入不足反馈 → Task A 修订              │
        └─通过──→ RP 候选池 ──────────────────────────┤
                                                      ↓
                              MT 变异测试（对候选打分）
                                └─存活变异体反馈 → Task A 增强
                                                      ↓
                              Final：查询双候选池 → success / failed
```

**要点**：任一候选池（OJ 或 RP）非空即判 success；OJ 与 RP 候选池同时保留；全程过程记录落库，支持断点恢复。

## 运行模式

由 `config.yaml` 的 `pipeline_mode` 控制（三选一）：

| 模式 | 说明 |
|---|---|
| `full`（默认） | 完整流程：OJ + RP 双通道验证 + 在线 MT 变异测试反馈 |
| `oj_only` | 只走 OpenJML ESC 静态验证，不含 RP 与 MT |
| `oj_rp` | OJ + RP 双通道验证，但不做在线 MT |

## 环境与依赖

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | ≥ 3.10 | 主流水线运行时 |
| JDK | 11 | **Major 3.0.1 只兼容 JDK 11** |
| OpenJML | — | ESC 静态验证 + RAC 运行时断言编译 |
| Major | 3.0.1 | 变异体生成 |
| MySQL | 5.7+ / 8.x | 任务状态与过程记录存储 |
| LLM API | OpenAI 兼容 | 任一符合 OpenAI Chat Completions 协议的供应商 |

Python 包依赖（权威来源为 `pyproject.toml`）：`pyyaml`、`PyMySQL`、`DBUtils`、`requests`、`rich`；测试额外需要 `pytest`。

> **平台说明**：Windows 下 OpenJML 通过 WSL 调用（`platform: "windows"`）；Linux 下全部原生执行（`platform: "linux"`）。

## 安装与配置

### 1. 克隆并安装

```bash
git clone <repo-url>
cd JSpecForge
pip install -e .
# 或仅安装依赖:
pip install pyyaml PyMySQL DBUtils requests rich
```

### 2. 初始化数据库

用建库脚本创建数据库与表结构：

```bash
mysql -u root -p < sql/jspecforge.sql
```

### 3. 准备配置文件

```bash
cp config.yaml.example config.yaml
cp vendors.example.csv vendors.csv
```

编辑 `config.yaml`，关键字段（带【必填】的必须改成你的真实路径/密钥）：

| 字段 | 说明 |
|---|---|
| `platform` | `"windows"` 或 `"linux"` |
| `pipeline_mode` | `"full"` / `"oj_only"` / `"oj_rp"` |
| `paths.*` | 工作目录、临时目录、OpenJML 命令等路径 |
| `database.*` | MySQL 连接 |
| `openjml.cmd` | OpenJML 可执行文件路径【必填】 |
| `llm.vendors_csv` | 指向 `vendors.csv`【必填】 |
| `mt.jdk11_javac` | JDK 11 的 `javac` 完整路径【必填】 |
| `mt.major_home` | Major 3.0.1 安装目录【必填】 |
| `workers.*` | 各阶段 Worker 线程数 |

编辑 `vendors.csv` 填写 LLM 供应商（`vendor_name, base_url, api_key, available_models, ...`），`available_models` 用**分号**分隔多个候选模型。

## 快速开始

### 命令 ① — 只导入数据集到数据库（不跑流水线）

```bash
python -m jspecforge.main load-tasks --jsonl data/data.jsonl
```

常用选项：`--max-rows 5`（只导前 5 行调试）、`--reset-pending`（导入前清空 pending 任务）。

### 命令 ② — 只跑流水线（数据已在库中）

```bash
python -m jspecforge.main run
```

默认是**长驻服务**，跑完不退出、等新任务。

常用选项：

- `--auto-shutdown`：跑完所有任务后自动退出（推荐日常使用）；
- `--idle-seconds 30`：持续空闲多少秒后才退出（默认 30）；
- `--no-monitor`：禁用控制台监控面板；
- `--export-on-finish`：跑完自动导出数据集。

### 命令 ③ — 一体化（导入 + 跑流水线 + 自动退出 + 自动导出）

```bash
python -m jspecforge.main run --load-from data/data.jsonl --auto-shutdown --export-on-finish
```

### 中断恢复

任何时候中断后，直接重新执行命令 ② 即可——系统按数据库 `current_step` 自动恢复 `pending` / `in_progress` 任务，`success` / `failed` 任务跳过。

### 停机

- **`Ctrl+C`**：按一次**优雅停机**（graceful shutdown，Worker 处理完当前任务后退出），按第二次**强制停机**（forced shutdown）；
- **STOP 文件**：在 `paths.stop_file` 配置的路径创建一个名为 `STOP` 的空文件即触发停机（程序退出后记得删除）；
- **`--auto-shutdown`**：跑完自动退出。

### 数据集导出（后处理）

```bash
python scripts/export_dataset.py
```

从数据库读取所有终态任务，联查各过程表组装出 JSONL，输出到 `output/<时间戳>/` 下的 `success_*.jsonl` / `failed_*.jsonl`。

### MT 离线重跑（可选）

在线流水线（`full` 模式）已内联执行 MT。若需对已导出的 JSONL **离线重跑或补充** MT：

```bash
python scripts/run_mt.py --input-dir output/<时间戳>
```

## 输出产物

主流水线导出两个 JSONL 数据集文件：

| 文件 | 内容 |
|---|---|
| `success_<ts>.jsonl` | 成功任务：原始字段 + `final_answer{oj, rp}` + `oj_candidates[]` + `rp_candidates[]`（每个答案带 `full_java_with_jml` 与 `mutation_score`） |
| `failed_<ts>.jsonl` | 失败任务：原始字段 + `failure_info{failure_stage, failure_reason, last_error_detail}` |

`final_answer` 关键子字段：

| 字段 | 说明 |
|---|---|
| `full_java_with_jml` | 最终 JML 注解完整 Java 源码 |
| `mutation_score` | 变异分数（0.0~1.0，-1.0 表示无变异体或异常） |
| `mt_detail` | MT 详情：`mutants_total`（= killed+survived+invalid）、`mutants_generated`（Major 截断前生成数）、`mutants_killed` / `mutants_survived` / `mutants_invalid`、`test_path`、`total_elapsed_seconds` |

### 数据集用途

- **SFT 微调**：用高 MT 分数的 OJ/RP 答案训练 JML 规约生成模型；
- **评测基准**：隐去答案，只给 NL + Code，用 OJ/RP/MT 自动评估被测模型；
- **错误分析**：研究 LLM 规约生成的失败模式分布；
- **工具研究**：分析 ESC 与 RAC/PBT 在 JML 场景中的互补关系；
- **分层使用**：按验证来源（OJ/RP）、MT 分数、分支覆盖率筛选不同可信层级。

## 项目结构

```text
JSpecForge/
├── src/jspecforge/          # 应用源代码
│   ├── main.py              # CLI 入口（run / load-tasks 子命令）
│   ├── config/              # 配置加载与对象
│   ├── core/                # 七队列、Router、Orchestrator、Worker、停机
│   ├── processors/          # 七个阶段的处理器
│   ├── llm/                 # LLM 会话、多供应商客户端、熔断、重试
│   ├── mt/                  # 变异测试：分数计算、OJ/RP 路径、Major 加载
│   ├── tools/               # OpenJML/RAC/PBT TestDriver/Major 等外部工具封装
│   ├── db/                  # 各表 DAO
│   ├── prompts/             # 提示词模板
│   └── monitor/             # 控制台监控面板
├── scripts/                 # 任务加载、数据集导出、MT 等脚本
│   ├── load_tasks.py        # 导入 jsonl 数据集到数据库
│   ├── export_dataset.py    # 从数据库导出 success/failed JSONL
│   ├── run_mt.py            # 对已导出 JSONL 离线重跑 MT
│   └── mt/                  # MT 相关的补充/审计脚本
├── sql/
│   └── jspecforge.sql       # 数据库建库建表脚本
├── templates/               # PBT / MT TestDriver 模板
├── data/                    # 输入数据集（jsonl）
├── tests/                   # pytest 测试套件
├── config.yaml.example      # 配置示例（复制为 config.yaml）
└── vendors.example.csv      # LLM 供应商示例（复制为 vendors.csv）
```

## 测试

```bash
pip install pytest
python -m pytest tests/ -q
```

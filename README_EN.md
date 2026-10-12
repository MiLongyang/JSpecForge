# JSpecForge

> An LLM-based pipeline for automatic generation and verification of method-level **JML formal specifications** for Java.

JSpecForge reads programming-task data consisting of a natural-language description plus a Java reference implementation, prompts an LLM to write [JML](https://www.cs.ucf.edu/~leavens/JML/) (Java Modeling Language) specifications for each method, verifies their correctness through **two independent channels — static (ESC) and dynamic (RAC + PBT)** — and quantifies specification strength with **mutation testing (MT)**. The final output is a **quality-labeled, tiered Java–JML specification dataset**.

[中文 README](README.md)

---

## Table of Contents

- [Features](#features)
- [System Architecture](#system-architecture)
- [Pipeline Modes](#pipeline-modes)
- [Requirements](#requirements)
- [Installation & Configuration](#installation--configuration)
- [Quick Start](#quick-start)
- [Output](#output)
- [Project Layout](#project-layout)

---

## Features

- **LLM-generated JML**: automatically produces formal specifications (`requires` / `ensures`, etc.) for each method from a natural-language description and Java source;
- **Dual-channel independent verification**:
  - **OJ channel**: OpenJML **ESC** static deductive verification — the strongest formal guarantee;
  - **RP channel**: OpenJML **RAC** compiled into runtime assertions + **PBT** property-based testing with large numbers of random inputs — covers cases ESC cannot discharge or times out on;
- **Feedback-driven closed-loop iteration**: concrete failure evidence (syntax diagnostics, verification errors, counterexample inputs, precondition-rejection statistics, surviving mutants) is fed back to the LLM to drive revision, until the spec passes or the retry budget is exhausted;
- **Mutation testing for spec strength**: Major generates mutants that are re-verified against the spec, `mutation_score = killed / (killed + survived)` — distinguishing "weak specs that merely pass" from "genuinely strong specs";
- **Tiered confidence labels**: the dataset explicitly distinguishes the OJ / RP verification sources, so downstream consumers can filter by confidence tier;
- **Engineering capabilities**: seven-queue concurrency, crash recovery, graceful shutdown, and full process traceability.

## System Architecture

The system is a concurrent pipeline driven by **database state + seven in-memory queues**:

```text
task_a → oj → rac → input_gen → pbt → mt → final
```

| Queue | Responsibility |
|---|---|
| `task_a` | Call the LLM to generate / revise JML |
| `oj` | Run OpenJML ESC static verification and classify results |
| `rac` | Compile JML-annotated Java into RAC classes |
| `input_gen` | Copy the task-level fixed input generator (**no LLM call**) |
| `pbt` | Build, compile, and run the TestDriver for dynamic testing |
| `mt` | Generate and verify mutants, compute the mutation score |
| `final` | Query both candidate pools and write the final state |

Core business path of a single task:

```text
Task data (NL description + Java code + pre-built generator)
   ↓
Task A: LLM generates/revises the full JML-annotated Java class
   ↓
JML extraction + source-fidelity check + JML blacklist check
   ↓
┌─ OJ channel: ESC static verification ──pass──→ OJ candidate pool ─┐
│     └─failure feedback → Task A revises                            │
│     └─timeout/unsupported & RP available ──→ fall into RP          │
└─ RP channel: RAC compile → load pre-built generator → PBT test     │
        └─counterexample / insufficient-input feedback → Task A      │
        └─pass──→ RP candidate pool ────────────────────────────────┤
                                                                     ↓
                          MT mutation testing (scores candidates)
                            └─surviving-mutant feedback → Task A
                                                                     ↓
                          Final: query both pools → success / failed
```

**Key points**: any non-empty candidate pool (OJ or RP) means success; both candidate pools are retained; all process records are persisted, with crash recovery supported.

## Pipeline Modes

Controlled by `pipeline_mode` in `config.yaml` (choose one):

| Mode | Description |
|---|---|
| `full` (default) | Full pipeline: OJ + RP dual-channel verification + online MT feedback |
| `oj_only` | OpenJML ESC static verification only; no RP, no MT |
| `oj_rp` | OJ + RP dual-channel verification, but no online MT |

## Requirements

| Dependency | Version | Notes |
|---|---|---|
| Python | ≥ 3.10 | Main pipeline runtime |
| JDK | 11 | **Major 3.0.1 only works with JDK 11** |
| OpenJML | — | ESC static verification + RAC runtime-assertion compilation |
| Major | 3.0.1 | Mutant generation |
| MySQL | 5.7+ / 8.x | Task-state and process-record storage |
| LLM API | OpenAI-compatible | Any vendor implementing the OpenAI Chat Completions protocol |

Python package dependencies (authoritative source: `pyproject.toml`): `pyyaml`, `PyMySQL`, `DBUtils`, `requests`, `rich`.

> **Platform note**: on Windows, OpenJML is invoked through WSL (`platform: "windows"`); on Linux everything runs natively (`platform: "linux"`).

## Installation & Configuration

### 1. Clone and install

```bash
git clone <repo-url>
cd JSpecForge
pip install -e .
# or install dependencies only:
pip install pyyaml PyMySQL DBUtils requests rich
```

### 2. Initialize the database

Create the database and tables with the schema script:

```bash
mysql -u root -p < sql/jspecforge.sql
```

### 3. Prepare configuration files

```bash
cp config.yaml.example config.yaml
cp vendors.example.csv vendors.csv
```

Edit `config.yaml`. Key fields (those marked 【required】 must be set to your real paths/keys):

| Field | Description |
|---|---|
| `platform` | `"windows"` or `"linux"` |
| `pipeline_mode` | `"full"` / `"oj_only"` / `"oj_rp"` |
| `paths.*` | Workspace dir, tmp dirs, OpenJML command, etc. |
| `database.*` | MySQL connection |
| `openjml.cmd` | Path to the OpenJML executable 【required】 |
| `llm.vendors_csv` | Points to `vendors.csv` 【required】 |
| `mt.jdk11_javac` | Full path to JDK 11 `javac` 【required】 |
| `mt.major_home` | Major 3.0.1 install directory 【required】 |
| `workers.*` | Per-stage Worker thread counts |

Edit `vendors.csv` to fill in your LLM vendors (`vendor_name, base_url, api_key, available_models, ...`); separate multiple candidate models in `available_models` with **semicolons**.

## Quick Start

### Command ① — Load a dataset into the DB only (no pipeline)

```bash
python -m jspecforge.main load-tasks --jsonl data/data.jsonl
```

Common options: `--max-rows 5` (load only the first 5 rows for debugging), `--reset-pending` (clear pending tasks before loading).

### Command ② — Run the pipeline only (data already in DB)

```bash
python -m jspecforge.main run
```

By default this is a **long-running service** — it does not exit when done, waiting for new tasks.

Common options:

- `--auto-shutdown`: exit automatically after all tasks finish (recommended for daily use);
- `--idle-seconds 30`: how many seconds of idleness before exiting (default 30);
- `--no-monitor`: disable the console monitoring dashboard;
- `--export-on-finish`: export the dataset automatically when done.

### Command ③ — All-in-one (load + run + auto-exit + auto-export)

```bash
python -m jspecforge.main run --load-from data/data.jsonl --auto-shutdown --export-on-finish
```

### Crash recovery

After any interruption, simply re-run Command ② — the system resumes `pending` / `in_progress` tasks from the database `current_step`, and skips `success` / `failed` tasks.

### Shutdown

- **`Ctrl+C`**: once for a **graceful shutdown** (Workers exit after finishing their current task), twice for a **forced shutdown**;
- **STOP file**: create an empty file named `STOP` at the path configured in `paths.stop_file` to trigger shutdown (remember to delete it after the process exits);
- **`--auto-shutdown`**: exit automatically when finished.

### Dataset export (post-processing)

```bash
python scripts/export_dataset.py
```

Reads all terminal-state tasks from the database, joins the process tables to assemble JSONL, and writes `success_*.jsonl` / `failed_*.jsonl` under `output/<timestamp>/`.

### Offline MT re-run (optional)

The online pipeline (`full` mode) already runs MT inline. To **re-run or supplement** MT on an already-exported JSONL offline:

```bash
python scripts/run_mt.py --input-dir output/<timestamp>
```

## Output

The main pipeline exports two JSONL dataset files:

| File | Contents |
|---|---|
| `success_<ts>.jsonl` | Successful tasks: original fields + `final_answer{oj, rp}` + `oj_candidates[]` + `rp_candidates[]` (each answer carries `full_java_with_jml` and `mutation_score`) |
| `failed_<ts>.jsonl` | Failed tasks: original fields + `failure_info{failure_stage, failure_reason, last_error_detail}` |

Key `final_answer` sub-fields:

| Field | Description |
|---|---|
| `full_java_with_jml` | The final JML-annotated full Java source |
| `mutation_score` | Mutation score (0.0–1.0; -1.0 means no mutants or an exception) |
| `mt_detail` | MT details: `mutants_total` (= killed+survived+invalid), `mutants_generated` (pre-truncation count from Major), `mutants_killed` / `mutants_survived` / `mutants_invalid`, `test_path`, `total_elapsed_seconds` |

### What the dataset can be used for

- **SFT fine-tuning**: train JML-generation models on high-mutation-score OJ/RP answers;
- **Evaluation benchmark**: hide the answers, give only NL + Code, and evaluate a model automatically with OJ/RP/MT;
- **Error analysis**: study the failure-mode distribution of LLM spec generation;
- **Tooling research**: analyze the complementary relationship between ESC and RAC/PBT on JML;
- **Tiered usage**: filter by verification source (OJ/RP), mutation score, and branch coverage for different confidence tiers.

## Project Layout

```text
JSpecForge/
├── src/jspecforge/          # Application source code
│   ├── main.py              # CLI entry (run / load-tasks subcommands)
│   ├── config/              # Config loading and objects
│   ├── core/                # Seven queues, Router, Orchestrator, Worker, shutdown
│   ├── processors/          # Processors for the seven stages
│   ├── llm/                 # LLM sessions, multi-vendor client, circuit breaker, retry
│   ├── mt/                  # Mutation testing: scoring, OJ/RP paths, Major loading
│   ├── tools/               # Wrappers for OpenJML/RAC/PBT TestDriver/Major, etc.
│   ├── db/                  # DAOs for each table
│   ├── prompts/             # Prompt templates
│   └── monitor/             # Console monitoring dashboard
├── scripts/                 # Task loading, dataset export, MT, etc.
│   ├── load_tasks.py        # Load a jsonl dataset into the database
│   ├── export_dataset.py    # Export success/failed JSONL from the database
│   ├── run_mt.py            # Offline MT re-run on an exported JSONL
│   └── mt/                  # Supplementary/audit scripts for MT
├── sql/
│   └── jspecforge.sql       # Database schema script
├── templates/               # PBT / MT TestDriver templates
├── data/                    # Input dataset (jsonl)
├── config.yaml.example      # Config template (copy to config.yaml)
└── vendors.example.csv      # LLM vendor template (copy to vendors.csv)
```

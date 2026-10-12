# Experimental Artifacts

This directory contains the experimental datasets that support the findings
reported in the paper. All evaluation runs were performed on 688 method-level
tasks preprocessed from FormalBench (Le-Cong et al., 2025).

## Input dataset

- `formalbench_final_688.jsonl` — the 688 tasks used in the evaluation. Each
  record contains the natural-language description (`nl_description`), the
  reference method (`method_code`), the surrounding class (`full_code`), a
  task-level input generator (`input_generator_code`), and branch-coverage
  metadata (`branch_coverage`). Tasks were admitted when the fixed input
  generator reached at least 90% branch coverage.

## Result datasets

The `results/` directory contains per-task outcomes exported from the MySQL
databases behind the experiments, using `scripts/export_dataset.py`. LLM
dialogue history has been omitted; the retained fields describe the task, the
final generated specification, and the verification / mutation evidence.

- `results/full/` — JSpecForge, the full framework (extended static checking,
  runtime-verification revision path, and mutation testing).
  598 of 688 tasks verified (86.9%).
- `results/norp/` — the ablated system with the runtime-verification revision
  path (RP) disabled.
  312 of 688 tasks verified.

Each of the two directories contains:

- `success.jsonl` — one record per verified task (`final_answer` holds the
  generated JML specification).
- `failed.jsonl` — one record per task that did not reach a verified
  specification (`final_answer` is `null`).

## Record schema

Key fields per record:

| Field | Meaning |
| --- | --- |
| `source_dataset_name` / `source_task_id` / `source_dataset_index` | provenance in the source dataset |
| `nl_description` | natural-language specification intent |
| `method_code` / `full_code` | the reference method and its surrounding class |
| `input_generator_code` | task-level input generator |
| `branch_coverage` | branch coverage of the input generator |
| `final_answer` | the generated JML specification (`null` when failed) |
| `oj_attempts` | extended-static-checking (ESC) attempt records |
| `rac_attempts` | runtime-assertion-checking (RAC) attempt records |
| `pbt_attempts` | property-based-testing / mutation attempt records |
| `oj_candidates` / `rp_candidates` | candidate specification pools |
| `failure_info` / `global_summary` | per-task failure reason / run summary |

Reproduction scripts live under `scripts/`; see the top-level `README.md` for
setup and usage.
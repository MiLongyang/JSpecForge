"""变异测试（MT）后处理模块。

依据 ``docs/design/变异测试设计方案.md``，MT 是独立于主流水线的后处理脚本：

1. 读取 ``success.jsonl``
2. 对每条成功记录：用 Major 生成变异体 → 按原路径（OJ / RP）重验 → 算分
3. 回填 ``final_answer.mutation_score`` / ``mt_detail`` / ``pbt_attempts[].mutation_score``
4. 输出 ``success_with_mt.jsonl``

两条 MT 验证路径（design §3）：

- **OJ 路径**：对变异体重跑 ESC，ESC 通过 = survived，ESC 失败 = killed
- **RP 路径**：对变异体重跑 RAC 编译 + TestDriver 动态测试
"""

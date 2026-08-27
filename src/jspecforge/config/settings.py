"""配置数据类定义。

每个 YAML section 对应一个 frozen dataclass，由 AppConfig 聚合持有全部子配置。
字段名、类型、默认值严格对齐 docs/design/config.yaml配置规格.md。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class EnvironmentConfig:
    """运行环境配置。"""

    mode: str = "windows_wsl"
    wsl_distro: str = "Ubuntu"


@dataclass(frozen=True)
class PathsConfig:
    """文件路径配置。所有目录启动时由 ensure_paths() 自动创建。"""

    workspace_dir: str = ""
    tmp_dir: str = ""
    java_source_dir: str = ""
    rac_output_dir: str = ""
    pbt_bin_dir: str = ""
    lib_dir: str = ""
    output_dir: str = ""
    log_dir: str = ""
    prompts_dir: str = ""
    template_dir: str = ""
    stop_file: str = ""
    blacklist_file: str = ""
    openjml_java_cmd: str = ""


@dataclass(frozen=True)
class DatabaseConfig:
    """MySQL 数据库连接配置。"""

    host: str = "localhost"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "jspecforge"
    charset: str = "utf8mb4"
    pool_size: int = 20
    connect_timeout: int = 10
    read_timeout: int = 30
    write_timeout: int = 30


@dataclass(frozen=True)
class OpenJMLConfig:
    """OpenJML (ESC + RAC) 配置。

    ``startup_optimize`` 控制是否在 ``-J-Xmx`` 之外追加一组 JVM 启动开销
    优化参数（详见 docs/plan/P7-编排器.md "实际落地说明"中的 hot-fix 说明）：

    - ``-J-XX:TieredStopAtLevel=1``：只用 C1 JIT，跳过 C2 完整优化
    - ``-J-Xverify:none``：跳过字节码校验
    - ``-J-XX:+UseParallelGC``：吞吐量优先 GC
    - ``-J-XX:CICompilerCount=2``：限制 JIT 编译线程数

    OJ ESC 与 RAC 都是"短任务 + 启动密集"，开启后单次响应时间显著下降。
    若发现 OJ 行为异常可改 false 关闭，无需改代码。
    """

    cmd: str = ""
    jvm_heap_max: str = "1536m"
    esc_extra_args: List[str] = field(
        default_factory=lambda: [
            "--code-math=bigint",
            "--spec-math=bigint",
            "--nullable-by-default",
        ]
    )
    rac_extra_args: List[str] = field(
        default_factory=lambda: ["--nullable-by-default"]
    )
    timeout: int = 180
    rac_timeout: int = 180
    timeout_threshold_seconds: int = 60
    startup_optimize: bool = True


@dataclass(frozen=True)
class JavaConfig:
    """Java 工具 (javac / java) 配置。

    ``javac_startup_optimize`` 控制是否给 javac 追加 ``-J-Xverify:none``
    以跳过字节码校验,小幅缩短启动开销。**不**追加 ``TieredStopAtLevel=1``——
    javac 是编译器自身,关闭 JIT 反而拖慢编译;且 ``-Xverify:none`` 仅影响
    javac 进程自己的字节码加载,不影响它对源码的检查。
    """

    java_cmd: str = "java"
    javac_cmd: str = "javac"
    test_driver_jvm_args: List[str] = field(
        default_factory=lambda: [
            "-Dorg.jmlspecs.openjml.rac=exception",
            "-ea",
            "-Dfile.encoding=UTF-8",
            "-Dstdout.encoding=UTF-8",
            "-Dstderr.encoding=UTF-8",
        ]
    )
    test_driver_heap_max: str = "512m"
    javac_timeout: int = 30
    test_driver_timeout: int = 60
    javac_startup_optimize: bool = True


@dataclass(frozen=True)
class LLMConfig:
    """LLM 调用配置。"""

    vendors_csv: str = ""
    request_timeout: int = 180
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
    max_tokens: int = 4096
    temperature: float = 0.7
    proxy_address: Optional[str] = None
    max_concurrent_global: int = 10
    max_concurrent_per_vendor: int = 3
    thinking_model_keywords: List[str] = field(
        default_factory=lambda: ["deepseek-r1", "thinking", "reasoning"]
    )
    thinking_tag: str = ""
    thinking_end_tag: str = ""
    model_cooldown_seconds: int = 300
    cooldown_trigger_failures: int = 2
    http_retry_max: int = 2
    rate_limit_wait_seconds: int = 10


@dataclass(frozen=True)
class PBTConfig:
    """PBT 动态测试配置。

    ``startup_optimize`` 控制是否给 TestDriver JVM 追加 ``-Xverify:none``
    以跳过字节码校验。**不**追加 ``TieredStopAtLevel=1`` —— PBT TestDriver
    要跑 ``test_runs`` 次（默认 1000）循环,需要 JIT 完整优化才跑得快;
    关闭 JIT 会让 PBT 单次执行变慢 3~10 倍。

    ``test_driver_threads`` (Phase 4 / 2026-05-14): TestDriver 内部并行
    线程数。1 = 单线程(与历史完全等价); >1 = 把 totalRuns 均分给 N 个
    Java Thread, 每线程独立 Random 子种子(``seed XOR golden_ratio*tid``),
    聚合写入 ConcurrentHashMap + AtomicInteger。8 线程在 mirrorReflection
    类的密集调用上能压到原耗时的 1/4~1/3。注意: 该并行度跟 Python 端的
    ``workers.pbt`` 是两层独立控制 —— Python worker 数决定同时跑几个
    task, 这里决定每个 task 内 TestDriver 用几个 JVM 线程。
    """

    test_runs: int = 1000
    valid_input_min_ratio: float = 0.2
    failure_sample_max: int = 5
    early_stop_on_fail: bool = False
    early_stop_max_fails: int = 50
    startup_optimize: bool = True
    # Phase 4 (2026-05-14): TestDriver 并行线程数, 详见类 docstring。
    test_driver_threads: int = 1


@dataclass(frozen=True)
class MTConfig:
    """变异测试配置（独立后处理脚本使用）。

    P3 阶段补入 ``jdk11_javac`` 与 ``major_home``：Major 3.0.1 仅兼容 JDK 11，
    必须显式指定路径，不得依赖系统 PATH（gotchas §2.1 / §2.2）。两平台都
    原生执行（不走 WSL）。
    """

    enabled: bool = True
    workers: int = 4
    operators: List[str] = field(
        default_factory=lambda: ["AOR", "ROR", "COR", "SBR", "BVR"]
    )
    max_mutants_per_method: int = 50
    timeout_per_mutant: int = 30
    jvm_heap_max: str = "512m"
    # JDK 11 javac 完整路径。Windows 例：D:/JDK/jdk-11.0.25/bin/javac.exe
    # Linux 例：/usr/lib/jvm/jdk-11/bin/javac
    jdk11_javac: str = ""
    # Major 安装目录（包含 bin/ lib/ mml/ 等）。
    # Windows 例：D:/JSpecForge/JSpecForge/lib/major-3.0.1_jre11/major
    # Linux 例：/opt/major-3.0.1_jre11/major
    major_home: str = ""
    # JVM 启动开销优化（与 OJ/RAC/PBT 同一套参数，通过 -J 透传给底层 JVM）。
    # Major 每个变异体都重新启动 JVM 跑测试套件，启动密集+短任务，开启收益大。
    startup_optimize: bool = True
    # P11: MT 反馈循环配置
    score_threshold: float = 0.80  # MT 分数达标阈值
    oj_feedback_max_rounds: int = 3  # OJ 路径 MT 反馈最大轮次
    rp_feedback_max_rounds: int = 3  # RP 路径 MT 反馈最大轮次
    terminate_on_threshold: bool = True  # 达标后是否立即终止
    feedback_max_survived: int = 25  # 反馈提示词中最多包含的存活变异体数


@dataclass(frozen=True)
class RetryConfig:
    """重试上限配置。所有 _max 值表示最大总尝试次数（含首次）。

    Phase 3 新增 (2026-05-13):
        - ``a_round_max``: Task A 总轮次上限, 替代 ``task_a_max_rounds``。
          Phase 3 仅引入字段, Phase 4 切换路由逻辑使用此字段。
        - ``rac_error_pattern_max``: OJ ESC 已通过语法校验后 RAC 仍编译失败时,
          同一错误模式 *连续* 出现次数上限, 达此值标 failed_oj_bug 终止。
          (2026-05-16: 仅黑名单跳过 OJ 的路径不参与本计数, 保持 syntax 重试。)
        - ``rac_oj_bug_total_max``: 同上前提下不区分 pattern 的累计次数上限,
          用于拦截不同 OJ 工具错误交替出现导致连续计数器永远清零的死循环。
        - ``task_a_logic_max`` 语义变更: 原为 OJ 逻辑失败计数, 改为 PBT 后条件
          失败计数 (Phase 4 切换触发点)。

    Phase 3 废弃 (保留字段避免破坏既有代码, Phase 4 清理):
        - ``task_a_max_rounds``: DEPRECATED, 由 ``a_round_max`` 替代。
        - ``input_gen_max_per_a_round`` / ``input_gen_total_max``:
          Phase 4 移除 InputGenProcessor 后整体清掉。

    InfraError 处理 (P7 hot-fix):
        - ``infra_error_wait_seconds``: 单次 InfraError 后等待几秒再重试
        - ``infra_error_max_per_task``: 同一任务最多重试几次,达上限后任务**保持
          status=pending**(不标 failed),仅写 last_error_detail。下次启动自动恢复
        - ``infra_error_global_max``: 滑动窗口内全局 InfraError 累计上限,达上限
          触发系统级优雅停机(set shutdown_event)
        - ``infra_error_global_window_seconds``: 全局计数滑动窗口长度(秒),
          窗口外的旧记录自然滚出,避免误触发停机

    PBT TestDriver 编译失败重试 (D.8 阶段引入, 2026-05-03):
        - ``pbt_compile_max_consecutive``: 单任务连续 PBT TestDriver 编译失败上限
          (类似 task_a_syntax_consecutive_max), 超限触发 final
        - ``pbt_compile_max_total``: 单任务累计 PBT TestDriver 编译失败上限
          (类似 task_a_syntax_total_max)
        与 task_a syntax 完全独立——PBT 编译失败回 input_gen 阶段重新生成
        generateValidInput 方法, **不**消耗 task_a 计数器, **不**回 task_a。

    PBT 有效输入不足独立计数器 (P10 Phase 4 引入, 2026-05-13):
        - ``task_a_insufficient_input_consecutive_max``: 连续有效输入不足次数上限。
          PBT ``valid_ratio >= min_ratio`` 时 consecutive 重置为 0。
        - ``task_a_insufficient_input_total_max``: 累计有效输入不足次数上限。
        与 ``task_a_logic_max`` (PBT 后置条件失败) 独立计数, 防止两类信号混淆。
        触发终止时 ``failure_stage='failed_pbt_insufficient_input'``。
    """

    # DEPRECATED (Phase 3 标记, Phase 4 已不再被代码读取):
    #   原作 Task A 总轮次上限, 现由 a_round_max 替代。字段保留仅为兼容旧 YAML。
    task_a_max_rounds: int = 10
    # 语义变更 (Phase 3): PBT 后条件失败最大次数 (Phase 4 切换触发点)
    task_a_logic_max: int = 5
    task_a_syntax_consecutive_max: int = 3
    task_a_syntax_total_max: int = 20
    # P13: OJ/RP 候选上限，各自独立、不共享。
    #   pbt_pass_count_max: RP(PBT) 100% 通过候选累计上限（原 3，P13 提至 5）
    #   oj_pass_count_max:  OJ ESC 通过候选累计上限（P13 新增，默认 5）
    #   - OJ 满 → 任务终止成功
    #   - RP 满 → 不终止，仅关闭该任务 RP 通道，继续逼 OJ
    pbt_pass_count_max: int = 5
    oj_pass_count_max: int = 5
    # DEPRECATED (Phase 3): Phase 4 移除 InputGenProcessor 时一并清理
    input_gen_max_per_a_round: int = 3
    input_gen_total_max: int = 30
    pbt_compile_max_consecutive: int = 3
    pbt_compile_max_total: int = 10
    # Phase 3 新增: Task A 总轮次上限 (替代 task_a_max_rounds)
    a_round_max: int = 20
    source_fidelity_max: int = 3
    # Phase 3 新增: 同一 RAC 错误模式上限, 触发 failed_oj_bug
    rac_error_pattern_max: int = 3
    # 2026-05-16 新增: 不区分 pattern 的 OJ-bug 累计次数上限
    rac_oj_bug_total_max: int = 8
    # Phase 4 新增 (2026-05-13): PBT 有效输入不足独立计数器上限
    task_a_insufficient_input_consecutive_max: int = 2
    task_a_insufficient_input_total_max: int = 5
    infra_error_wait_seconds: int = 5
    infra_error_max_per_task: int = 2
    infra_error_global_max: int = 20
    infra_error_global_window_seconds: int = 600


@dataclass(frozen=True)
class BlacklistConfig:
    """JML 黑名单配置。

    完整设计见 docs/design/核心工作流程设计.md §7。

    - ``v1``：预置黑名单，永久启用，命中即跳过 OJ 直接进 RP
    - ``protected``：OJ 实际支持的关键字，**绝对不允许**进入运行时黑名单
      （防止动态扩充把 ``\\forall`` 等误吸入）
    - ``dynamic_persist_file``：动态黑名单持久化文件。启动时读入合并到运行时
      set；进程退出时（atexit + SIGTERM/SIGINT）把"运行期间新增的部分"序列化
      为 JSON 写回。设为 ``None`` 禁用持久化
    """

    v1: List[str] = field(
        default_factory=lambda: [
            "\\sum",
            "\\product",
            "\\num_of",
            "\\max",
            "\\min",
            "\\count",
            "\\big_or",
        ]
    )
    protected: List[str] = field(
        default_factory=lambda: [
            "\\forall",
            "\\exists",
            "\\result",
            "\\old",
            "\\typeof",
            "\\nothing",
        ]
    )
    dynamic_persist_file: Optional[str] = None


@dataclass(frozen=True)
class WorkersConfig:
    """Worker 线程数配置。与 LLM 并发是两层独立控制。"""

    task_a: int = 4
    openjml_esc: int = 3
    rac: int = 2
    input_gen: int = 3
    pbt: int = 3
    mt: int = 2
    final: int = 2


@dataclass(frozen=True)
class TaskLoadingConfig:
    """任务加载配置。"""

    batch_size: int = 50
    queue_low_watermark: int = 10
    source_machine: str = "local"
    max_active_tasks: int = 0  # 全局最大活跃任务数，0=不限制


@dataclass(frozen=True)
class MonitorConfig:
    """监控面板配置。"""

    enabled: bool = True
    refresh_interval: int = 3
    show_queue_depth: bool = True
    show_circuit_breaker: bool = True
    show_retry_stats: bool = True


@dataclass(frozen=True)
class LoggingConfig:
    """日志配置。"""

    level: str = "INFO"
    console_level: str = "INFO"
    filename: str = "jspecforge.log"
    max_file_size_mb: int = 50
    backup_count: int = 5
    format: str = "detailed"


@dataclass(frozen=True)
class ExportConfig:
    """数据导出配置。"""

    float_precision: int = 4
    include_llm_history: bool = True
    include_call_meta: bool = True
    include_failure_samples: bool = True


@dataclass(frozen=True)
class CleanupConfig:
    """清理配置。"""

    auto_cleanup_on_complete: bool = False
    cleanup_on_startup: bool = False


@dataclass(frozen=True)
class ErrorLogConfig:
    """报错日志截断配置（P6 处理器使用）。

    Task A / RAC / PBT / InputGen 的报错回喂给 LLM 重试时必须截断，避免
    超长报错占用过多 prompt token。各类报错独立配置上限。

    - ``openjml_max_lines``: OJ ESC stdout/stderr 最大保留行数
    - ``rac_max_lines``: RAC 编译错误最大保留行数
    - ``compile_error_max_lines``: input_gen wrapper 类 javac 编译错误最大保留行数
    - ``pbt_compile_error_max_lines``: D.6 (2026-05-03) PBT TestDriver
      javac/java 失败错误最大保留行数 (跟 input_gen wrapper 区分,因为
      TestDriver 报错通常更长——含模板代码)
    - ``failure_sample_max_chars``: PBT 失败样本格式化后的总字符数上限

    P6 处理器在调用 ``prompts.render(...)`` 之前用这些配置截断字符串
    （由 ``processors._format_helpers.truncate_error_log`` 等函数完成）。
    """

    openjml_max_lines: int = 50
    rac_max_lines: int = 50
    compile_error_max_lines: int = 50
    pbt_compile_error_max_lines: int = 80
    failure_sample_max_chars: int = 4000


@dataclass(frozen=True)
class AppConfig:
    """顶层配置对象，聚合所有子配置。"""

    platform: str = "windows"
    pipeline_mode: str = "full"  # "full" | "oj_only" | "oj_rp"
    # P13: OJ 失败后是否进入 RP 兜底路径。
    #   True  = 当前主流程行为（OJ logic/timeout/unsupported/黑名单 → 进 RP）
    #   False = 不走 RP（OJ 失败回 task_a 重试；黑名单当不存在，继续送 OJ）
    # 与运行时 per-task 的 task.rp_disabled 取或：任一为"关闭"即不走 RP。
    rp_fallback_enabled: bool = True
    environment: EnvironmentConfig = field(default_factory=EnvironmentConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    openjml: OpenJMLConfig = field(default_factory=OpenJMLConfig)
    java: JavaConfig = field(default_factory=JavaConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    pbt: PBTConfig = field(default_factory=PBTConfig)
    mt: MTConfig = field(default_factory=MTConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    blacklist: BlacklistConfig = field(default_factory=BlacklistConfig)
    workers: WorkersConfig = field(default_factory=WorkersConfig)
    task_loading: TaskLoadingConfig = field(default_factory=TaskLoadingConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    error_log: ErrorLogConfig = field(default_factory=ErrorLogConfig)

    def validate(self) -> List[str]:
        """校验配置合法性，返回警告列表；非法时抛出 ConfigValidationError。

        委托给 jspecforge.config.loader.validate_config 执行。
        """
        from jspecforge.config.loader import validate_config

        return validate_config(self)

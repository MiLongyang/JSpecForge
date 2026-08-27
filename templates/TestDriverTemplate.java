// JSpecForge PBT TestDriver 模板
// 占位符（test_driver_builder 注入时替换）：
//   __CLASS_NAME__               → 全限定测试驱动类名（如 TestDriver_42_0）
//   PLACEHOLDER_INPUT_GENERATOR  → LLM 生成的 generateValidInput 方法
//   PLACEHOLDER_METHOD_CALL      → 单行调用目标方法的 Java 代码
//
// 设计依据：docs/design/外部工具调用技术规格.md §5.3 / §5.4 / §5.5 / §5.6
//
// 必须用 -Dorg.jmlspecs.openjml.rac=exception JVM 参数运行，否则 RAC 不抛
// JmlAssertionError，本驱动捕获不到 postcondition 违反。
//
// args 协议（Phase 4 / 2026-05-14 增加 args[5]=threadCount）：
//   args[0] totalRuns         int
//   args[1] earlyStopOnFail   boolean
//   args[2] earlyStopMaxFails int
//   args[3] maxInputsPerError int
//   args[4] seed              long  (可选)
//   args[5] threadCount       int   (可选, 缺省=1 即单线程, 与旧版完全等价)

import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;
import java.math.*;        // P13 fix: BigInteger/BigDecimal support
import java.time.*;        // P13 fix: LocalDate/LocalDateTime support
import org.jmlspecs.runtime.JmlAssertionError;

public class __CLASS_NAME__ {

    PLACEHOLDER_INPUT_GENERATOR

    // P10: 样本分组类 - 相同错误信息合并输入
    // 多线程访问时, count++ 与 inputs.add(...) 必须 synchronized(this) 保护
    static class GroupedSample {
        String error;  // 完整错误信息
        List<String> inputs = new ArrayList<String>();
        int count = 0;
    }

    public static void main(String[] args) throws Exception {
        int totalRuns = Integer.parseInt(args[0]);
        boolean earlyStopOnFail = Boolean.parseBoolean(args[1]);
        int earlyStopMaxFails = Integer.parseInt(args[2]);
        int maxInputsPerError = Integer.parseInt(args[3]);
        long seed = (args.length >= 5) ? Long.parseLong(args[4]) : System.nanoTime();
        // Phase 4 (2026-05-14): 线程数, 由配置 pbt.test_driver_threads 透传
        int threadCount = (args.length >= 6) ? Integer.parseInt(args[5]) : 1;
        if (threadCount < 1) threadCount = 1;
        if (totalRuns <= 0) {
            outputJson(0, 0, 0, 0, 0, seed,
                    new LinkedHashMap<String, Integer>(),
                    new LinkedHashMap<String, Map<String, GroupedSample>>());
            return;
        }
        if (threadCount > totalRuns) threadCount = totalRuns;

        // 共享聚合 - 全部走 lock-free 的 atomic / concurrent 容器
        final AtomicInteger genErrorCount = new AtomicInteger();
        final AtomicInteger preSkipCount = new AtomicInteger();
        final AtomicInteger postFailCount = new AtomicInteger();
        final AtomicInteger validInputs = new AtomicInteger();
        final ConcurrentHashMap<String, AtomicInteger> failureCounts =
                new ConcurrentHashMap<String, AtomicInteger>();
        final ConcurrentHashMap<String, ConcurrentHashMap<String, GroupedSample>> groupedFailureSamples =
                new ConcurrentHashMap<String, ConcurrentHashMap<String, GroupedSample>>();
        final AtomicBoolean stopFlag = new AtomicBoolean(false);

        int chunkBase = totalRuns / threadCount;
        int remainder = totalRuns % threadCount;

        Thread[] workers = new Thread[threadCount];
        for (int t = 0; t < threadCount; t++) {
            final int runs = chunkBase + (t < remainder ? 1 : 0);
            // 不同线程 seed 互不重叠且与 (seed, threadCount) 一一对应,保留确定性
            final long subSeed = seed ^ (0x9E3779B97F4A7C15L * ((long) t + 1L));
            final boolean fEarlyStopOnFail = earlyStopOnFail;
            final int fEarlyStopMaxFails = earlyStopMaxFails;
            final int fMaxInputsPerError = maxInputsPerError;
            workers[t] = new Thread(new Runnable() {
                @Override
                public void run() {
                    runChunk(runs, subSeed, fEarlyStopOnFail, fEarlyStopMaxFails,
                            fMaxInputsPerError, genErrorCount, preSkipCount,
                            postFailCount, validInputs, failureCounts,
                            groupedFailureSamples, stopFlag);
                }
            }, "pbt-worker-" + t);
            workers[t].start();
        }
        for (Thread w : workers) {
            w.join();
        }

        // 转回 LinkedHashMap 以保留与旧版兼容的输出形态;
        // key 顺序按字典序排序,保证给定 (seed, threadCount) 输出稳定可 diff
        Map<String, Integer> finalCounts = new LinkedHashMap<String, Integer>();
        List<String> sortedClauses = new ArrayList<String>(failureCounts.keySet());
        java.util.Collections.sort(sortedClauses);  // P13 fix: FQN to avoid shadowing
        for (String c : sortedClauses) {
            finalCounts.put(c, failureCounts.get(c).get());
        }
        Map<String, Map<String, GroupedSample>> finalSamples =
                new LinkedHashMap<String, Map<String, GroupedSample>>();
        List<String> sampleClauses = new ArrayList<String>(groupedFailureSamples.keySet());
        java.util.Collections.sort(sampleClauses);  // P13 fix: FQN to avoid shadowing
        for (String c : sampleClauses) {
            finalSamples.put(c, new LinkedHashMap<String, GroupedSample>(groupedFailureSamples.get(c)));
        }

        outputJson(totalRuns, validInputs.get(), postFailCount.get(),
                preSkipCount.get(), genErrorCount.get(), seed,
                finalCounts, finalSamples);
    }

    // 单个 worker 线程跑 runs 次循环。命中 stopFlag 后立即退出;
    // 失败样本写入共用 ConcurrentHashMap, GroupedSample 内部用 synchronized 保护。
    private static void runChunk(
            int runs, long subSeed,
            boolean earlyStopOnFail, int earlyStopMaxFails, int maxInputsPerError,
            AtomicInteger genErrorCount, AtomicInteger preSkipCount,
            AtomicInteger postFailCount, AtomicInteger validInputs,
            ConcurrentHashMap<String, AtomicInteger> failureCounts,
            ConcurrentHashMap<String, ConcurrentHashMap<String, GroupedSample>> groupedFailureSamples,
            AtomicBoolean stopFlag) {
        Random random = new Random(subSeed);
        for (int i = 0; i < runs; i++) {
            if (earlyStopOnFail && stopFlag.get()) {
                return;
            }
            Object[] input;
            try {
                input = generateValidInput(random);
            } catch (Throwable t) {
                genErrorCount.incrementAndGet();
                continue;
            }

            try {
                PLACEHOLDER_METHOD_CALL
                validInputs.incrementAndGet();
            } catch (JmlAssertionError jae) {
                if (jae instanceof JmlAssertionError.Precondition) {
                    preSkipCount.incrementAndGet();
                } else {
                    validInputs.incrementAndGet();
                    int newPostFail = postFailCount.incrementAndGet();
                    String clause = extractClause(jae);

                    AtomicInteger cnt = failureCounts.get(clause);
                    if (cnt == null) {
                        AtomicInteger nv = new AtomicInteger();
                        AtomicInteger old = failureCounts.putIfAbsent(clause, nv);
                        cnt = (old != null) ? old : nv;
                    }
                    cnt.incrementAndGet();

                    ConcurrentHashMap<String, GroupedSample> errorMap = groupedFailureSamples.get(clause);
                    if (errorMap == null) {
                        ConcurrentHashMap<String, GroupedSample> nm = new ConcurrentHashMap<String, GroupedSample>();
                        ConcurrentHashMap<String, GroupedSample> old = groupedFailureSamples.putIfAbsent(clause, nm);
                        errorMap = (old != null) ? old : nm;
                    }
                    String errorKey = normalizeError(jae.toString());
                    GroupedSample gs = errorMap.get(errorKey);
                    if (gs == null) {
                        GroupedSample ng = new GroupedSample();
                        ng.error = jae.toString();
                        GroupedSample old = errorMap.putIfAbsent(errorKey, ng);
                        gs = (old != null) ? old : ng;
                    }
                    synchronized (gs) {
                        gs.count++;
                        if (gs.inputs.size() < maxInputsPerError) {
                            gs.inputs.add(formatInput(input));
                        }
                    }

                    if (earlyStopOnFail && newPostFail >= earlyStopMaxFails) {
                        stopFlag.set(true);
                    }
                }
            } catch (Throwable t) {
                // 非 JML 异常(NPE 等):design §5.3 视为有效输入但不计 postcondition 失败
                validInputs.incrementAndGet();
            }
        }
    }

    // P10: 标准化错误信息，用于分组（去掉位置信息中的具体行号等可能变化的细节）
    private static String normalizeError(String error) {
        // 简单方案：取错误信息的前200字符作为 key（足够区分不同错误）
        // 保留错误类型和规约信息，去掉可能变化的行号细节
        if (error.length() > 200) {
            return error.substring(0, 200);
        }
        return error;
    }

    private static String extractClause(Throwable e) {
        String msg = e.getMessage();
        if (msg == null) {
            return "unknown";
        }
        if (!(e instanceof JmlAssertionError.Precondition)) {
            int idx = msg.indexOf("Associated declaration:");
            if (idx >= 0) {
                String tail = msg.substring(idx);
                String line = extractJmlLine(tail);
                if (line != null) {
                    return line;
                }
            }
        }
        String line = extractJmlLine(msg);
        if (line != null) {
            return line;
        }
        // Hot-fix #33 (2026-05-16): RAC 抛出的 JmlAssertionError.getMessage()
        // 不一定包含可识别的 JML 关键字行(只有 file:line: + "Postcondition is
        // false" 之类的 stack-style 输出)。原实现一律返回 "unknown" 会把
        // 不同 clause 失败聚到同一个桶, 喂给 LLM 失去定位价值。
        // 兜底策略: 取 message 首行有意义内容(优先含 file:line + 错误类型),
        // 截 200 字符当 clause key, 既保留可区分性又不致过长。
        String fallback = firstMeaningfulLine(msg);
        if (fallback != null && !fallback.isEmpty()) {
            if (fallback.length() > 200) {
                fallback = fallback.substring(0, 200);
            }
            return fallback;
        }
        return "unknown";
    }

    private static String firstMeaningfulLine(String msg) {
        for (String line : msg.split("\n")) {
            String t = line.trim();
            if (t.isEmpty()) continue;
            // 跳过纯指针行(只含 ^ 与空格)
            boolean onlyCaret = true;
            for (int i = 0; i < t.length(); i++) {
                char c = t.charAt(i);
                if (c != '^' && c != ' ' && c != '\t') {
                    onlyCaret = false;
                    break;
                }
            }
            if (onlyCaret) continue;
            return t;
        }
        return null;
    }

    private static String extractJmlLine(String text) {
        // P10 fix: 扩展匹配逻辑，支持更多 JML 关键字
        // 原问题：只匹配 //@ 开头的行，但 JmlAssertionError 消息中
        // JML 行可能是 "ensures \result == x * 2;" 格式，不以 //@ 开头
        String[] jmlKeywords = {
            "ensures", "requires", "assignable", "invariant",
            "signals", "measured_by", "decreases", "loop_invariant"
        };
        for (String line : text.split("\n")) {
            String trimmed = line.trim();
            // 匹配标准 JML 注解格式
            if (trimmed.startsWith("//@") || trimmed.startsWith("@")
                    || trimmed.startsWith("/*@")) {
                return trimmed;
            }
            // P10: 匹配 JML 关键字开头的行
            for (String kw : jmlKeywords) {
                if (trimmed.startsWith(kw + " ") || trimmed.startsWith(kw + "(")) {
                    return trimmed;
                }
            }
        }
        return null;
    }

    private static int[] __jsf_asIntArray(Object value) {
        if (value == null || value instanceof int[]) return (int[]) value;
        if (value instanceof Integer[]) {
            Integer[] src = (Integer[]) value;
            int[] out = new int[src.length];
            for (int i = 0; i < src.length; i++) out[i] = src[i];
            return out;
        }
        return (int[]) value;
    }

    private static long[] __jsf_asLongArray(Object value) {
        if (value == null || value instanceof long[]) return (long[]) value;
        if (value instanceof Long[]) {
            Long[] src = (Long[]) value;
            long[] out = new long[src.length];
            for (int i = 0; i < src.length; i++) out[i] = src[i];
            return out;
        }
        return (long[]) value;
    }

    private static double[] __jsf_asDoubleArray(Object value) {
        if (value == null || value instanceof double[]) return (double[]) value;
        if (value instanceof Double[]) {
            Double[] src = (Double[]) value;
            double[] out = new double[src.length];
            for (int i = 0; i < src.length; i++) out[i] = src[i];
            return out;
        }
        return (double[]) value;
    }

    private static float[] __jsf_asFloatArray(Object value) {
        if (value == null || value instanceof float[]) return (float[]) value;
        if (value instanceof Float[]) {
            Float[] src = (Float[]) value;
            float[] out = new float[src.length];
            for (int i = 0; i < src.length; i++) out[i] = src[i];
            return out;
        }
        return (float[]) value;
    }

    private static boolean[] __jsf_asBooleanArray(Object value) {
        if (value == null || value instanceof boolean[]) return (boolean[]) value;
        if (value instanceof Boolean[]) {
            Boolean[] src = (Boolean[]) value;
            boolean[] out = new boolean[src.length];
            for (int i = 0; i < src.length; i++) out[i] = src[i];
            return out;
        }
        return (boolean[]) value;
    }

    private static char[] __jsf_asCharArray(Object value) {
        if (value == null || value instanceof char[]) return (char[]) value;
        if (value instanceof Character[]) {
            Character[] src = (Character[]) value;
            char[] out = new char[src.length];
            for (int i = 0; i < src.length; i++) out[i] = src[i];
            return out;
        }
        return (char[]) value;
    }

    private static byte[] __jsf_asByteArray(Object value) {
        if (value == null || value instanceof byte[]) return (byte[]) value;
        if (value instanceof Byte[]) {
            Byte[] src = (Byte[]) value;
            byte[] out = new byte[src.length];
            for (int i = 0; i < src.length; i++) out[i] = src[i];
            return out;
        }
        return (byte[]) value;
    }

    private static short[] __jsf_asShortArray(Object value) {
        if (value == null || value instanceof short[]) return (short[]) value;
        if (value instanceof Short[]) {
            Short[] src = (Short[]) value;
            short[] out = new short[src.length];
            for (int i = 0; i < src.length; i++) out[i] = src[i];
            return out;
        }
        return (short[]) value;
    }

    private static int[][] __jsf_asInt2DArray(Object value) {
        if (value == null || value instanceof int[][]) return (int[][]) value;
        Object[] src = (Object[]) value;
        int[][] out = new int[src.length][];
        for (int i = 0; i < src.length; i++) out[i] = __jsf_asIntArray(src[i]);
        return out;
    }

    private static long[][] __jsf_asLong2DArray(Object value) {
        if (value == null || value instanceof long[][]) return (long[][]) value;
        Object[] src = (Object[]) value;
        long[][] out = new long[src.length][];
        for (int i = 0; i < src.length; i++) out[i] = __jsf_asLongArray(src[i]);
        return out;
    }

    private static double[][] __jsf_asDouble2DArray(Object value) {
        if (value == null || value instanceof double[][]) return (double[][]) value;
        Object[] src = (Object[]) value;
        double[][] out = new double[src.length][];
        for (int i = 0; i < src.length; i++) out[i] = __jsf_asDoubleArray(src[i]);
        return out;
    }

    private static float[][] __jsf_asFloat2DArray(Object value) {
        if (value == null || value instanceof float[][]) return (float[][]) value;
        Object[] src = (Object[]) value;
        float[][] out = new float[src.length][];
        for (int i = 0; i < src.length; i++) out[i] = __jsf_asFloatArray(src[i]);
        return out;
    }

    private static boolean[][] __jsf_asBoolean2DArray(Object value) {
        if (value == null || value instanceof boolean[][]) return (boolean[][]) value;
        Object[] src = (Object[]) value;
        boolean[][] out = new boolean[src.length][];
        for (int i = 0; i < src.length; i++) out[i] = __jsf_asBooleanArray(src[i]);
        return out;
    }

    private static char[][] __jsf_asChar2DArray(Object value) {
        if (value == null || value instanceof char[][]) return (char[][]) value;
        Object[] src = (Object[]) value;
        char[][] out = new char[src.length][];
        for (int i = 0; i < src.length; i++) out[i] = __jsf_asCharArray(src[i]);
        return out;
    }

    private static byte[][] __jsf_asByte2DArray(Object value) {
        if (value == null || value instanceof byte[][]) return (byte[][]) value;
        Object[] src = (Object[]) value;
        byte[][] out = new byte[src.length][];
        for (int i = 0; i < src.length; i++) out[i] = __jsf_asByteArray(src[i]);
        return out;
    }

    private static short[][] __jsf_asShort2DArray(Object value) {
        if (value == null || value instanceof short[][]) return (short[][]) value;
        Object[] src = (Object[]) value;
        short[][] out = new short[src.length][];
        for (int i = 0; i < src.length; i++) out[i] = __jsf_asShortArray(src[i]);
        return out;
    }

    private static String formatSample(Object[] input, Throwable t) {
        StringBuilder sb = new StringBuilder();
        sb.append("input=");
        sb.append(formatInput(input));
        sb.append("; error=");
        sb.append(t.toString());
        return sb.toString();
    }

    private static String formatInput(Object[] input) {
        if (input == null) {
            return "null";
        }
        StringBuilder sb = new StringBuilder("[");
        for (int i = 0; i < input.length; i++) {
            if (i > 0) {
                sb.append(", ");
            }
            Object v = input[i];
            if (v == null) {
                sb.append("null");
            } else if (v.getClass().isArray()) {
                sb.append(formatArray(v));
            } else {
                sb.append(v.toString());
            }
        }
        sb.append("]");
        return sb.toString();
    }

    private static String formatArray(Object arr) {
        // P13 fix: use FQN to avoid shadowing by local var/param named "Arrays"
        if (arr instanceof int[]) {
            return java.util.Arrays.toString((int[]) arr);
        }
        if (arr instanceof long[]) {
            return java.util.Arrays.toString((long[]) arr);
        }
        if (arr instanceof double[]) {
            return java.util.Arrays.toString((double[]) arr);
        }
        if (arr instanceof float[]) {
            return java.util.Arrays.toString((float[]) arr);
        }
        if (arr instanceof boolean[]) {
            return java.util.Arrays.toString((boolean[]) arr);
        }
        if (arr instanceof char[]) {
            return java.util.Arrays.toString((char[]) arr);
        }
        if (arr instanceof byte[]) {
            return java.util.Arrays.toString((byte[]) arr);
        }
        if (arr instanceof short[]) {
            return java.util.Arrays.toString((short[]) arr);
        }
        if (arr instanceof Object[]) {
            return java.util.Arrays.toString((Object[]) arr);
        }
        return arr.toString();
    }

    private static void outputJson(
            int totalRuns, int validInputs, int postFailCount, int preSkipCount,
            int genErrorCount, long seed,
            Map<String, Integer> counts,
            Map<String, Map<String, GroupedSample>> groupedSamples) {
        StringBuilder sb = new StringBuilder();
        sb.append("{");
        sb.append("\"total_runs\":").append(totalRuns).append(",");
        sb.append("\"valid_inputs\":").append(validInputs).append(",");
        sb.append("\"post_fail_count\":").append(postFailCount).append(",");
        sb.append("\"precondition_reject_count\":").append(preSkipCount).append(",");
        sb.append("\"gen_error_count\":").append(genErrorCount).append(",");
        sb.append("\"seed\":").append(seed).append(",");
        sb.append("\"failure_counts\":{");
        boolean first = true;
        for (Map.Entry<String, Integer> entry : counts.entrySet()) {
            if (!first) {
                sb.append(",");
            }
            first = false;
            sb.append("\"").append(escapeJson(entry.getKey())).append("\":");
            sb.append(entry.getValue());
        }
        sb.append("},");
        // P10: 新格式 - 按clause分组，每组内按错误分组，合并输入
        sb.append("\"failure_samples\":{");
        first = true;
        for (Map.Entry<String, Map<String, GroupedSample>> clauseEntry : groupedSamples.entrySet()) {
            if (!first) {
                sb.append(",");
            }
            first = false;
            sb.append("\"").append(escapeJson(clauseEntry.getKey())).append("\":[");
            boolean first2 = true;
            for (GroupedSample gs : clauseEntry.getValue().values()) {
                if (!first2) {
                    sb.append(",");
                }
                first2 = false;
                sb.append("{");
                sb.append("\"error\":\"").append(escapeJson(gs.error)).append("\",");
                sb.append("\"inputs\":[");
                boolean first3 = true;
                for (String inp : gs.inputs) {
                    if (!first3) {
                        sb.append(",");
                    }
                    first3 = false;
                    sb.append("\"").append(escapeJson(inp)).append("\"");
                }
                sb.append("],");
                sb.append("\"count\":").append(gs.count);
                sb.append("}");
            }
            sb.append("]");
        }
        sb.append("}");
        sb.append("}");
        System.out.println(sb.toString());
    }

    private static String escapeJson(String s) {
        if (s == null) {
            return "";
        }
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    sb.append("\\\"");
                    break;
                case '\\':
                    sb.append("\\\\");
                    break;
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        return sb.toString();
    }
}

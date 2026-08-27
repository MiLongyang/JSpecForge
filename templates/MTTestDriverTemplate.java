// JSpecForge MT (变异测试) 专用 TestDriver 模板
// 占位符（mt.rp_path_runner 注入时替换；下方用全角字符避开 .replace 误伤）：
//   ＿＿CLASS_NAME＿＿               → 全限定测试驱动类名（如 MTTestDriver_007_0）
//   PLACEHOLDER＿INPUT＿GENERATOR    → 主流水线已验证的 generateValidInput 方法（来自 pbt_attempts）
//   PLACEHOLDER＿METHOD＿CALL        → 单行调用目标方法的 Java 代码
//
// 与主流水线 TestDriverTemplate.java 的关键差异：
//   1. 新增 otherExceptionCount 计数器：单独统计非 JML 异常（NPE / ArrayIndex 等）
//   2. catch(Throwable) 分支不再累加 validInputs，避免污染分母
//   3. JSON 输出多一个 "other_exception_count" 字段
//
// 设计依据：docs/design/变异测试设计方案.md §3.2
//   "其他异常（NullPointerException、ArrayIndexOutOfBoundsException）→
//    视为代码运行异常，不算 killed，标记为 invalid"
//
// 主流水线 TestDriverTemplate 把 NPE 算 valid_inputs（吞掉），那是为了主流水线
// 判断"输入生成器是否合理"的需求。MT 评估的是"JML 规约对变异体的识别力"，
// 当变异让方法在 JVM 层面崩溃时，JML postcondition 还来不及检查，这种变异
// 不能用来评判规约强弱，应标 invalid 不计入分母。
//
// 必须用 -Dorg.jmlspecs.openjml.rac=exception JVM 参数运行，否则 RAC 不抛
// JmlAssertionError，本驱动捕获不到 postcondition 违反。

import java.util.*;
import org.jmlspecs.runtime.JmlAssertionError;

public class __CLASS_NAME__ {

    PLACEHOLDER_INPUT_GENERATOR

    public static void main(String[] args) {
        int totalRuns = Integer.parseInt(args[0]);
        boolean earlyStopOnFail = Boolean.parseBoolean(args[1]);
        int earlyStopMaxFails = Integer.parseInt(args[2]);
        int maxSamplesPerClause = Integer.parseInt(args[3]);
        long seed = (args.length >= 5) ? Long.parseLong(args[4]) : System.nanoTime();

        Random random = new Random(seed);
        int genErrorCount = 0;
        int preSkipCount = 0;
        int postFailCount = 0;
        int otherExceptionCount = 0;   // MT 新增：非 JML 异常计数（NPE / ArrayIndex 等）
        int validInputs = 0;
        Map<String, Integer> failureCounts = new LinkedHashMap<String, Integer>();
        Map<String, List<String>> failureSamples = new LinkedHashMap<String, List<String>>();

        for (int i = 0; i < totalRuns; i++) {
            Object[] input;
            try {
                input = generateValidInput(random);
            } catch (Throwable t) {
                genErrorCount++;
                continue;
            }

            try {
                PLACEHOLDER_METHOD_CALL
                validInputs++;
            } catch (JmlAssertionError jae) {
                if (jae instanceof JmlAssertionError.Precondition) {
                    preSkipCount++;
                } else {
                    validInputs++;
                    postFailCount++;
                    String clause = extractClause(jae);
                    failureCounts.put(clause, failureCounts.getOrDefault(clause, 0) + 1);
                    List<String> samples = failureSamples.get(clause);
                    if (samples == null) {
                        samples = new ArrayList<String>();
                        failureSamples.put(clause, samples);
                    }
                    if (samples.size() < maxSamplesPerClause) {
                        samples.add(formatSample(input, jae));
                    }
                    if (earlyStopOnFail && postFailCount >= earlyStopMaxFails) {
                        break;
                    }
                }
            } catch (Throwable t) {
                // MT 关键差异：单独计数非 JML 异常，不再累加 validInputs
                // 这样后续分类时能区分"方法正常完成"和"方法在 JVM 层崩溃"
                otherExceptionCount++;
            }
        }

        outputJson(totalRuns, validInputs, postFailCount, preSkipCount, genErrorCount,
                otherExceptionCount, seed, failureCounts, failureSamples);
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
        return line != null ? line : "unknown";
    }

    private static String extractJmlLine(String text) {
        for (String line : text.split("\n")) {
            String trimmed = line.trim();
            if (trimmed.startsWith("//@") || trimmed.startsWith("@")
                    || trimmed.startsWith("/*@")) {
                return trimmed;
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
        if (arr instanceof int[]) {
            return Arrays.toString((int[]) arr);
        }
        if (arr instanceof long[]) {
            return Arrays.toString((long[]) arr);
        }
        if (arr instanceof double[]) {
            return Arrays.toString((double[]) arr);
        }
        if (arr instanceof float[]) {
            return Arrays.toString((float[]) arr);
        }
        if (arr instanceof boolean[]) {
            return Arrays.toString((boolean[]) arr);
        }
        if (arr instanceof char[]) {
            return Arrays.toString((char[]) arr);
        }
        if (arr instanceof byte[]) {
            return Arrays.toString((byte[]) arr);
        }
        if (arr instanceof short[]) {
            return Arrays.toString((short[]) arr);
        }
        if (arr instanceof Object[]) {
            return Arrays.toString((Object[]) arr);
        }
        return arr.toString();
    }

    private static void outputJson(
            int totalRuns, int validInputs, int postFailCount, int preSkipCount,
            int genErrorCount, int otherExceptionCount, long seed,
            Map<String, Integer> counts, Map<String, List<String>> samples) {
        StringBuilder sb = new StringBuilder();
        sb.append("{");
        sb.append("\"total_runs\":").append(totalRuns).append(",");
        sb.append("\"valid_inputs\":").append(validInputs).append(",");
        sb.append("\"post_fail_count\":").append(postFailCount).append(",");
        sb.append("\"precondition_reject_count\":").append(preSkipCount).append(",");
        sb.append("\"gen_error_count\":").append(genErrorCount).append(",");
        sb.append("\"other_exception_count\":").append(otherExceptionCount).append(",");
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
        sb.append("\"failure_samples\":{");
        first = true;
        for (Map.Entry<String, List<String>> entry : samples.entrySet()) {
            if (!first) {
                sb.append(",");
            }
            first = false;
            sb.append("\"").append(escapeJson(entry.getKey())).append("\":[");
            boolean first2 = true;
            for (String s : entry.getValue()) {
                if (!first2) {
                    sb.append(",");
                }
                first2 = false;
                sb.append("\"").append(escapeJson(s)).append("\"");
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

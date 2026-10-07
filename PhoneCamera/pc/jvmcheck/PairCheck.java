import com.mouse.phoneprobe.PairCode;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.List;
import kotlin.Pair;

/**
 * 直接调用 **App 的解析器** `PairCode`，并跑它的内建自检向量。
 *
 * 关键在于它跑的是**真的那份 Kotlin 代码**（`app/build/tmp/kotlin-classes/debug` 里的 class），
 * 不是"照着它再写一遍"的镜像实现 —— 镜像实现只能证明我抄得对。
 * `PairCode` 刻意不依赖任何 Android 类（纯 Kotlin），所以能在普通 JVM 上直接跑；
 * 这也是"解析逻辑与相机/传输解耦"的一个附带好处：**它可被自动验证**。
 *
 * 用法：`java -cp <...> PairCheck <文本文件> [期望 host:port|token | --expect-fail=关键词]`
 *   退出码：0 通过 / 3 结果与期望不符 / 4 自检向量有失败项 / 5 期望被拒但没拒
 *
 * ⚠ 输出统一走 UTF-8 的 PrintStream：Windows 上 JVM 默认按 GBK 写管道，
 *   中文 reason 会变成乱码，而判据的输出是给人看的。
 */
public final class PairCheck {
    public static void main(String[] args) throws Exception {
        PrintStream out = new PrintStream(new FileOutputStream(FileDescriptor.out), true,
                StandardCharsets.UTF_8);
        String text = new String(Files.readAllBytes(Paths.get(args[0])), StandardCharsets.UTF_8).trim();
        PairCode.Result r = PairCode.INSTANCE.parse(text);
        StringBuilder sb = new StringBuilder();
        sb.append("ok=").append(r.getOk())
          .append(" host=").append(r.getHost())
          .append(" port=").append(r.getPort())
          .append(" token=").append(r.getToken())
          .append(" source=").append(r.getSource())
          .append(" warnings=").append(r.getWarnings());

        // 第二个参数是**模式**：`--expect-fail` 或 `期望 host:port|token`（空 = 只打印）。
        // ⚠ 参数下标要数清：`java -cp <cp> PairCheck <file> <mode>` ⇒ args[0]=文件、args[1]=模式。
        //    第一版写成 `args.length > 2 && args[2]...` ⇒ 选项永远被忽略，反向用例静默变成正例
        //    （现象：期望被拒却报 PARSE-FAIL，测试"失败在一个与缺陷无关的地方"）。
        String mode = args.length > 1 ? args[1] : "";

        // 期望被拒（误扫防线的反向用例）。
        // ⚠ 关键词不通过命令行传：Windows 上中文参数的编码不确定，
        //    判据会因为"参数乱码"这种和被测对象无关的原因失败。原因文本照原样打印，
        //    由调用方（Python）按 UTF-8 解码后自己检查关键词。
        if (mode.equals("--expect-fail")) {
            if (r.getOk()) {
                out.println("EXPECTED-FAIL 但解析成功了：" + sb);
                System.exit(5);
            }
            out.println("REJECTED-AS-EXPECTED 原因=" + r.getReason());
            return;
        }

        if (!r.getOk()) {
            out.println("PARSE-FAIL 原因=" + r.getReason() + "  " + sb);
            System.exit(3);
        }
        if (!mode.isEmpty()) {
            String got = r.getHost() + ":" + r.getPort() + "|" + r.getToken();
            if (!mode.equals(got)) {
                out.println("MISMATCH 期望 " + mode + "，实得 " + got + "  " + sb);
                System.exit(3);
            }
        }

        Pair<Integer, List<String>> st = PairCode.INSTANCE.runSelfTest();
        int total = st.getFirst();
        List<String> fails = st.getSecond();
        sb.append(" | 内建自检 ").append(total - fails.size()).append("/").append(total);
        if (!fails.isEmpty()) {
            sb.append(" 失败项：").append(fails);
            out.println(sb);
            System.exit(4);
        }
        out.println(sb);
    }
}

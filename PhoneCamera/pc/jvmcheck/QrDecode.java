import com.google.zxing.BinaryBitmap;
import com.google.zxing.LuminanceSource;
import com.google.zxing.MultiFormatReader;
import com.google.zxing.RGBLuminanceSource;
import com.google.zxing.Result;
import com.google.zxing.common.HybridBinarizer;
import java.awt.image.BufferedImage;
import java.io.FileDescriptor;
import java.io.FileOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import javax.imageio.ImageIO;

/**
 * 用**App 里那个 zxing jar**（`app/libs/core-3.5.3.jar`）解码一张二维码图，打印原文。
 *
 * 为什么要专门做一个：扫码的最后一步是"图像 → 字符串"，如果这里用别的库或自己写解码，
 * 验证的就只是"我这边能解"，而非"App 能解"。用**同一个 jar**、同一套二值化器才有意义。
 *
 * 两种输入（都是**被测系统真实产出的形态**，不是为测试另造的）：
 *   · `*.pgm`（P2 ASCII 灰度）—— 契约验证用（`pc/verify_pair_contract.py`）；
 *   · 其它图片（PNG/…）—— 走 `ImageIO`，**直接吃服务端 `/api/v1/pair` 给的 `qr_png_b64`**。
 *     ⚠ 用 ImageIO 而不是 zxing 的 `BufferedImageLuminanceSource`：那个类在
 *     **javase** 包里，而本项目只带了 **core**（`core-3.5.3.jar`，纯 Java、无 awt）。
 *     自己把 `BufferedImage` 的 RGB 数组喂给 core 里的 `RGBLuminanceSource` 就够了。
 *
 * 用法：`java -Djava.awt.headless=true -cp <...> QrDecode <file>`
 *      （stdout 打印解出的文本，不带换行）
 */
public final class QrDecode {
    public static void main(String[] args) throws Exception {
        if (args.length < 1) {
            System.err.println("用法：QrDecode <file.pgm|file.png>");
            System.exit(1);
        }
        String path = args[0];
        LuminanceSource src;
        if (path.toLowerCase().endsWith(".pgm")) {
            src = fromPgm(path);
        } else {
            BufferedImage im = ImageIO.read(Paths.get(path).toFile());
            if (im == null) {
                throw new IllegalArgumentException("ImageIO 读不出这张图（不支持或文件损坏）："
                        + path);
            }
            int w = im.getWidth(), h = im.getHeight();
            int[] px = im.getRGB(0, 0, w, h, null, 0, w);
            src = new RGBLuminanceSource(w, h, px);
        }
        // 与 QrScanActivity 用同一套二值化器（HybridBinarizer 优先）
        Result r = new MultiFormatReader().decode(new BinaryBitmap(new HybridBinarizer(src)));
        // UTF-8 显式输出：Windows 下 JVM 默认按 GBK 写管道，中文错误信息会乱码
        PrintStream out = new PrintStream(new FileOutputStream(FileDescriptor.out), true,
                StandardCharsets.UTF_8);
        out.print(r.getText());
    }

    private static LuminanceSource fromPgm(String path) throws Exception {
        String[] tok = new String(Files.readAllBytes(Paths.get(path)), StandardCharsets.UTF_8)
                .trim().split("\\s+");
        if (!"P2".equals(tok[0])) {
            throw new IllegalArgumentException("只接受 P2（ASCII 灰度）PGM，实际是 " + tok[0]);
        }
        int w = Integer.parseInt(tok[1]);
        int h = Integer.parseInt(tok[2]);
        int max = Integer.parseInt(tok[3]);
        if (tok.length < 4 + w * h) {
            throw new IllegalArgumentException("像素数不足：header " + w + "x" + h
                    + "，实际给了 " + (tok.length - 4));
        }
        int[] px = new int[w * h];
        for (int i = 0; i < w * h; i++) {
            int v = Integer.parseInt(tok[4 + i]) * 255 / max;
            px[i] = (v << 16) | (v << 8) | v;          // 灰度铺成 RGB
        }
        return new RGBLuminanceSource(w, h, px);
    }
}

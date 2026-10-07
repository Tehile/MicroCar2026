package com.mouse.phoneprobe

import android.annotation.SuppressLint
import android.content.Context
import android.graphics.ImageFormat
import android.graphics.Rect
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.CaptureResult
import android.hardware.camera2.TotalCaptureResult
import android.media.Image
import android.media.ImageReader
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.HandlerThread
import android.os.StatFs
import android.os.SystemClock
import android.util.Size
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.File
import java.io.InputStream
import java.io.InputStreamReader
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URL
import java.security.MessageDigest
import java.util.concurrent.atomic.AtomicBoolean
import java.util.zip.CRC32

/**
 * 交付会话（M1）：连续流 + 按 `SENSOR_TIMESTAMP` 抽帧交付（D24）。
 *
 * 与 M0 探针的分工 —— **刻意不改 `LockedCaptureProbe`**：
 *   · 探针是 M0 的证据来源（②③④⑤⑥ 的报告都出自它），判据 `capture_path_versions.json`
 *     按路径记 `rev`。在探针里加东西会动到那条证据链；
 *   · 本类只做"现场交付"这一件事：按 profile 开流 → 抽帧 → 落本地队列 → 分片上传 → 等 ACK 删。
 * 两者的请求参数与锁定配方保持一致（同一个 `burst_yuv_manual` 语义，见 PATH_ID/PATH_REV）。
 *
 * 设计依据：
 *   · §5.6 待传队列与断网容错（D13/D14）：先落本地持久化队列，再上传；**只有 ACK 才删**
 *   · §5.4 profile：记**回读值**，并把 `capture_path_id/rev`、`crop_region`、`skew` 一起带上
 *   · §6.3 帧级元数据：字段名与 PC 端 `pc/server.py` 严格对齐
 *   · §7.1/§7.2 协议：WS 控制 + HTTP 分片（init / PUT Content-Range + X-Chunk-Crc32 / complete）
 *
 * 三个实现上的坑（都是本项目已经踩过的，写在这里免得重犯）：
 *  1. **图与结果是两条回调通道，先后不保证** ⇒ 抽帧时不能"在结果回调里等图"。
 *     这里用"近几帧的 Y 平面小缓存 + 待抽集合"两侧兜住（同 R28 的离线配对思路）。
 *  2. **`Image` 的 `rowStride` 可能大于宽度**（补齐行）⇒ 必须按 stride 逐行取，
 *     并在元数据里记下 `y_stride` 与是否重排过，否则下游按 width 读会整体错位。
 *  3. **连续流里"每帧的请求时刻"没有定义**（只下发过一次重复请求）⇒ 不伪造
 *     `sensor_ts_minus_request_ns`，改记 `sensor_ts_minus_stream_start_ns` 并说明语义。
 */
class DeliverySession(
    private val ctx: Context,
    private val onLog: (String) -> Unit,
    private val onStatus: (JSONObject) -> Unit,
    private val onSessionId: (String) -> Unit,
    /** 连接结果回调：(成功?, 说明)。**必须有它** —— 否则连不上时界面"没有任何反应"，
     *  用户只能靠猜（实测就踩过：服务没跑，App 静默装作连上了）。 */
    private val onConnectResult: (Boolean, String) -> Unit,
    /**
     * 预览回调：(灰度位图, 叠加文字)。
     *
     * **为什么用"已采到的 Y 平面"渲染，而不是加一个预览 Surface**：
     * 往会话里再加一个 Surface 就改变了**会话配置**，按本项目的规则那是一处采集行为变更
     * （要动 `capture_path_versions.json` 的 rev，并让既有证据作废）。而"把已经采到的
     * Y 平面降采样画到界面上"是**纯显示**：不进 HAL 请求、不进元数据、不进指纹。
     * ⇒ 预览的代价是几毫秒 CPU，收益是现场能把目标摆进画面（否则只能盲拍）。
     */
    private val onPreview: (android.graphics.Bitmap, String) -> Unit,
) {
    companion object {
        // 交付路径是**独立的一条**（不是 burst_yuv_manual 的复用）：
        // 请求层与它差两个键 —— ① 显式 CONTROL_ENABLE_ZSL=false（② 不设，靠 HAL 默认，
        // 回读双方都是 false）；② SENSOR_FRAME_DURATION 由 profile.frame_interval_ms 决定，
        // 默认**不发**（② 恒发 30 fps 对应的值）。丢弃规则也不同（warmup + 按间隔抽帧；
        // **第 24 轮起默认不抽帧 = "每一帧都留"**），见 tools/capture_path_versions.json。
        //    "新增一条路径不得让其它路径的证据作废"（R20/R21）——所以 ②③⑥ 的 rev 一个都没动。
        const val PATH_ID = "stream_deliver_yuv"
        // ⚠ v1.10.7 **记账更正**：登记表在 1.10.3 就把本路径记为 rev=2（默认档改 0.6×，
        // 那是请求参数变更），但代码这一行没跟着改，于是 1.10.3~1.10.6 的帧**宣称 rev=1
        // 而实际行为是 rev=2**。登记表存在的意义就是"声明必须为真"，声明错了比没声明更坏
        // （看着有，实际对不上）。本次按登记表改回 2；1.10.7 自身只改元数据**标签格式**，
        // 不改请求参数/会话配置/模板/输出格式/调度/丢弃规则 ⇒ 不构成 rev 变更。
        // ⚠ v1.13.0 **rev 2 → 3**：默认档由「每 200 ms 抽 1 帧」改成「**不抽帧 = 每一帧都留**」
        // （交付道与归档道合并后的唯一语义，见 docs/对外接口与单一交付道.md）。为什么算
        // 采集行为变更：**丢弃规则**变了 ⇒ 默认配置下产出的数据量差约 6 倍（单帧 2 764 800 B，
        // 30.04 fps ⇒ 83.0 MB/s），交付内容的含义变了。先例是 1.10.3 把默认变焦 1.0× 改 0.6×
        // （同为"默认值改变 ⇒ rev +1"）。⇒ 1.13.0 起的交付数据与 1.10.3~1.12.0 的**不得互相
        // 替代**（后者是按 0.6× + 抽帧产的）。登记表必须同步为 3（"声明必须为真"）。
        const val PATH_REV = 3
        const val DELIVERY = "stream"

        // ---------------------------------------------------- 采集模式（第 27 轮）
        //
        // `deliver`（默认）= 每一帧都留（现状）；`live` = **只看不留**：只实时回传，
        // 不储存、也不在事后回传额外的数据，**允许彩色**。
        //
        // ⚠ 这个模式**跑完不留任何东西**（R38）：不用它做任何测量/定位 —— 画面是
        // 有损 JPEG，而且没有逐帧 sha256、没有 `frames/`、`delivery_complete` 判"不适用"。
        const val MODE_DELIVER = "deliver"
        const val MODE_LIVE = "live"
        /**
         * 实时道是**另一条采集路径**：产出的是**彩色 JPEG**，与 `stream_deliver_yuv`
         * （原始 Y 平面）根本不是同一种东西 ⇒ **不能用同一个 id 的 rev 表达**
         * （先例：1.13.0 改默认抽帧率时改了 rev，因为"丢弃规则与产出量"变了）。
         * 登记表 `tools/capture_path_versions.json` 里有对应条目；既有路径的 rev 一个没动。
         */
        const val LIVE_PATH_ID = "live_stream_color"
        // ⚠ v1.15.0 **rev 1 → 2**：新增 `color_source` 维度。`rgba_hal` 档走
        //   `FLEX_RGBA_8888` reader ⇒ YUV→RGB 由**相机 HAL** 完成，本进程只下采样+编码。
        //   为什么 rev +1 而不是新开一个 path id：它仍是 `live_stream_color`，
        //   只是同一路径的**参数化子档**（与 `live_quality`/`live_width` 同性质）。
        //   为什么必须 +1：**产出像素变了**（自算 RGB vs HAL 算 RGB）——
        //   而 rev 的作用就是"同一条路径下产出是否可互相替代"，这里显然不可。
        //   ⚠ 交付道的 `stream_deliver_yuv`（rev=3）**一个字节都没动**。
        const val LIVE_PATH_REV = 2
        /** 实时道默认档 960×720 q70（链路实测 5.5 MB/s 下 ≈119 KB/帧 ⇒ 约 46 fps 余量）。 */
        const val LIVE_DEF_WIDTH = 960
        const val LIVE_DEF_QUALITY = 70

        // ---- 实时道的**颜色来源**（第 29 轮：二分定位"绿偏"到底是谁的锅）----
        //
        // 现场：live 画面整体偏绿。同一个场景的**相机原生照片**颜色正常。
        // 能把偏色拆成两段的**唯一**干净办法是换一个**不由我们计算**的颜色来源 ——
        // `RGBA_8888`（用 `FLEX_RGBA_8888` 常量，见下）：YUV→RGB 由相机 HAL 完成
        // 完成，我们只做**下采样 + JPEG 编码**。
        //
        // ▸ `yuv_bt601` = 现状：本进程读 Y/U/V 三个平面、自己按 BT.601 有限范围换算。
        // ▸ `rgba_hal`  = HAL 换算。若**这一档也偏绿** ⇒ 问题在相机/HAL 或显示链路，
        //   **我们的转换代码是清白的**；若只有这一档正常 ⇒ 问题在我们的换算里。
        //
        // ⚠ 这不是"多一个好看的选项"，是**判决性实验**：两条路必有一条绿，
        //   责任归属立刻唯一确定。所以取值非法必须**显式回退并报出**（见 [startLiveLoop]
        //   的告警）—— 静默按默认处理会让这次二分**悄悄跑成两遍同一条路**，
        //   面板显示正常、结论却是假的。
        const val COLOR_SRC_YUV_BT601 = "yuv_bt601"
        const val COLOR_SRC_RGBA_HAL = "rgba_hal"
        /**
         * ⚠ 不能是 `const val` —— Kotlin 的 `const` 只允许原始类型与 String，
         *   `arrayOf` 是对象引用。写成 `const` 会编译失败（这正是本轮踩到的）。
         */
        val COLOR_SOURCES = arrayOf(COLOR_SRC_YUV_BT601, COLOR_SRC_RGBA_HAL)
        /**
         * RGBA reader 的格式常量。
         *
         * ⚠ **不能写 `ImageFormat.RGBA_8888`** —— 它在 Camera2 里是**隐藏常量**
         *   （值 1），`android.graphics.ImageFormat` **只导出了 `FLEX_RGBA_8888`**。
         *   写 `RGBA_8888` 编译直接失败（`Unresolved reference`）。
         *   而 `FLEX_RGBA_8888`（**真值 42 / 0x2A**，从 `android-34/android.jar`
         *   用 `javap -constants` 读出，**不是** 0x7F420888 —— 我第一版注释就是这么
         *   写错的，判据 P16c' 把它当场抓了出来）**正是 camera2 官方文档对
         *   "RGBA 打包"给出的写法**，且在 HAL 支持时可拿到 camera2 的 RGBA 输出。
         *   ⇒ 用它，且**判据锚定这个符号名**（写错成别的值 ⇒ 现场变成另一种格式，
         *   而表现只是"颜色不对"，看不出是格式选错）。
         */
        const val IMG_FMT_RGBA = ImageFormat.FLEX_RGBA_8888
        /**
         * HAL 是否真的给了 `RGBA_8888`。**不支持时不能假装支持** ——
         * `rgba_hal` 拿不到 RGBA 就必须报"不适用"并说清原因，否则这次二分
         * 会退化成"两条路其实是同一条"，而表面上看不出区别。
         */
        @Volatile private var rgbaSupported: Boolean? = null
        /** 本会话**实际生效**的颜色来源（可能与请求不同 —— HAL 不支持时会被替换）。 */
        @Volatile private var liveColorSource: String = COLOR_SRC_YUV_BT601
        /** PC **请求**的颜色来源（只记录，不据此判断实际行为）。 */
        @Volatile private var colorSourceRequested: String = COLOR_SRC_YUV_BT601
        /** 实时道单帧 HTTP 的读超时：一帧发不出去就该跳过，**不许把循环钉死**。 */
        const val LIVE_HTTP_READ_TIMEOUT_MS = 4000
        const val WARMUP_FRAMES = 5                   // 与 ② 的预热帧数一致（丢镜头机械收敛段）
        /** 监视道节流：1 Hz 足够看清取景，且不给控制道添负担。 */
        const val THUMB_INTERVAL_MS = 1000L
        /** 保留多少个"结果先到、图未到"的结果对象（够覆盖若干帧周期的回调错位即可）。 */
        const val RECENT_RESULT_KEEP = 30
        const val FORMAT = "YUV_Y"
        const val CHUNK = 1 shl 20                    // 1 MiB
        const val MAX_IMAGES = 4                      // FIFO 消费，留余量避免 HAL 侧溢出
        const val RECENT_Y_KEEP = 3                   // 近几帧 Y 平面缓存深度
        const val STATUS_INTERVAL_MS = 2000L
        // ---- 本地留存（**每一帧都留**：采集期写手机本地，边采边传、传不完的停采后排空）----
        //
        // ⚠ 第 24 轮**合并**：以前"交付道（抽帧·实时）"与"归档道（每一帧·采集后回传）"
        // 是两条道，各带一套 writer/队列/上传。但交付道本来就"先落本地、再 HTTP 分片上传"，
        // 归档那套是**同一套机器的复制品**。用户口径明确为"每一帧都留" ⇒ 合并成**一条道**：
        //   不抽帧 + 先落本地 + 边采边传 + 配额熔断 + 缺口账本
        // 判据也随之简化：**"抽帧数"与"回传数"这两个口径变成一个**（`frames_expected`）。
        /** 本地_write 队列深度。24 × 2.765 MB ≈ 66 MB 上限：够吸收突发 I/O 停顿，
         *  又不至于把内存吃掉。**满了必须记缺口**（`gaps_on_device`），不许静默丢
         *  —— 见 [enqueue] 的注释：这里曾经静默丢帧且三方都看不出来。 */
        const val STORE_QUEUE = 24
        /** 逐帧 `CaptureResult` 派生元数据的缓存深度（只有小 JSON，不含像素）。 */
        const val STORE_META_KEEP = 150
        /** 每写这么多帧检查一次剩余空间（熔断不能等写满才发现）。 */
        const val STORE_SPACE_CHECK_EVERY = 32
        /** 缺口时间戳最多记这么多（防止长采集把清单撑爆；计数仍然精确）。 */
        const val STORE_GAP_KEEP = 2000
        /** `delivery_progress` 的最小间隔（PC 侧还会再节流一次）。 */
        const val DELIVERY_PROGRESS_MS = 5000L
        const val STORE_INDEX = "index.jsonl"
        // 与探针共用同一个版本号（一个 APK 一个版本）：两条报告出口报出不同的版本串，
        // 会让"这份证据是哪次构建产出的"变得无法回答。
        const val APP_VERSION = LockedCaptureProbe.APP_VERSION
    }

    // ------------------------------------------------------------------ 状态
    private val running = AtomicBoolean(false)
    var sessionId: String = ""
        private set
    var connected = false
        private set

    /**
     * 是否正在采集。**给扫码入口用的**：采集握着后摄，此时进扫码界面必然"相机打不开"。
     * 与其让用户对着黑屏猜，不如在入口处直接说清楚。
     */
    val isCapturing: Boolean get() = running.get()

    /**
     * **交付道存活**（与 [connected] 无关）。这是第 21 轮的核心修正。
     *
     * 双流方案里交付道（HTTP 分片续传）与控制道（WS）是**两条独立的路**：
     * `connected` 只表示"WS 握手有效"，它**不能**拿来当"还能不能传帧"。
     * 旧实现把上传线程写成 `while (connected)` —— WS 一断（PC 重启、WiFi 抖动、
     * 对端关闭、用户退出面板），上传线程立刻退出，队列里已抽好的帧就**停在手机上不动了**，
     * 而 HTTP 明明是通的。用户诉求正是这条："实时联通结束后仍要按双流方案回传全部信息"。
     */
    @Volatile private var deliveryAlive = false
    /** 用户**显式**点了「断开」⇒ 交付道才允许收工（与"意外掉线"区别对待）。 */
    @Volatile private var userClosed = false
    /** 每个会话已 verified 并删除本地的帧数（跨会话按业务键累计，不按目录切）。 */
    private val uploadedBySession = java.util.concurrent.ConcurrentHashMap<String, Int>()
    private var uploadFailStreak = 0
    /** "交付收尾已宣告"标记：**每轮采集只宣告一次**（重启采集时复位）。 */
    @Volatile private var deliveryDoneAnnounced = false
    /** 采集停止的时刻（算收尾耗时用）。 */
    @Volatile private var stoppedAtMs = 0L
    private var httpBase = ""

    // ------------------------------------------------- 采集模式 + 实时道账本（第 27 轮）
    //
    // `capture_mode` 由 PC 在 `start` 的 plan 里下发；缺省 `deliver`（现状）。
    @Volatile private var captureMode: String = MODE_DELIVER
    @Volatile private var liveWidth: Int = LIVE_DEF_WIDTH
    @Volatile private var liveQuality: Int = LIVE_DEF_QUALITY
    @Volatile private var liveMaxMbps: Double = 0.0     // 0 = 不限（用户口径：阻塞发送）
    @Volatile private var liveColor: Boolean = true
    @Volatile private var liveStop: Boolean = false     // 停采集时先置它，让循环立刻退出
    private var liveThread: Thread? = null
    // 四个数**必须分开**（观测口径也是判据）：
    //   sent         = 真的发出去了几张（HTTP 2xx）
    //   dropped      = 异常丢的（编码失败 / HTTP 非 2xx / 无图）
    //   skipped_est  = **取图时被跳过的陈帧**（`acquireLatestImage` 的固有行为，**是设计**）
    //   last/avg_ms  = 端到端闭环耗时（取图→编码→POST 返回）
    // 把 skipped 与 dropped 合并，就再也分不清"链路不够快"与"实现有 bug"。
    //
    // ⚠ `skipped_est` 是**估算**，不是精确计数：Android 没有"本道丢弃了多少张"的 API
    //   （`ImageReader` 的 listener 只在缓冲区由空转非空时通知，而我们**故意不消费**
    //   那些陈帧）。口径 = 取图间隔 ÷ 帧栅格周期 - 1，逐次累加。
    //   名字里带 `_est` 就是为了不让它被当成实测值 —— **判据不得强于事实**。
    @Volatile private var liveSkippedEst = 0
    @Volatile private var liveSent = 0
    @Volatile private var liveDropped = 0
    @Volatile private var liveBytes = 0L
    @Volatile private var liveLastLatencyMs = 0L
    @Volatile private var liveLatencySumMs = 0L
    @Volatile private var liveLatencyN = 0
    @Volatile private var liveFirstAtMs = 0L
    @Volatile private var liveLastAtMs = 0L
    private var liveDropLogged = 0

    @Volatile private var profile = JSONObject()
    // ⚠ 第 24 轮起 `interval_ms` **默认 0 = 不抽帧 = "每一帧都留"**（合并后的语义）。
    // 它现在是**降载旋钮**：设成 >0 会让语义退回"只留一部分"，账本里 `every_frame` 变 false。
    @Volatile private var plan: JSONObject = JSONObject().put("mode", "interval")
        .put("interval_ms", 0).put("count", 0)

    // ---------------------------------------------------------------- 变焦档位
    //
    // 需求（用户）：**定位默认用 0.6×**（超广角，视场大 1.6 倍，便于把目标摆进画面），
    // 但必须能切回 1.0×（主摄，已标定）。
    //
    // **权威是谁**（这一条必须写死，否则两边会在界面上打架）：
    //  · PC 的 `profile.zoom_ratio` **> 0** ⇒ 用它（操作台显式指定，例如 `--profile` 里写死）；
    //  · PC 的值为 **0 / 缺省** ⇒ 用**本机界面上的档位**（App 是取景与标定的执行端）。
    //    ⇒ PC 默认 profile 里把该项设为 0 = "不指定"，不再默默压成 1.0。
    //  · 实际生效值与来源逐帧记进元数据（`requested.zoom_ratio` / `zoom_source`），
    //    任何一份数据都能自答"为什么是这个档"。
    @Volatile private var zoomChoice: Double = 0.6      // 本机档位（默认 0.6，定位需求）
    @Volatile private var reqZoom: Double = 0.6         // 本会话实际下发的档位（开流时解析一次）
    @Volatile private var zoomSource: String = "app_ui" // app_ui | pc_profile

    /**
     * 待传队列：**全局一个**（不按 session 分目录）。
     *
     * 为什么不按 session 切：§5.6（D13/D14）要求"每帧先落持久化队列，恢复后按序自动补传"。
     * 而 PC 每次 `start` 都会生成**新的 session_id**，App 会跟着切换 —— 若队列目录按 session
     * 切，上一轮没传完的帧就变成**永远不传的孤儿**（实测踩到：一轮 51 帧整批滞留，且没有任何
     * 判据会报它）。帧属于哪个会话写在它自己的元数据里（`session_id` 也是幂等键的一半），
     * 所以队列本身不需要按会话切。
     */
    private val queueDir: File
        get() = File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "delivery/queue")

    /** 把旧版按 session 切的队列目录（`delivery/<sid>/queue/`）并进全局队列。 */
    private fun migrateLegacyQueues() {
        val base = File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "delivery")
        val target = queueDir
        if (!target.exists()) target.mkdirs()
        base.listFiles()?.filter { it.isDirectory && it.name != "queue" }?.forEach { d ->
            val q = File(d, "queue")
            if (!q.isDirectory) return@forEach
            q.listFiles()?.forEach { f ->
                when {
                    f.name == "next_seq.txt" -> {
                        // 序号取全局最大，避免新帧撞旧键
                        val n = f.readText().trim().toIntOrNull() ?: 1
                        val g = File(target, "next_seq.txt")
                        val cur = g.takeIf { it.exists() }?.readText()?.trim()?.toIntOrNull() ?: 1
                        if (n > cur) g.writeText(n.toString())
                    }
                    f.name.endsWith(".yuv") || f.name.endsWith(".json") -> {
                        val dst = File(target, f.name)
                        // 同名就加 session 前缀（本地文件名只是本地标识；
                        // 交给 PC 的名字仍按帧自己的 seq 生成，各会话各写各的目录，不会撞）
                        val real = if (!dst.exists()) dst
                                   else File(target, "${d.name}__${f.name}")
                        try { f.renameTo(real) } catch (e: Exception) { }
                    }
                }
            }
            // 清掉空壳目录
            if ((q.listFiles()?.isEmpty() ?: true)) {
                q.delete(); d.delete()
            }
        }
    }

    private fun log(s: String) = onLog(s)

    /** 会话号格式与 §6.3 的落盘目录一致：`2026-09-25_0123_ab12`。 */
    private fun newSessionId(): String {
        val ts = java.text.SimpleDateFormat("yyyy-MM-dd_HHmm", java.util.Locale.US)
            .format(java.util.Date())
        val rnd = String.format("%04x", (Math.random() * 0xFFFF).toInt())
        return "${ts}_$rnd"
    }

    // ------------------------------------------------------------------ WS 客户端
    private var ws: WsClient? = null
    private var statusThread: Thread? = null

    /**
     * 连接 PC。输入**统一交给 [PairCode]** —— 手输、粘贴、扫码三条路只有这一个解析器
     * （两条路各自解析会漂移出"扫到的能连、粘同一串连不上"这种没法自证的分叉）。
     *
     * 接受三种写法：
     *   `m0probe://pair?h=10.205.123.245&p=8765&t=ab12cd34`（PC 二维码/面板的规范码）
     *   `http://10.205.123.245:8765/?token=ab12cd34`（终端打印的整行 URL）
     *   `10.205.123.245:8765` / `10.205.123.245`（再用输入框里的 token）
     *
     * 两步走：**先 HTTP 探测可达性，再开 WS**。理由：只开 WS 的话，失败原因
     * （网络不通 / 服务没跑 / 只有 token 错）全都表现为"没反应"；而 HTTP 探测能把
     * 前两者与"token 错"分开 —— 现场排查就靠这个区分。
     */
    fun connect(rawTarget: String, rawToken: String) {
        val pc = PairCode.parse(rawTarget)
        if (!pc.ok) {
            // 说出来的是**原因**，不是"解析失败"这种废话：
            // 扫到无关二维码时，"这不是配对码（看起来是一个 weixin:// 链接）"才是有效信息。
            log("✗ 地址无法识别：${pc.reason}")
            log("  可用写法：m0probe://pair?h=<ip>&p=<port>&t=<token> ／ http://<ip>:<port>/?token=… ／ ip:port")
            onConnectResult(false, "连接失败：${pc.reason}")
            return
        }
        val h = pc.host
        val p = pc.port
        val token = rawToken.trim().ifBlank { pc.token }
        httpBase = "http://$h:$p"
        if (sessionId.isEmpty()) sessionId = newSessionId()
        onSessionId(sessionId)
        log("目标 $h:$p   token=${if (token.isEmpty()) "（空！服务端会拒）" else token.take(4) + "…"}")
        Thread {
            val ok = probeHttp()
            if (!ok.first) {
                connected = false
                log("✗ PC 不可达：${ok.second}")
                log("  逐条排查：① PC 上 server.py 是否还在跑（那个窗口不能关）")
                log("  ② 地址是否填成了虚拟网卡（192.168.x.1 常是 VMware/VirtualBox，手机连不上）")
                log("  ③ 手机与 PC 是否同一 Wi-Fi/网段（关掉手机流量）")
                log("  ④ Windows 防火墙是否放行（见 pc/README 的排查清单）")
                log("  ⑤ 用手机浏览器打开 http://$h:$p/?token=$token —— 能看到面板说明网络通")
                onConnectResult(false, "连接失败：PC 不可达（${ok.second}）")
                return@Thread
            }
            log("✓ PC 可达（HTTP ${ok.second}），开始建 WS")
            ws = WsClient(
                h, p, token,
                onMessage = { msg -> onWsMessage(msg) },
                onOpen = {
                    // ⚠ **成功必须显式上报**。此前只有失败路径调 onConnectResult：
                    // 成功时界面永远停在「连接中…」，而"开流交付/停流"一直是 disabled
                    // ⇒ 现场看到「电脑有输出、App 点不动」（实测踩到）。
                    connected = true
                    log("✓ WS 已握手（控制道就绪）")
                    if (running.get() && !sessionConfirmed) {
                        // 重连：PC 可能重启过（会话表没了），先把会话声明出去再继续
                        log("重连后重新声明会话")
                        announceSession()
                    }
                    // 控制道就绪 ⇒ 交付道也允许工作（HTTP 与 WS 同址，但**生命周期独立**）
                    userClosed = false
                    deliveryAlive = true
                    migrateLegacyQueues()
                    startWriter()
                    startStatusLoop()
                    startThumbSender()
                    onConnectResult(true, "WS 已握手")
                },
                onClosed = { reason -> onConnectionLost(reason) }
            )
            ws?.start()
            // ⚠ `connected` 不在这里置 true：它现在表示「WS 握手已确认」。
            // 抢在握手前置位有两个害处：token 错时也会启动上传线程白跑一遍；
            // onConnectionLost 的幂等闸门失效（会把"从未连上"当"连接丢失"）。
        }.also { it.isDaemon = true; it.start() }
    }

    /** HTTP 可达性探测（不需要 token；403 也说明服务活着）。 */
    private fun probeHttp(): Pair<Boolean, String> = try {
        val c = (java.net.URL("$httpBase/api/v1/sessions").openConnection() as java.net.HttpURLConnection)
        c.connectTimeout = 3000
        c.readTimeout = 3000
        val code = c.responseCode
        if (code in 200..299) true to "$code" else true to "$code（服务在，响应异常）"
    } catch (e: Exception) {
        false to "${e.javaClass.simpleName}: ${e.message}"
    }

    /** 切换到 0.6× / 1.0×（界面按钮）。 */
    fun setZoom(z: Double) {
        if (zoomChoice == z) return
        zoomChoice = z
        log("变焦档位 → ${if (z < 0.9) "0.6× 超广角" else "1.0× 主摄"}（zoom=$z）")
        if (running.get()) {
            // **运行中切档必须换会话**：换档=换镜头=换内参，混在同一段会话里
            // 会让 manifest 的断言面对两套内参（画面/时间戳/参数回读全都正常 —— 典型的静默污染）。
            log("运行中切档 ⇒ 停流并换新会话号后重开（避免一段数据混两套内参）")
            rotateSession("切档")
            startCapture()
        } else {
            log("将在下次开流生效")
        }
    }

    /** 换新会话号（改配置/切档时用）：PC 端需要重新确认会话，监视道自动等闸门。 */
    private fun rotateSession(reason: String) {
        stopCapture()
        sessionId = newSessionId()
        sessionFromPc = false
        sessionConfirmed = false
        thumbGateLogged = false
        announceRetries = 0
        onSessionId(sessionId)
        log("新会话 $sessionId（原因：$reason）")
    }

    /** 解析本会话的生效档位（PC 显式指定优先；0/缺省 ⇒ 本机档位）。 */
    private fun resolveZoom() {
        val pv = profile.optDouble("zoom_ratio", 0.0)
        if (pv > 0.0) { reqZoom = pv; zoomSource = "pc_profile" }
        else { reqZoom = zoomChoice; zoomSource = "app_ui" }
        log("本会话变焦 = $reqZoom（来源 $zoomSource）⇒ " +
            if (reqZoom < 0.9) "超广角档：**内参与主摄不同，须单独标定/取证**"
            else "主摄档：可沿用 M0 的主摄结论")
    }

    fun disconnect() {
        // ⚠ connected 必须先置 false：它既是"WS 握手有效"的标记，
        // 也是 onConnectionLost 的幂等闸门 —— 晚置会让**用户主动断开**
        // 被当成"连接意外丢失"，界面白弹一次告警。
        connected = false
        running.set(false)
        stopCapture()
        ws?.close(); ws = null
        statusThread = null
        thumbThread = null
        lastPreviewAt = 0L
        previewDropped = 0
        // **交付道不由"用户点断开"决定生死**：交付走 HTTP，与 WS 无关
        // （双流方案的本意，也是用户第 21 轮的诉求）。
        // 队列还有帧 ⇒ 转入**收尾模式**：不再采集，但把已抽的帧传完才收工。
        // 队列已空 ⇒ 立刻收工，不养一个空转线程。
        //
        // ⚠ 收尾模式下**不能**把 `uploader` 置 null：线程还活着，
        // 引用一丢就再也没人管得到它（重连时会又起一个，两个线程抢同一个队列）。
        // 重连时是否要重建，判据是 `isAlive`（见 startWriter），不是"引用非空"。
        userClosed = true
        val pending = queueCount()
        if (pending > 0) {
            log("已断开控制道；队列还剩 $pending 帧 ⇒ **交付道继续收尾**（HTTP 独立于 WS），" +
                "传完会自动宣告 delivery_done")
            if (uploader == null) startWriter()      // 线程若已退出（例如从未连过），补起一个
        } else {
            deliveryAlive = false
            uploader = null
            log("已断开（队列已空，交付道收工）")
        }
    }

    /**
     * WS 断开（对端关闭 / 网络断 / 读写异常）后的统一收尾。
     *
     * **幂等**：`ws_io` 错误消息与 WsClient 的 `onClosed` 都可能触发，
     * 第二次进来直接返回（否则会重复 stopCapture、重复刷状态栏）。
     *
     * 与 `disconnect()` 一样必须把 `uploader`/`statusThread` 置 null ——
     * 两个线程都以 `while (connected)` 为循环条件、断开即退出，不置空则**重连不会重建线程**
     * （上一轮实测：三次重连后一帧都没传出去，界面还看不出异常）。
     */
    private fun onConnectionLost(reason: String) {
        if (!connected) return
        connected = false
        running.set(false)
        stopCapture()
        ws = null
        statusThread = null
        thumbThread = null
        lastPreviewAt = 0L
        previewDropped = 0
        // **交付道不受影响**（双流方案的要害）：控制道断了只说明"没人指挥了"，
        // 不代表"不能回传" —— 交付走 HTTP，与 WS 是两条路。
        // 所以这里**不置 uploader = null、也不动 deliveryAlive**：
        // 上传线程照旧把队列传完（失败就退避重试，文件一律保留），
        // drain_state 会如实显示 draining / blocked_offline。
        log("✗ 连接已断开：$reason")
        log("  控制道断了，**交付道继续**：队列剩余 ${queueCount()} 帧仍会经 HTTP 回传" +
            "（HTTP 不依赖 WS）；重连后未完成的会话会自动续传")
        onConnectResult(false, "连接已断开：$reason")
    }

    private fun onWsMessage(msg: String) {
        val m = try { JSONObject(msg) } catch (e: Exception) { return }
        when (m.optString("type")) {
            "config" -> {
                m.optJSONObject("profile")?.let {
                    profile = it
                    log("profile: 输出 ${it.optJSONArray("output_size")} " +
                        (if (currentIntervalNs() > 0)
                             "抽帧 ${currentIntervalNs() / 1_000_000} ms（**降载模式**）"
                         else "**每一帧都留**（不抽帧）"))
                }
                // 收到 config 说明 WS 已建好 ⇒ 立刻 hello（§7.1）。顺序反了会让
                // 服务端在面板上一直显示"没有 App 在线"，而其实已经连上了。
                ws?.send(JSONObject().apply {
                    put("type", "hello")
                    put("device_serial", Build.MODEL + "-" + Build.ID)
                    put("model", Build.MODEL)
                    put("device", Build.DEVICE)
                    put("app_version", APP_VERSION)
                    // hello **不带 session_id**：会话由 PC 的 `start` 创建。
                    // 带了它，服务端会为每次连接建一个空会话目录（实测：连三次就有三个空壳），
                    // 现场会积累一堆 0 帧目录、把"哪次是真采集"搞乱。
                    put("calib_id", calibId())
                    put("delivery", DELIVERY)
                    put("capture_path_id", PATH_ID)
                    put("capture_path_rev", PATH_REV)
                    put("zoom_choice", zoomChoice)
                    put("zoom_effective", reqZoom)
                    put("camera_caps", JSONObject().put("manual_focus", true)
                        .put("raw", true).put("timestamp_source", "REALTIME"))
                }.toString())
            }
            "session_ack" -> {
                // 控制道的应答：**PC 已经知道这个会话了** ⇒ 监视道可以发。
                // 这与"首帧 verified"是两条独立的确认路径，谁先到用谁：
                //  · session_ack：控制道往返（通常 <100 ms）；PC 重启后仍适用；
                //  · 首帧 verified：交付道证明（可能被**旧会话积压**排在后面，实测要等 ~1 s 甚至更久）。
                val sid = m.optString("session_id")
                if (sid.isNotEmpty() && sid == sessionId) {
                    if (!sessionConfirmed) log("PC 已确认会话（session_ack）⇒ 监视道开始发送缩略图")
                    sessionConfirmed = true
                    announceRetries = 0          // 声明成功，重试计数归零
                }
            }
            "thumb_rejected" -> {
                // PC 说"这个会话我不认识" ⇒ **立刻关闸**，别按 1 Hz 继续灌。
                // 有了这条回话，闸门就不再依赖"App 猜 PC 的进度"，而是双向可见的。
                thumbsRejected++
                sessionConfirmed = false
                thumbGateLogged = false
                log("PC 未识别当前会话（${m.optString("reason")}）⇒ 监视道暂停")
                // **自愈**：PC 侧会话可能丢（典型：服务重启过，内存里的会话表没了），
                // 而 App 这时还在采 —— 只关闸就会永久黑屏。重新声明一次即可恢复；
                // 但要有上限，避免"被拒→声明→又被拒"打成死循环。
                if (announceRetries < 3) {
                    announceRetries++
                    log("重新声明会话（第 $announceRetries 次）")
                    announceSession()
                } else {
                    log("已重试 $announceRetries 次仍被拒 ⇒ 停止声明（请检查两端 session_id 是否一致）")
                }
            }
            "start" -> {
                m.optString("session_id").takeIf { it.isNotEmpty() }?.let {
                    sessionId = it
                    sessionFromPc = true          // 记住"这个会话号是 PC 分配的"
                    sessionConfirmed = true       // PC 刚建的会话 ⇒ 它显然知道
                    onSessionId(it)
                }
                m.optJSONObject("plan")?.let { plan = it }
                m.optJSONObject("profile")?.let { profile = it }
                // ---- 采集模式（第 27 轮）----
                // 顶层字段优先（与 PC 的 h_control 一致：它既认顶层、也认 plan 里的）。
                val wantMode = m.optString("capture_mode").takeIf { it.isNotEmpty() }
                    ?: plan.optString("capture_mode").takeIf { it.isNotEmpty() }
                    ?: MODE_DELIVER
                if (wantMode != MODE_DELIVER && wantMode != MODE_LIVE) {
                    // PC 侧已经校验过取值，这里再挡一道：静默按 deliver 处理会让
                    // 「我要只看不留」变成「结果全量落盘」—— 两种现场行为差一个量级，
                    // 而这种"意图被悄悄换掉"是使用者完全看不出来的。
                    log("⚠ 未知 capture_mode=$wantMode ⇒ 按 $MODE_DELIVER 处理（PC 端版本可能不匹配）")
                }
                captureMode = if (wantMode == MODE_LIVE) MODE_LIVE else MODE_DELIVER
                liveWidth = plan.optInt("live_width", LIVE_DEF_WIDTH).coerceIn(160, 1920)
                liveQuality = plan.optInt("live_quality", LIVE_DEF_QUALITY).coerceIn(30, 95)
                liveMaxMbps = plan.optDouble("live_max_mbps", 0.0)
                // ---- 颜色来源（第 29 轮）----
                // 非法取值**必须报出**：这是一次**判决性实验**，静默退回默认会让
                // "我请求了 A 档"与"我跑的是 B 档"两件事看起来一样，
                // 而结论（偏色是谁的锅）会整个反过来。
                val wantSrc = m.optString("color_source").takeIf { it.isNotEmpty() }
                    ?: plan.optString("color_source").takeIf { it.isNotEmpty() }
                    ?: COLOR_SRC_YUV_BT601
                colorSourceRequested = wantSrc
                liveColorSource = if (wantSrc in COLOR_SOURCES) wantSrc else {
                    log("⚠ 未知 color_source=$wantSrc（可选 ${COLOR_SOURCES.joinToString("/")}）" +
                        "⇒ 按 $COLOR_SRC_YUV_BT601 处理。**若你本想跑 ${COLOR_SRC_RGBA_HAL} 做二分，"+
                        "这次实验已作废**（两条路是同一条）")
                    COLOR_SRC_YUV_BT601
                }
                if (liveColorSource == COLOR_SRC_RGBA_HAL && captureMode != MODE_LIVE) {
                    log("⚠ 交付道不支持 color_source=$COLOR_SRC_RGBA_HAL" +
                        "（它要的是原始 Y 平面）⇒ 按 $COLOR_SRC_YUV_BT601 处理")
                    liveColorSource = COLOR_SRC_YUV_BT601
                }
                rgbaSupported = null                 // 每个会话重新探测，不跨会话继承
                // PC 侧 profile 里 zoom_ratio>0 表示显式指定；否则沿用本机档位（见 resolveZoom）
                remoteStart = true
                startAckSent = false
                log("收到 start（PC 主控）：session=$sessionId capture_mode=$captureMode plan=$plan")
                startCapture()
            }
            "stop" -> {
                val why = m.optString("reason")
                val hard = m.optJSONArray("hard_fail")
                log("收到 stop（PC 主控" + (if (why.isNotEmpty()) "，原因：$why" else "") + "）" +
                    "⇒ 停止采集；队列继续补传（不丢弃已采数据）")
                if (why == "start_gate_failed") {
                    // **门控失败 = 下发的 profile 没被真正采纳**（例如 zoom 被 HAL 拒）。
                    // 这种情况必须让人看见，否则现场只会发现"莫名其妙被停了"。
                    log("  ⚠ 门控失败项：$hard ⇒ 本会话的锁定项与 PC profile 不一致，" +
                        "数据不能按该配置使用；请核对档位/输出尺寸后重开")
                }
                stopCapture()
                sendStopAck()
            }
            "error" -> {
                val code = m.optString("code")
                val msg = m.optString("message")
                log("✗ $code: $msg")
                when (code) {
                    // 握手就没成功（connected 从未置位）⇒ 直接报错并给出处置
                    "ws_handshake" -> {
                        log("  排查：token 是否与服务端窗口里打印的一致" +
                            "（重启服务会换新 token，除非启动时用 --token 固定）")
                        val head = msg.take(60)
                        onConnectResult(false, "连接失败：token 不符或握手被拒（$head）")
                    }
                    // 已经连上又掉了 ⇒ 走统一收尾（停采集、清线程、界面回到可重连）
                    "ws_io" -> onConnectionLost("$code: $msg")
                }
            }
            "ack" -> {
                // WS 的 ack 是**面板用的副本**；本地删除的权威依据是与该文件绑定的
                // HTTP complete 响应（同一个 file_id 的判定），见 Uploader 的注释。
                log("ACK seq=${m.optInt("seq")} state=${m.optString("state")}")
            }
        }
    }

    // ------------------------------------------------------------------ 采集
    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var reader: ImageReader? = null
    /** 本次会话**实际**使用的 reader 格式（判决性：谁算的颜色取决于它）。 */
    @Volatile private var readerFmt: Int = ImageFormat.YUV_420_888
    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private var request: CaptureRequest? = null

    private val recentY = LinkedHashMap<Long, ByteArray>()   // ts -> 已抽好的 Y 平面
    private val pendingSample = HashSet<Long>()              // 已决定抽、但图还没到的 ts
    /**
     * ts -> 结果，**供"图比结果晚到"的那一帧补齐元数据**。
     *
     * ⚠ 为什么必须有它：交付的每帧要配三个东西 —— 图（ImageReader 回调）、
     * 结果（CaptureCallback）、时间戳。两条回调**顺序不保证**。原实现是
     * "结果先到就把 ts 记进 pendingSample、**结果对象丢掉**"，等图到达时只能
     * 拿 `null` 去组元数据 ⇒ **这一帧整组 CaptureResult 派生字段静默缺失**
     * （skew / active_physical_id / zoom 回读 / 曝光 / ISO / crop_region …）。
     * 实测（第 18 轮）：**44 帧里 28 帧中招（64%）**，而画面、时间戳、计数全都正常。
     */
    private val recentResult = LinkedHashMap<Long, TotalCaptureResult>()
    private var metaMissingResult = 0
    private var lastSampledTs = 0L
    private var lastRowStride = 0
    /** 当前 `sessionId` 是否由 PC 的 `start` 分配（见 startCaptureLocal）。 */
    private var sessionFromPc = false

    /**
     * 「PC 端是否已知道当前会话」—— 监视道的发送闸门。
     *
     * 实测踩到：本地开流时 App 自己生成会话号，而 PC 是在**首帧 init** 时才建会话
     * ⇒ 第一张缩略图（1 Hz，比首帧 init 更早）会撞上"未知会话"被忽略
     * （PC 端日志：`[ws] thumb 忽略：未知会话 …（监视道不建会话）`）。
     * 修法不是让 PC 放宽（那样陈旧/伪造的缩略图会凭空造出会话目录），
     * 而是**让监视道等交付道确认会话**：可靠的那条车道才是"会话是否存在"的权威。
     * 首帧 verified 通常只要 1–2 s，代价可以忽略。
     */
    @Volatile private var sessionConfirmed = false
    private var thumbGateLogged = false
    private var captureStartElapsedNs = 0L
    private var streamStartElapsedNs = 0L
    private var deliveredCount = 0

    @SuppressLint("MissingPermission")
    /**
     * 本地点「开流交付」时走这里（PC 面板下发 start 时走 startCapture()）。
     *
     * **为什么要区分**：PC 每次 start 都新建一个会话号，而交付帧的元数据里带着
     * "它属于哪个会话"。若本地开的这一轮复用 PC 上一次分配的会话号，这批帧会被上传进
     * **PC 那边已经结束的会话目录** —— 两轮采集混进同一份 manifest，
     * 而 manifest 的断言（calib_id 一致 / 参数漂移 / 温度区间）会被静默污染。
     */
    fun startCaptureLocal() {
        remoteStart = false               // 本机开流：PC 没发指令，不回 start_ack
        if (sessionFromPc) {
            sessionId = newSessionId()
            sessionFromPc = false
            sessionConfirmed = false          // 新会话 PC 还不知道，等首帧 verified
            thumbGateLogged = false
            announceRetries = 0
            onSessionId(sessionId)
            log("本地开流：新会话 $sessionId（不复用 PC 上一次分配的会话号）")
        }
        startCapture()
    }

    /**
     * 控制道声明"我要开始采这个会话了"（§7.1 `session_begin`）。
     *
     * 为什么不再依赖"首帧 verified 开闸"：交付道是**队列**驱动的，连接后往往先要
     * 补传上一个会话的积压帧，本会话的首帧可能排在几十帧之后 ⇒ 监视道会长时间黑屏。
     * 控制道是即时的、有序的（同一条连接），所以由它来声明会话最恰当；
     * 交付道的 verified 与 PC 的 thumb_rejected 仍作为**独立校验**保留。
     */
    private fun announceSession() {
        if (!connected) return
        val w = ws ?: return
        try {
            w.send(JSONObject().apply {
                put("type", "session_begin")
                put("session_id", sessionId)
                put("app_version", APP_VERSION)
                put("model", android.os.Build.MODEL)
                put("calib_id", calibId())
                put("delivery", DELIVERY)
                // 路径标识**按模式分**：`live` 产出的是彩色 JPEG，与 `stream_deliver_yuv`
                // 的原始 Y 平面**根本不是同一种东西** ⇒ 不能借同一个 id 的 rev 表达
                //（登记表里两条路径各自独立；既有路径的 rev 一个都没动）。
                put("capture_path_id", if (captureMode == MODE_LIVE) LIVE_PATH_ID else PATH_ID)
                put("capture_path_rev", if (captureMode == MODE_LIVE) LIVE_PATH_REV else PATH_REV)
                put("capture_mode", captureMode)
                put("zoom_choice", zoomChoice)
                put("zoom_effective", reqZoom)
                put("zoom_source", zoomSource)
            }.toString())
            log("已向 PC 声明会话 $sessionId（session_begin，等 ack）")
        } catch (e: Exception) {
            log("声明会话失败: ${e.message}")
        }
    }

    fun startCapture() {
        if (running.get()) { log("已在采集中"); return }
        running.set(true)
        // 新一轮采集 ⇒ **收尾标记复位**：否则第二轮采完就不会再宣告"全部回传完成"
        // （`delivery_done` 是"每轮一次"的事件，不是"一辈子一次"）。
        deliveryDoneAnnounced = false
        stoppedAtMs = 0L
        liveStop = false
        // ⚠ `live` 模式下**总是**声明一次：它的路径标识（`live_stream_color/1`）
        // 只能从 `session_begin` 送出去 —— live 不发 `/api/v1/files/*`，
        // 没有 `init` 可以把路径补进 PC 的会话记录（manifest 里 `capture_path` 空着，
        // "这份数据是哪条路径产的"就无法回答）。
        if (!sessionConfirmed || captureMode == MODE_LIVE) announceSession()
        val live = captureMode == MODE_LIVE
        if (live) {
            // ⚠ **不起留存器** —— 这就是"不储存"的落地位置（用户口径：不储存、
            // 也不在事后回传额外的数据）。`storeState` 停在 `not_applicable`，
            // `status.store` 里 `applicable=false`，PC 侧据此把 `delivery_complete`
            // 判 **null（不适用）** 而不是 false。
            resetLiveLedger()
            storeState = "not_applicable"
            log("实时道模式（只看不留）：**不落盘、不停采排空、不回传**；允许彩色传输")
        } else {
            // 留存账本：采集期间把每一帧写本地。**必须在开相机之前**重置好 ——
            // 否则前若干帧会因为"账本还没就绪"而进不了留存（首帧起就该一帧不落）。
            startStore()
        }
        val cm = ctx.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val id = pickBackCamera(cm) ?: run { log("找不到后置摄像头"); running.set(false); return }
        val chars = cm.getCameraCharacteristics(id)
        // ⚠ **顺序有依赖**：reader 格式先定（它决定"颜色由谁算"），尺寸再**按该格式**查 ——
        //   两格式的可选尺寸常不重叠，先查 YUV 尺寸再建 RGBA reader 会让 HAL 抛
        //   "尺寸不支持"，而真正原因是格式，报错信息会把排查方向带偏。
        // ⚠ **ImageReader 的格式就是"颜色由谁算"的开关** —— 它在建 reader 时就定死了，
        //   之后拿到什么像素全看 HAL 填的是什么。所以 `rgba_hal` 必须**换一种 reader**，
        //   不能"先拿 YUV 再在本地转成 RGBA"（那就是原路，实验归零）。
        //   ⇒ 且 `rgba_hal` 只在 live 下有意义：交付道要的是原始 Y 平面，
        //   而 RGBA reader 根本没有 Y 平面 ⇒ 交付道请求它必须**显式拒绝**，
        //   不能静默按 yuv 处理（那会让"交付数据"变成"经过色彩处理的数据"，
        //   而 `stream_deliver_yuv` 宣称自己是 raw Y —— 声明与实际不符）。
        readerFmt = resolveReaderFormat(chars, live)
        val size = profileSize(chars, readerFmt)
        resolveZoom()
        log("开流：$id ${size.width}x${size.height}（" +
            (if (live) "capture_mode=live ⇒ 只走实时道"
             else if (currentIntervalNs() > 0)
                 "抽帧 ${currentIntervalNs() / 1_000_000} ms = **降载模式**"
             else "**每一帧都留**") + "）")
        thread = HandlerThread("delivery-cam").also { it.start() }
        handler = Handler(thread!!.looper)
        reader = ImageReader.newInstance(size.width, size.height, readerFmt, MAX_IMAGES)
        log("颜色来源：**$liveColorSource**（ImageReader 格式=$readerFmt）" +
            if (live) "" else "；交付道只支持 yuv_bt601（要的是原始 Y 平面）")
        // ⚠ **两个模式的取图语义相反，所以注册的东西也不同**：
        //   · `deliver` 走 onImage 回调（内部 `acquireNextImage()`，FIFO 逐帧配对
        //     SENSOR_TIMESTAMP —— 图被丢掉就意味着那帧的配对永远等不到图）；
        //   · `live` **不注册它**，改由 [startLiveLoop] 的独立线程
        //     `acquireLatestImage()` 取"此刻最新"。把两个语义塞进同一个回调，
        //     必然有一边是错的（而且错得静默）。
        if (!live) {
            reader!!.setOnImageAvailableListener({ rd -> onImage(rd) }, handler)
        }
        cm.openCamera(id, object : CameraDevice.StateCallback() {
            override fun onOpened(d: CameraDevice) {
                device = d
                try {
                    buildSession(d)
                } catch (e: Exception) {
                    fail("建会话失败: ${e.message}")
                }
            }
            override fun onDisconnected(d: CameraDevice) { fail("摄像头被断开") }
            override fun onError(d: CameraDevice, error: Int) { fail("摄像头错误 code=$error") }
        }, handler)
    }

    private fun fail(why: String) {
        log("采集失败：$why")
        running.set(false)
        sendStartAck(false, why)          // 启动失败必须回执，否则 PC 会一直以为在采
        stopCapture()
        onStatus(JSONObject().put("state", "error").put("message", why))
    }

    /** 启动回执：ok=false 时带原因（相机被占用 / 设备瞬时故障 / 会话配置失败…）。 */
    private fun sendStartAck(ok: Boolean, err: String?) {
        if (!remoteStart || startAckSent) return          // 本机开流不回执；只回一次
        startAckSent = true
        val w = ws ?: return
        try {
            w.send(JSONObject().apply {
                put("type", "start_ack")
                put("session_id", sessionId)
                put("ok", ok)
                if (!ok) put("error", err ?: "unknown")
                if (ok) {
                    put("output_size", JSONArray(listOf(reader?.width ?: 0, reader?.height ?: 0)))
                    put("format", FORMAT)
                    put("plan_interval_ms", plan.optInt("interval_ms", 0))
                    put("zoom_requested", reqZoom)
                    put("warmup_frames", if (captureMode == MODE_LIVE) 0 else WARMUP_FRAMES)
                    put("capture_mode", captureMode)
                }
                put("at_ms", System.currentTimeMillis())
            }.toString())
            log(if (ok) "已回 start_ack（相机已开流）" else "已回 start_ack（失败：$err）")
        } catch (e: Exception) {
            log("start_ack 发送失败: ${e.message}")
        }
    }

    /** 停止回执：把"停在哪"（已交付多少帧、队列还剩多少）一并报给 PC。 */
    private fun sendStopAck() {
        val w = ws ?: return
        try {
            w.send(JSONObject().apply {
                put("type", "stop_ack")
                put("session_id", sessionId)
                put("running", running.get())
                put("capture_mode", captureMode)
                if (captureMode == MODE_LIVE) {
                    // ⚠ live 下**没有**"要交付多少帧"这回事：三个数一律 0，
                    // `will_drain=false`。绝不能把 `deliveredCount`（那是别的模式的
                    // 抽帧计数）报出去 —— PC 会拿它去推 `delivery_complete`，
                    // 推出一个"收了 0/应有 N"的假缺口。
                    put("frames_delivered", 0)
                    put("queue_len", 0)
                    put("pending_frames", 0)
                    put("will_drain", false)
                    put("live", liveStatusJson())
                } else {
                    put("frames_delivered", deliveredCount)
                    put("queue_len", queueCount())
                    // **停采集 ≠ 停交付**（双流方案）：停流只是不再产生新帧，
                    // 队列里已抽好的帧仍要传完。所以这里必须把"还欠多少"讲清楚，
                    // 否则 PC 面板分不清"停了 = 结束了"与"停了但还在传"。
                    put("pending_frames", queueCount())
                    put("will_drain", true)
                }
                put("uploaded_frames", uploadedBySession[sessionId] ?: 0)
                put("meta_missing_result", metaMissingResult)
                put("at_ms", System.currentTimeMillis())
            }.toString())
            log(if (captureMode == MODE_LIVE)
                    "已回 stop_ack（实时道：发出 $liveSent 帧；本模式无留存 ⇒ will_drain=false）"
                else "已回 stop_ack（已抽帧 $deliveredCount，队列剩 ${queueCount()}）")
        } catch (e: Exception) {
            log("stop_ack 发送失败: ${e.message}")
        }
    }

    fun stopCapture() {
        // 实时道先"关闸"：它的循环可能正阻塞在一次 HTTP 上等对端（读超时上限 4 s）。
        // `liveStop` 让它在**本轮结束后立刻退出**，不必等下一轮去查 running。
        liveStop = true
        try { reader?.setOnImageAvailableListener(null, null) } catch (e: Exception) { }
        // 留存账本**先封口**：把"应有 / 实存 / 缺口"定下来（此时采集已停、写队列正在排空）。
        // 顺序上必须在 `reader` 关闭之前，否则最后几帧的 Y 已经没了。
        // ⚠ `live` 模式下 `storeState` 不是 "recording" ⇒ 本调用是**空操作**
        //（**没有留存可封口**：不是"没传完"，是"本来就没有"）。
        finishStoreRecording()
        try { session?.stopRepeating() } catch (e: Exception) { }
        try { session?.close() } catch (e: Exception) { }
        try { device?.close() } catch (e: Exception) { }
        try { reader?.close() } catch (e: Exception) { }
        session = null; device = null; reader = null
        thread?.quitSafely(); thread = null; handler = null
        recentY.clear(); pendingSample.clear(); recentResult.clear()
        if (running.get()) {
            stoppedAtMs = System.currentTimeMillis()
            val q = queueCount()
            if (captureMode == MODE_LIVE) {
                log("已停采集（实时道：发出 $liveSent 帧 / 异常丢 $liveDropped / " +
                    "跳过陈帧(估) $liveSkippedEst）")
                // ⚠ 队列里若还有帧，那是**别的会话**留下的积压 —— 队列是全局一个
                // （见 queueDir 的注释：按 session 分会把未传完的帧变成孤儿）。
                // 本模式的"不储存"不影响那些帧的交付，必须把这句话讲出来，
                // 否则用户看到"live 模式怎么还在传"会以为模式没生效。
                if (q > 0) log("  ⚠ 队列里还有 $q 帧：那是**之前会话**的积压，" +
                               "交付道照常把它传完（与本模式无关）")
            } else {
                log("已停采集（本次抽帧 $deliveredCount）")
                // **停采集不停交付**（双流方案）：队列与采集无关，交付道把它传完才收工。
                // 不把这句话讲出来，用户看到"停了还在传"会以为出了错。
                if (q > 0) log("  队列还剩 $q 帧 ⇒ 交付道继续回传（HTTP 独立于 WS），" +
                               "传完自动发 delivery_done")
                else log("  队列已空 ⇒ 全部已回传")
            }
        }
        running.set(false)
        // 采集一停就检查一次收尾：若队列本来已空，不必等下一轮扫描才宣告
        maybeAnnounceDeliveryDone()
    }

    private fun pickBackCamera(cm: CameraManager): String? =
        cm.cameraIdList.firstOrNull { cid ->
            cm.getCameraCharacteristics(cid)
                .get(CameraCharacteristics.LENS_FACING) == CameraMetadata.LENS_FACING_BACK
        }

    /**
     * 决定本次会话用哪种 reader 格式 —— **也就是决定"颜色由谁算"**。
     *
     * 判决性实验（第 29 轮）：现场 live 画面偏绿，而同场景的相机原生照片正常。
     * 要把责任唯一确定下来，就必须有一条**不由我们算颜色**的路：
     * `RGBA_8888` 档的 YUV→RGB 由**相机 HAL**（厂商 ISP 的色彩管线）完成，
     * 我们只做下采样 + JPEG 编码。
     *   · `rgba_hal` **也偏绿** ⇒ 我们的 BT.601 换算是清白的，问题在 HAL 或显示链路；
     *   · 只有 `yuv_bt601` 偏绿 ⇒ 问题在我们的换算里。
     *
     * ⚠ 三处**不许静默**，否则这次二分等于没做（两条路悄悄变成同一条，
     *   而表面上看不出区别，结论却是假的）：
     *   ① 非法取值 ⇒ **大声报出**并按 `yuv_bt601` 处理（不用抛异常中断开流 ——
     *      那是"什么都看不到"，比偏色更难查）；
     *   ② HAL 不支持 `RGBA_8888` ⇒ `rgba_supported=null` + 明确日志，
     *      **不许假装成功**；
     *   ③ 交付道请求 `rgba_hal` ⇒ **拒绝**（它要的是原始 Y 平面，RGBA 没有 Y）。
     */
    private fun resolveReaderFormat(chars: CameraCharacteristics, live: Boolean): Int {
        val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        if (!live) return ImageFormat.YUV_420_888        // 交付道只有一条路（见 ③）
        if (liveColorSource != COLOR_SRC_RGBA_HAL) return ImageFormat.YUV_420_888
        val avail = try {
            map?.getOutputSizes(IMG_FMT_RGBA)
        } catch (e: Exception) {
            null
        }
        if (avail.isNullOrEmpty()) {
            rgbaSupported = false
            liveColorSource = COLOR_SRC_YUV_BT601
            log("⚠ 请求 color_source=$COLOR_SRC_RGBA_HAL，但本相机**不支持** " +
                "RGBA_8888 档 ⇒ 退回 $COLOR_SRC_YUV_BT601。" +
                "**本次二分跑不成**（两条路会是同一条），结论请按\"未验证\"对待")
            return ImageFormat.YUV_420_888
        }
        rgbaSupported = true
        return IMG_FMT_RGBA
    }

    /**
     * 输出尺寸：优先 profile 声明的；不可用则退回 1920×1440（M0 的现行档）。
     *
     * ⚠ 可用尺寸**按格式分别查**：`RGBA_8888` 与 `YUV_420_888` 的可选尺寸**不重叠**是常事
     * （大尺寸往往只给 YUV）。拿 YUV 的尺寸去建 RGBA reader ⇒ HAL 直接抛
     * `IllegalArgumentException`，而报错信息是"尺寸不支持"，**看不出真正原因是格式**。
     */
    private fun profileSize(chars: CameraCharacteristics, fmt: Int = ImageFormat.YUV_420_888): Size {
        val want = profile.optJSONArray("output_size")
        val w = want?.optInt(0) ?: 1920
        val h = want?.optInt(1) ?: 1440
        val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
        val avail = map?.getOutputSizes(fmt) ?: emptyArray()
        avail.firstOrNull { it.width == w && it.height == h }?.let { return it }
        log("⚠ profile 尺寸 ${w}x$h 在格式 $fmt 下不可用，退回默认档")
        return avail.firstOrNull { it.width == 1920 && it.height == 1440 }
            ?: avail.minByOrNull { it.width * it.height } ?: Size(1920, 1440)
    }

    @SuppressLint("MissingPermission")
    private fun buildSession(d: CameraDevice) {
        val size = reader!!.surface.let {
            val avail = reader!!.width to reader!!.height
            Size(avail.first, avail.second)
        }
        d.createCaptureSession(listOf(reader!!.surface), object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(s: CameraCaptureSession) {
                session = s
                request = buildRequest(d, size)
                streamStartElapsedNs = SystemClock.elapsedRealtimeNanos()
                captureStartElapsedNs = streamStartElapsedNs
                try {
                    s.setRepeatingRequest(request!!, object : CameraCaptureSession.CaptureCallback() {
                        override fun onCaptureCompleted(
                            se: CameraCaptureSession, rq: CaptureRequest, result: TotalCaptureResult
                        ) { onResult(result) }
                    }, handler)
                    log(if (captureMode == MODE_LIVE)
                            "已开始连续流（实时道：串行闭环取图 —— 发完一帧才取下一帧）"
                        else "已开始连续流（按 sensor_timestamp 抽帧）")
                    // live 的循环**等流真起来了才启动**：否则它会一次次取到 null
                    // （相机还没出图），既刷日志又没有意义。
                    if (captureMode == MODE_LIVE) startLiveLoop()
                    sendStartAck(true, null)         // 相机真的开了 ⇒ 回执给 PC
                } catch (e: Exception) { fail("下发失败: ${e.message}") }
            }
            override fun onConfigureFailed(s: CameraCaptureSession) { fail("会话配置失败") }
        }, handler)
    }

    /** 锁定配方与 M0 的 ② 一致（manual 模板 + 手动曝光/对焦 + 关掉一切自动处理）。 */
    private fun buildRequest(d: CameraDevice, size: Size): CaptureRequest {
        val b = d.createCaptureRequest(CameraDevice.TEMPLATE_MANUAL)
        b.addTarget(reader!!.surface)
        b.set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_OFF)
        b.set(CaptureRequest.CONTROL_AE_MODE, CameraMetadata.CONTROL_AE_MODE_OFF)
        b.set(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_OFF)
        b.set(CaptureRequest.CONTROL_AWB_MODE, CameraMetadata.CONTROL_AWB_MODE_OFF)
        b.set(CaptureRequest.CONTROL_ENABLE_ZSL, false)          // 连续流也应显式关（可回读校验）
        val exp = profile.optLong("exposure_time_ns", 10_000_000L)
        val iso = profile.optInt("iso", 200)
        b.set(CaptureRequest.SENSOR_EXPOSURE_TIME, exp)
        b.set(CaptureRequest.SENSOR_SENSITIVITY, iso)
        val diopter = profile.optDouble("focus_diopter", 1.001001).toFloat()
        b.set(CaptureRequest.LENS_FOCUS_DISTANCE, diopter)
        b.set(CaptureRequest.CONTROL_ZOOM_RATIO, reqZoom.toFloat())
        b.set(CaptureRequest.LENS_OPTICAL_STABILIZATION_MODE,
            CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF)
        b.set(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE,
            CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF)
        b.set(CaptureRequest.EDGE_MODE, CameraMetadata.EDGE_MODE_OFF)
        b.set(CaptureRequest.NOISE_REDUCTION_MODE, CameraMetadata.NOISE_REDUCTION_MODE_OFF)
        b.set(CaptureRequest.COLOR_CORRECTION_ABERRATION_MODE,
            CameraMetadata.COLOR_CORRECTION_ABERRATION_MODE_OFF)
        try {
            b.set(CaptureRequest.DISTORTION_CORRECTION_MODE,
                CameraMetadata.DISTORTION_CORRECTION_MODE_OFF)
        } catch (e: Exception) { }
        val fd = profile.optLong("frame_interval_ms", 0L)
        if (fd > 0) b.set(CaptureRequest.SENSOR_FRAME_DURATION, fd * 1_000_000L)
        return b.build()
    }

    private var framesSeen = 0
    private var warmupDropped = 0

    // ---- 生效物理镜头（换镜 = 换内参，必须能看见）----
    //
    // 背景（M0 原始能力报告 m0_capability.json）：
    //  · id=0 是**逻辑相机**，`CONTROL_ZOOM_RATIO` 区间 **[0.6, 10]**，由 3 颗物理镜头融合
    //    （id=2 主摄 6.55mm ≈23.1mm 等效 / id=3 超广角 2.16mm ≈14.3mm / id=4 9mm ≈59.4mm）；
    //  · ⇒ **0.6× 不是"主摄缩小"，而是切到 id=3 超广角**：不同传感器、不同像元
    //    （1.28 vs 2.394 µm）、不同光圈（f/2.2 vs f/1.62）⇒ **内参完全不同**
    //    （满幅 fx：主摄 2736 px、超广角约 1688 px）。
    //  因此必须：① 记录每帧生效镜头；② **会话内换镜要大声报出来** —— 否则一段数据里
    //  混着两套内参，而画面、时间戳、参数回读全都正常（又一个静默失效）。
    private var lensId: String = ""
    private var lensChanges = 0

    private fun onResult(result: TotalCaptureResult) {
        val ts = result.get(CaptureResult.SENSOR_TIMESTAMP) ?: return
        noteGridDelta(ts)
        framesSeen++
        noteLens(result)
        // ⚠ `live` 模式**没有留存**：结果回调在这里就收工。
        // 继续往下走会把 `storeExpected` 这类"只对留存有意义的分母"搅乱 ——
        // 界面上会显示成"应有 N 帧、实存 0 帧"，看起来像**整批丢失**，
        // 而实际上这是设计（只看不留）。那种假故障比没有数字更坏。
        if (captureMode == MODE_LIVE) return
        // 留存的曝光/ISO/镜头/skew 全在**结果侧**，而图与结果是两条回调（R28）⇒
        // 这里为**每一帧**留一份小的结果元数据（只含小 JSON，不含像素），
        // 由 onImage 侧取走。刻意放在预热判断**之前**：口径统一，跳过与否由留存器自己决定。
        if (storeState == "recording") {
            storeMetaCache[ts] = resultMetaJson(result)
            while (storeMetaCache.size > STORE_META_KEEP) {
                storeMetaCache.remove(storeMetaCache.keys.first())
            }
        }
        // 预热：M0 实测第 0 帧 LENS_STATE=MOVING（机械收敛瞬态），② 的做法是丢前 5 帧。
        // 交付沿用同一丢弃规则 —— 否则"第一批留存帧"可能是在镜头还在动时拍的，
        // 而那种帧的几何是**静默**不可信的（画面正常、参数回读也正常）。
        // 跳过数进账本（`warmup_frames_skipped`），不是悄悄丢。
        if (framesSeen <= WARMUP_FRAMES) {
            warmupDropped++
            storeWarmupSkipped++
            if (framesSeen == WARMUP_FRAMES) {
                log("预热 $warmupDropped 帧已丢弃（镜头机械收敛段，几何静默不可信）"
                    + "；此后**每一帧都留**")
            }
            return
        }
        // ---- 抽帧：**默认不抽**（第 24 轮合并后的语义 = "每一帧都留"）----
        // `plan.interval_ms` 与 `profile.deliver_fps` 保留为**降载旋钮**：一旦设成 >0，
        // 语义就变成"只留一部分"，账本里的 `every_frame` 会变 false —— 必须显式可见。
        // 判据是**传感器时间差**（不是回调次数）—— 回调抖动/丢一次都不影响节奏。
        val intervalNs = currentIntervalNs()
        if (intervalNs > 0) {
            if (!intervalAdvised) { intervalAdvised = true; adviseInterval(intervalNs) }
            if (ts - lastSampledTs < intervalNs) return
        }
        val maxCount = plan.optInt("count", 0)
        if (maxCount > 0 && storeExpected >= maxCount) {
            log("按 plan 达到 $maxCount 帧，停止"); stopCapture(); return
        }
        lastSampledTs = ts
        noteSampled()
        // 账本分母：**已决定留存的帧数**。与 `frames_stored + gaps_on_device` 恒等。
        storeExpected++
        sweepPending(ts)                                          // 先清掉"永远等不到图"的旧账
        val y = recentY.remove(ts)
        if (y != null) {
            enqueue(ts, y, result)
        } else {
            // 图还没到：**结果必须留着**（见 recentResult 的注释 —— 丢掉它等于丢掉
            // 该帧整组 CaptureResult 派生字段，而且下游完全看不出来"缺了"）。
            recentResult[ts] = result
            while (recentResult.size > RECENT_RESULT_KEEP) {
                recentResult.remove(recentResult.keys.first())
            }
            pendingSample.add(ts)
        }
    }

    private fun onImage(rd: ImageReader) {
        // ⚠ 必须用 acquireNextImage()（FIFO），**不能用 acquireLatestImage()**：
        // 后者会「取最新、丢弃队列里更旧的图像」。交付路径的每一帧都要与它的
        // SENSOR_TIMESTAMP 配对（见 pendingSample / recentY），图被丢掉就意味着
        // 那帧的 ts 永远等不到图 ⇒ 抽帧被静默跳过。
        // 实测症状：期望 5 fps、实得约 2 fps，且丢的都是"结果先到"的那几帧。
        val img = try { rd.acquireNextImage() } catch (e: Exception) { null } ?: return
        try {
            val ts = img.timestamp
            val y = extractY(img)
            maybePreview(y, ts)      // 纯显示 + 监视道：不参与任何留存判定
            if (pendingSample.remove(ts)) {
                val rs = recentResult.remove(ts)
                if (rs == null) {
                    // 结果确实缺席（例如回调被丢）：如实记账，并在该帧元数据里标出来
                    metaMissingResult++
                    if (metaMissingResult == 1) {
                        log("⚠ 有已抽帧的结果对象缺席 ⇒ 该帧缺 skew/镜头/曝光 等字段（累计计数见 status）")
                    }
                }
                enqueue(ts, y, rs)
            } else {
                recentY[ts] = y
                while (recentY.size > RECENT_Y_KEEP) {
                    val it = recentY.keys.first()
                    recentY.remove(it)
                }
            }
        } catch (e: Exception) {
            log("取图失败: ${e.message}")
        } finally {
            try { img.close() } catch (e: Exception) { }
        }
    }

    // ------------------------------------------------- 预览（纯显示）+ 监视道（缩略图）
    private var lastPreviewAt = 0L
    private var previewDropped = 0
    private var lastThumbAt = 0L
    // ---- 监视道记账：**入队 / 真发出 / 被挤掉 / 发送失败** 四个数必须分开 ----
    //
    // 旧实现只有一个 `thumbsSent`，且它是在**入队成功**时 `++`（不是真发出去时），
    // 而 `ws.send` 失败只记 `thumbsDropped` ⇒ "手机说发了 30 张、电脑只收到 28 张"
    // 这种对不上账的情况**永远查不出来**（第 21 轮用户就是在问这个比例）。
    private var thumbsEnqueued = 0     // 交给发送线程的张数
    private var thumbsSent = 0         // **真正发出去的**张数（ws.send 返回）
    private var thumbsDropped = 0      // 被挤掉/未发送（未连接、会话未确认、图异常）
    private var thumbsSendFailed = 0   // 发出时抛异常的张数
    private var thumbsGated = 0        // 因"会话未被 PC 确认"而暂缓的张数
    private var thumbsRejected = 0     // 被 PC 明确拒收（thumb_rejected）的次数
    private var announceRetries = 0    // 被拒后重新声明会话的次数（上限，防死循环）

    // ---- PC 主控的启动/停止回执 ----
    //
    // **为什么必须有回执**：`start` / `stop` 原来是**单向广播** —— PC 只知道"WS 发送成功"，
    // 不知道"相机是否真的开起来了"。相机打开失败（设备级瞬时故障、被别的应用占用）时，
    // PC 面板会一直显示 running，而实际上什么都没采到。主控要成立，指令必须有闭环。
    @Volatile private var remoteStart = false    // 本次采集由 PC 的 start 发起
    private var startAckSent = false

    /**
     * 把 Y 平面降采样成灰度位图 + 交叉线，节流到 ~8 fps；**顺带产出监视道缩略图**。
     *
     * **为什么缩略图不复用交付帧、也不进交付队列**（D6 双车道）：
     * 监视道要的是"秒级看到取景"，它必须在**交付道堵住时仍然可用**（这正是两条车道
     * 分离的意义）。所以它是从"已经采到的那一帧"现算出来的 480 宽 JPEG（约 20–40 KB），
     * 走 WS 控制道发出；**不排队、不落盘、断开即丢**（丢了只影响"看不见画面"，
     * 不影响任何交付数据的完整性）。
     */
    private fun maybePreview(y: ByteArray, ts: Long) {
        val now = SystemClock.elapsedRealtime()
        if (now - lastPreviewAt < 125) { previewDropped++; return }   // 采得快、画得慢：丢显示帧不丢采集帧
        lastPreviewAt = now
        try {
            val w = reader?.width ?: return
            val h = reader?.height ?: return
            if (y.size < w * h) return
            val step = maxOf(1, w / 480)                              // 预览宽度 ~480
            val pw = w / step
            val ph = h / step
            val px = IntArray(pw * ph)
            var i = 0
            for (r in 0 until ph) {
                val row = r * step * w
                for (c in 0 until pw) {
                    val v = y[row + c * step].toInt() and 0xFF
                    px[i++] = (0xFF shl 24) or (v shl 16) or (v shl 8) or v
                }
            }
            // 十字准线（画在像素数组上，省一次 Canvas 绘制）：帮现场把目标摆到画面中心
            val cx = pw / 2
            val cy = ph / 2
            val arm = maxOf(8, pw / 12)
            for (d in -arm..arm) {
                px[cy * pw + (cx + d).coerceIn(0, pw - 1)] = 0xFFFF3B30.toInt()
                px[(cy + d).coerceIn(0, ph - 1) * pw + cx] = 0xFFFF3B30.toInt()
            }
            val bmp = android.graphics.Bitmap.createBitmap(px, pw, ph,
                android.graphics.Bitmap.Config.ARGB_8888)
            if (now - lastThumbAt >= THUMB_INTERVAL_MS) {
                lastThumbAt = now
                sendThumb(bmp, ts)
            }
            val info = buildString {
                append(if (running.get()) "采集 $w×$h" else "未开流")
                append("  队列 ").append(queueCount()).append(" 帧")
                append("  温度 ").append(batteryTemp()).append("C")
                append("  抽帧→").append(deliveredCount)
                if (previewDropped > 0) append("  显示丢 ") .append(previewDropped) .append(" 帧")
            }
            onPreview(bmp, info)
        } catch (e: Exception) {
            log("预览失败: ${e.message}")
        }
    }

    /**
     * 监视道：把位图编成 JPEG（q70）后经 WS 发出（`thumb`，§7.1）。
     *
     * **纹理：走文本帧 + base64**，而不是二进制帧 —— 本工程是零依赖的
     * 手写 WS 客户端，只实现了文本帧；480 宽 JPEG 的 base64 也就 30–50 KB，
     * 1 Hz 的量级（≈0.4 Mbps）对控制道毫无压力。这个选择写进了方案 §7.1。
     *
     * 失败/未连接一律**静默丢弃**并计数（监视道是可丢的，见 maybePreview 的说明）；
     * 但计数要能看见，否则"面板一直黑屏"会查不出原因。
     */
    private fun sendThumb(bmp: android.graphics.Bitmap, ts: Long) {
        if (ws == null || !connected) { thumbsDropped++; return }
        if (!sessionConfirmed) {
            // 还没确认会话就发，PC 只会回一句"未知会话"并丢掉 —— 不如自己先不发
            thumbsGated++
            if (!thumbGateLogged) {
                thumbGateLogged = true
                log("监视道暂缓：等 PC 确认会话（session_ack 或本会话首帧 verified 后开始）")
            }
            return
        }
        try {
            val bos = java.io.ByteArrayOutputStream(64 * 1024)
            bmp.compress(android.graphics.Bitmap.CompressFormat.JPEG, 70, bos)
            val jpg = bos.toByteArray()
            if (jpg.size < 512) { thumbsDropped++; return }        // 明显异常就不发
            if (jpg.size > 400 * 1024) { thumbsDropped++; return }  // 防守：不该这么大
            val payload = JSONObject().apply {
                put("type", "thumb")
                put("session_id", sessionId)
                put("seq", deliveredCount)          // 用"已抽帧数"当序号：与交付进度对得上
                put("sensor_timestamp_ns", ts)
                put("capture_path_id", PATH_ID)
                put("capture_path_rev", PATH_REV)
                put("jpeg_bytes", jpg.size)
                put("jpeg_b64", android.util.Base64.encodeToString(jpg, android.util.Base64.NO_WRAP))
                put("meta", JSONObject().apply {
                    put("state", if (running.get()) "running" else "idle")
                    put("queue_len", queueCount())
                    put("frames_seen", framesSeen)
                    put("preview_w", bmp.width)
                    put("preview_h", bmp.height)
                })
            }.toString()
            // **不能在这里直接发**：sendThumb 跑在相机 HandlerThread 上，
            // 而 WsClient.send 是阻塞式写 socket —— 链路慢/PC 卡顿时会把帧回调堵住，
            // 进而扰动"哪一帧被抽中"。入队交给专用线程，队列满就丢最旧的（监视道可丢）。
            if (!thumbQueue.offer(payload)) {
                thumbQueue.poll()
                if (!thumbQueue.offer(payload)) { thumbsDropped++; return }
                thumbsDropped++          // 挤掉了队列里最旧的一张（监视道取"最新"语义）
            }
            thumbsEnqueued++
        } catch (e: Exception) {
            thumbsDropped++
            log("监视道发送失败: ${e.message}")
        }
    }

    // 监视道发送线程：从队列取 → 由它来写 socket（把阻塞从相机线程上挪走）
    private val thumbQueue = java.util.concurrent.ArrayBlockingQueue<String>(2)
    private var thumbThread: Thread? = null

    private fun startThumbSender() {
        if (thumbThread != null) return
        thumbThread = Thread {
            while (connected) {
                val msg = try {
                    thumbQueue.poll(500, java.util.concurrent.TimeUnit.MILLISECONDS)
                } catch (e: Exception) { null }
                if (msg != null) {
                    // 只有 `send` 真的返回了才记 `thumbsSent`：把"入队"当"发出"
                    // 会让"手机发了 N 张、电脑只收到 M 张"永远对不上账。
                    val w = ws
                    if (w == null) thumbsSendFailed++
                    else try { w.send(msg); thumbsSent++ }
                         catch (e: Exception) { thumbsSendFailed++ }
                }
            }
            thumbThread = null
        }.also { it.isDaemon = true; it.start() }
    }

    private var samplesLost = 0
    private var intervalAdvised = false

    /**
     * 把"请求的抽帧间隔"换算成**实际落点**，并给出可用的替代值。
     *
     * 为什么必须有这句话：抽帧规则是"取第一个与上次样本相隔 ≥ interval 的帧"，
     * 而帧只存在于 33.288 ms 的栅格上 ⇒ **请求值会被量化到整数个帧周期**。
     * 实测：`interval_ms=200` 在该机型上量化成 **233.0 ms（4.29 fps）**而不是 5 fps
     * —— 因为 6 个帧周期只有 199.7 ms（差 0.3 ms 没够上），只能等第 7 个。
     * 这类"少了两成却没报错"的偏差只能靠这句话让人看见；改不改是现场的决定。
     */
    /** 记录本帧生效的物理镜头；会话内变化时告警（**不中止**：数据仍有效，只是分属两套内参）。 */
    private fun noteLens(result: TotalCaptureResult) {
        val id = result.get(CaptureResult.LOGICAL_MULTI_CAMERA_ACTIVE_PHYSICAL_ID) ?: return
        if (lensId.isEmpty()) {
            lensId = id
            log("生效物理镜头 = $id（请求 zoom=$reqZoom，回读 " +
                "zoom=${num(result.get(CaptureResult.CONTROL_ZOOM_RATIO))}）；同一段会话内必须保持唯一，" +
                "否则内参不唯一")
            return
        }
        if (id != lensId) {
            lensChanges++
            log("⚠ 会话内换镜：$lensId → $id（第 $lensChanges 次）。**该段数据不再共用一套内参**，" +
                "下游必须按镜头分段；若本意是固定档位，请把 zoom 调离切镜阈值")
            lensId = id
        }
    }

    private fun adviseInterval(intervalNs: Long) {
        val grid = gridPeriodNs()
        if (grid <= 0) return
        val n = maxOf(1L, Math.ceil(intervalNs.toDouble() / grid).toLong())
        val effMs = n * grid / 1e6
        val wantFps = 1e9 / intervalNs
        val nWant = maxOf(1L, Math.round(wantFps / (1e9 / grid)))
        val suggest = Math.floor(nWant * grid / 1e6 - 0.5).toLong().coerceAtLeast(1L)
        log(String.format(
            "抽帧落点：请求 %d ms ≈ 帧栅格的 %.2f 倍（实测栅格 %.3f ms）⇒ 实际落在第 %d 帧后 = " +
            "%.1f ms（%.2f fps）。**请求值会被量化到整数个帧栅格**：若要 %.1f fps，" +
            "interval_ms 应设为 %d（不是 %d）",
            intervalNs / 1_000_000, intervalNs.toDouble() / grid, grid / 1e6, n, effMs,
            1000.0 / effMs, wantFps, suggest, intervalNs / 1_000_000))
    }

    // 帧栅格：最近若干帧的 SENSOR_TIMESTAMP 间隔的**最小值**。
    // 用最小值而不是平均：偶尔漏一次回调只会让某个间隔变成 2 倍，最小值才是栅格本身。
    private var lastTsForGrid = 0L
    private val gridDeltas = ArrayList<Long>()

    private fun noteGridDelta(ts: Long) {
        if (lastTsForGrid > 0) {
            val d = ts - lastTsForGrid
            if (d in 1..200_000_000L) {
                synchronized(gridDeltas) {
                    gridDeltas.add(d)
                    while (gridDeltas.size > 90) gridDeltas.removeAt(0)
                }
            }
        }
        lastTsForGrid = ts
    }

    private fun gridPeriodNs(): Long = synchronized(gridDeltas) {
        if (gridDeltas.size < 5) 0L else gridDeltas.min()
    }


    /**
     * 清掉"等不到图"的抽帧记录并计数。
     *
     * ImageReader 队列深度只有 4，而同一帧的图**通常**在结果前后几毫秒内到达；
     * 若某个被抽中的时间戳等了超过 ~10 帧周期还没有图，那张图不会再来了
     * ⇒ 这是一次**静默少帧**，必须能被看见（否则"抽了 100 帧、交付 80 帧"永远说不清）。
     */
    private fun sweepPending(nowTs: Long) {
        if (pendingSample.isEmpty()) return
        val stale = pendingSample.filter { nowTs - it > 10L * 33_288_000L }
        if (stale.isEmpty()) return
        stale.forEach { pendingSample.remove(it); recentResult.remove(it) }
        samplesLost += stale.size
        // 这些帧**确实没留**（图回调从未到达）⇒ 与"写队列满"一样进账本（只能重采）
        stale.forEach { noteStoreGap(it, "image_never_arrived") }
        log("⚠ 有 ${stale.size} 个已决定留存的时间戳等不到对应图像（累计 $samplesLost）"
            + " ⇒ 这些帧**没留**，已记入缺口")
    }

    private val enqSlotSec = LongArray(10) { -1L }
    private val enqSlotCount = IntArray(10)

    private fun noteEnqueued() {
        val sec = SystemClock.elapsedRealtime() / 1000
        synchronized(rateLock) {
            val i = (sec % 10).toInt()
            if (enqSlotSec[i] != sec) { enqSlotSec[i] = sec; enqSlotCount[i] = 0 }
            enqSlotCount[i]++
        }
    }

    /** 最近 10 秒"成功入队（=待交付）"的速率（帧/s）。 */
    fun deliveredRateFps(): Double {
        val sec = SystemClock.elapsedRealtime() / 1000
        synchronized(rateLock) {
            var n = 0
            for (i in 0 until 10) if (enqSlotSec[i] in (sec - 9)..sec) n += enqSlotCount[i]
            return n / 10.0
        }
    }

    /**
     * 读该帧元数据里的会话号。
     *
     * 为什么需要：本地队列是**全局一个**（跨会话补传），所以"上传成功了一帧"不等于
     * "当前会话已被 PC 认识" —— 必须看这一帧**属于哪个会话**（见首帧 verified 开闸处的注释）。
     */
    private fun metaSessionId(jf: File): String = try {
        JSONObject(jf.readText()).optString("session_id")
    } catch (e: Exception) {
        ""
    }

    private fun queueCount(): Int =
        queueDir.listFiles { f -> f.name.endsWith(".yuv") }?.size ?: 0

    /** 按 `rowStride` 逐行取 Y 平面并重排成紧密排布（下游按 width 读即可）。 */
    /**
     * 取**一个平面**为紧密排布的一维字节数组。
     *
     * 交付道只用 Y（`planes[0]`）；实时道的彩色要用 U/V（`planes[1]/[2]`）⇒
     * 这份 stride 处理必须**共用一份实现** —— 各写一份，必然在其中一处漏掉
     * `pixelStride`（那种错的表现是"画面整体错位/串色"，而参数回读全都正常）。
     *
     * ⚠ 两个最容易写错的点：① `rowStride` 可能大于宽度（补齐行）；
     * ② **U/V 的 `pixelStride` 通常是 2（交错）**，且它们的宽高是亮度的一半（4:2:0）。
     *
     * ⚠⚠ 第三条（本轮现场踩到）：**不许用相对 position 的批量读**（`buf.get(out,0,n)`）。
     * `position` 是跨调用残留的可变状态 ⇒ 读到的字节数对、位置错，且**不报错**。
     * 详见函数体里的注释。
     */
    private fun planeToBytes(p: Image.Plane, w: Int, h: Int): ByteArray {
        val buf = p.buffer
        val out = ByteArray(w * h)
        val rowStride = p.rowStride
        val pixelStride = p.pixelStride
        // ⚠⚠ **基址必须取 `buf.position()`，不能硬写 0**。
        //
        // NV12/NV21 的 U/V **共享同一个底层 buffer**，Camera2 靠
        // `planes[2].buffer` 的 **position = 1** 标记 V 平面相对 U 平面的字节偏移。
        // ⇒ 绝对索引必须从 `position` 起算：
        //     idx = position + r*rowStride + c*pixelStride
        // 写成 `buf.get(r * rowStride + ...)`（隐含基址 0）在这种设备上
        // 会把 **U 的字节当成 V** —— 亮度完全正常（Y 是独立平面、position=0），
        // 只有颜色脏，且**不抛任何异常**。
        //
        // 旧实现的三条分支在这里**全都**从 0 起算（快路径用相对 position 读看似
        // 避开了，但它的正确性又依赖"position 没被前面的调用挪过"），
        // ⇒ 无论走哪条，NV12 的 V 平面都可能取错。
        //
        // 另一半修复：**三条路径必须统一用绝对索引 `buf.get(index)`**。
        // 相对 position 的批量读（`buf.get(out,0,n)`）是**跨调用残留的可变状态**，
        // 读到的字节数对、位置错，且不报错（Y/U/V 各调一次本函数，谁先跑谁挪 position）。
        val base = buf.position()
        if (pixelStride >= 1) {
            for (r in 0 until h) {
                var idx = base + r * rowStride
                for (c in 0 until w) {
                    out[r * w + c] = buf.get(idx)
                    idx += pixelStride
                }
            }
        } else {
            // `pixelStride == 0` 的设备不存在；真出现时**宁可全 0 也不要读错位置**：
            // 一片确定的中性灰还能看出"色度没了"，随机字节只会伪装成"色彩丰富"。
            java.util.Arrays.fill(out, 0.toByte())
        }
        return out
    }

    private fun extractY(img: Image): ByteArray {
        val p = img.planes[0]
        val out = planeToBytes(p, img.width, img.height)
        lastRowStride = p.rowStride
        return out
    }

    // ================================================ 实时道（`capture_mode=live`）
    //
    // 需求原话：「监视道添加可选项，此时只实时回传数据，不储存也不在事后回传额外的数据。
    // 这一过程允许使用彩色传输」＋「阻塞发送但是要保证实时性，甚至发送完再拍摄，发送都可以」。
    //
    // 落地要点（每一条都能对应到一处代码）：
    //   ① **不储存** = 不调 `startStore()` / 不 `enqueue()` / 不落盘 / 不排空；
    //   ② **彩色**   = 从 `planes[1]/[2]` 取 U/V 做 YUV→RGB（彩色数据本来就在 Image 里，
    //      只是交付道的 `extractY` 把它丢掉了 ⇒ **不增加采集成本，只增加编码与传输**）；
    //   ③ **实时**   = 串行闭环 `acquireLatestImage → 编码 → 阻塞 POST → 回到 acquire`。
    //      目标不是高 fps，而是**延迟不累积**（有队列时第 N 帧延迟 = 队列长 × 单帧时间，
    //      随积压线性增长；串行则恒为"编码 + 单帧传输"，与运行时长无关）。

    private fun resetLiveLedger() {
        liveSkippedEst = 0
        liveSent = 0
        liveDropped = 0
        liveBytes = 0L
        liveLastLatencyMs = 0L
        liveLatencySumMs = 0L
        liveLatencyN = 0
        liveFirstAtMs = 0L
        liveLastAtMs = 0L
        liveDropLogged = 0
    }

    /**
     * 实时道主循环：**串行闭环**（发完一帧才取下一帧）。
     *
     * ⚠ 用 `acquireLatestImage()`（**与交付道相反**）：交付道必须 FIFO 逐帧配对
     * SENSOR_TIMESTAMP（见 [onImage] 的注释 —— 图被丢掉就意味着那帧的配对永远等不到图）；
     * 本道要「此刻最新」，被跳过的陈帧记 `liveSkippedEst`。
     * **两个模式对取图语义的要求相反 ⇒ 不能共用同一个 `onImage` 回调。**
     *
     * ⚠ 这个线程**不是**相机回调线程：即使卡在一次 HTTP 上，也只影响"下一帧什么时候取"，
     * 不会堵住 CameraDevice 的 HandlerThread 消息（否则现场会表现为"画面与采集一起卡住"，
     * 而原因看起来在别处 —— 第 15 条铁律的又一次应用）。
     */
    private fun startLiveLoop() {
        if (liveThread?.isAlive == true) return
        liveThread = Thread {
            val fullW = reader?.width ?: 0
            val fullH = reader?.height ?: 0
            if (fullW <= 0 || fullH <= 0) { liveThread = null; return@Thread }
            val tw = liveWidth.coerceIn(64, fullW)
            val th = ((tw.toLong() * fullH / fullW).toInt() / 2 * 2).coerceAtLeast(2)
            log("实时道开跑：${tw}x$th 彩色 JPEG q$liveQuality，**颜色来源=$liveColorSource**" +
                "（ImageReader 格式=$readerFmt），串行闭环" +
                "（发完一帧才取下一帧 ⇒ 延迟不累积，而不是追求高 fps）")
            if (liveColorSource == COLOR_SRC_RGBA_HAL) {
                log("  ⚠ 本档的 YUV→RGB 由**相机 HAL** 完成 —— 若它**也偏绿**，" +
                    "则我们的 BT.601 换算是清白的（责任在 HAL 或显示链路）")
            }
            var prevNs = 0L
            val periodNs = gridPeriodNs().takeIf { it > 0 } ?: 33_300_000L
            while (running.get() && !liveStop) {
                val t0 = SystemClock.elapsedRealtime()
                val nowNs = SystemClock.elapsedRealtimeNanos()
                if (prevNs > 0) {
                    // `skipped_est`：两次取图的间隔里有几个帧栅格 ⇒ 中间那些陈帧被
                    // `acquireLatestImage` 丢掉了（**这是设计**，不是缺陷）。
                    val n = Math.round((nowNs - prevNs).toDouble() / periodNs)
                    if (n > 1) liveSkippedEst += (n - 1).toInt()
                }
                prevNs = nowNs
                val img = try { reader?.acquireLatestImage() } catch (e: Exception) { null }
                if (img == null) { Thread.sleep(5); continue }
                // ⚠ `ts` 必须在 `img.close()` 之前取（关了就没了）。
                // `finally` 里关图 ⇒ 编码抛异常也不会泄漏 `Image` 缓冲
                //（不关的后果是相机缓冲区耗尽、**整条流静默停住**）。
                val ts = img.timestamp
                val jpg: ByteArray? = try {
                    encodeColorJpeg(img, tw, th)
                } catch (e: Exception) {
                    log("实时道编码失败: ${e.message}"); null
                } finally {
                    try { img.close() } catch (e: Exception) { }
                }
                if (jpg == null) { liveDropped++; Thread.sleep(2); continue }
                val ok = postLiveFrame(jpg, ts)
                val dt = SystemClock.elapsedRealtime() - t0
                liveLastLatencyMs = dt
                liveLatencySumMs += dt
                liveLatencyN++
                if (ok) {
                    liveSent++
                    liveBytes += jpg.size
                    liveLastAtMs = SystemClock.elapsedRealtime()
                    if (liveFirstAtMs == 0L) liveFirstAtMs = liveLastAtMs
                } else {
                    liveDropped++
                }
            }
            liveThread = null
            log("实时道已停：发出 $liveSent 帧 / 异常丢 $liveDropped 帧 / 跳过陈帧(估) $liveSkippedEst")
        }.also { it.isDaemon = true; it.start() }
    }

    /**
     * YUV_420_888 → RGB 并降采样成 JPEG（实时道专用）。
     *
     * **为什么彩色不增加采集成本**：相机本来就出 YUV_420_888，U/V 已经在 `Image` 里，
     * 只是交付道的 `extractY` 把它们丢掉了。所以"上彩色"的全部代价在**编码与传输**，
     * 不在采集（这也是选它作为 live 默认形态的原因）。
     *
     * ⚠ 本函数的产物**有损**（JPEG + 降采样）⇒ 只用于"看得见"，
     * **不得用于测量/定位**（与交付道 `lossy:false` 是两回事）。
     */
    private fun encodeColorJpeg(img: Image, tw: Int, th: Int): ByteArray? {
        val w = img.width
        val h = img.height
        val cw = (w + 1) / 2
        val ch = (h + 1) / 2
        // ⚠ **按 reader 格式分派，不按 color_source 字面**：格式是**事实**，
        //   字面只是请求。若两者不一致（HAL 不支持而被降级），这里必须跟着事实走 ——
        //   否则会去读 RGBA reader 的"Y 平面"，而它只有一个 plane ⇒ IndexOutOfBounds，
        //   现场表现是"实时道疯狂报错"，而真正原因是降级了。
        if (img.format == IMG_FMT_RGBA) {
            return encodeRgbaJpeg(img, tw, th)
        }
        val y = planeToBytes(img.planes[0], w, h)
        val u = planeToBytes(img.planes[1], cw, ch)
        val v = planeToBytes(img.planes[2], cw, ch)
        val px = IntArray(tw * th)
        val sx = w.toDouble() / tw
        val sy = h.toDouble() / th
        var i = 0
        for (r in 0 until th) {
            val cy = (r * sy).toInt().coerceIn(0, h - 1)
            val cu = (cy / 2).coerceIn(0, ch - 1)
            for (c in 0 until tw) {
                val cx = (c * sx).toInt().coerceIn(0, w - 1)
                val cuv = (cx / 2).coerceIn(0, cw - 1)
                val Y = y[cy * w + cx].toInt() and 0xFF
                val U = (u[cu * cw + cuv].toInt() and 0xFF) - 128
                val V = (v[cu * cw + cuv].toInt() and 0xFF) - 128
                // BT.601 有限范围（相机的 YUV 就是这一套；用错矩阵的典型症状是
                // 肤色/天空明显偏色 —— 而画面"看起来正常"，不比对看不出来）
                val yy = (Y - 16).coerceAtLeast(0)
                val R = ((298 * yy + 409 * V + 128) shr 8).coerceIn(0, 255)
                val G = ((298 * yy - 100 * U - 208 * V + 128) shr 8).coerceIn(0, 255)
                val B = ((298 * yy + 516 * U + 128) shr 8).coerceIn(0, 255)
                px[i++] = (0xFF shl 24) or (R shl 16) or (G shl 8) or B
            }
        }
        // 十字准线保留（现场摆位要用）—— 与监视道一致
        drawCrosshair(px, tw, th)
        return jpegEncode(px, tw, th)
    }

    /**
     * `RGBA_8888` 档的一帧：**颜色是 HAL 算的**，我们只下采样 + 编码。
     *
     * ★ 这是第 29 轮"绿偏归属"的**判决性实验**那一半：
     * 现场 `yuv_bt601` 档偏绿，而同场景相机原生照片正常。若本档**也偏绿**，
     * 就证明我们的 BT.601 换算**是清白的**（责任在相机 HAL 或显示链路）；
     * 若本档正常，责任就在我们的换算里。**两条路必有一条绿 ⇒ 归属唯一确定。**
     *
     * ⚠ 与 `planeToBytes` 同源的坑：这里**同样只用绝对索引 `buf.get(index)`**，
     *   且**基址取 `buf.position()`** —— RGB reader 的 position 语义由 HAL 定，
     *   猜错就是整体偏色且不报错。
     * ⚠ 通道顺序按 Android 文档为 **R,G,B,A**。这一点**必须锚定文档**，
     *   不能凭印象 —— 写反了会得到"红蓝颠倒"，那是**另一个**故障；
     *   把它误当成"偏绿"会把排查带偏一整条街。
     */
    private fun encodeRgbaJpeg(img: Image, tw: Int, th: Int): ByteArray? {
        val w = img.width
        val h = img.height
        val p = img.planes[0]
        val buf = p.buffer
        val rowStride = p.rowStride
        val pixelStride = p.pixelStride
        val base = buf.position()          // 见上：基址不能写死 0
        val px = IntArray(tw * th)
        val sx = w.toDouble() / tw
        val sy = h.toDouble() / th
        var i = 0
        for (r in 0 until th) {
            val cy = (r * sy).toInt().coerceIn(0, h - 1)
            val rowBase = base + cy * rowStride
            for (c in 0 until tw) {
                val cx = (c * sx).toInt().coerceIn(0, w - 1)
                val o = rowBase + cx * pixelStride
                val R = buf.get(o).toInt() and 0xFF
                val G = buf.get(o + 1).toInt() and 0xFF
                val B = buf.get(o + 2).toInt() and 0xFF
                px[i++] = (0xFF shl 24) or (R shl 16) or (G shl 8) or B
            }
        }
        drawCrosshair(px, tw, th)
        return jpegEncode(px, tw, th)
    }

    /**
     * 画十字准线（**两条颜色来源共用**）。
     *
     * ⚠ 为什么不各写一份：同一段逻辑抄两遍必然漂移，而且漂移是**静默**的
     *   （准线还在，只是不在中心了，现场看着"画面正常"）。
     * 这段同时是**下游色彩链路的自检探针**：它在 YUV→RGB **之后**、JPEG **之前**
     *   写入已知色，所以它在传输图里长什么样，直接回答
     *   "JPEG 编码 / HTTP 传输 / 浏览器显示 有没有偏色"。
     */
    private fun drawCrosshair(px: IntArray, tw: Int, th: Int) {
        val cx0 = tw / 2
        val cy0 = th / 2
        val arm = maxOf(6, tw / 20)
        for (d in -arm..arm) {
            px[cy0 * tw + (cx0 + d).coerceIn(0, tw - 1)] = 0xFFFF3B30.toInt()
            px[(cy0 + d).coerceIn(0, th - 1) * tw + cx0] = 0xFFFF3B30.toInt()
        }
    }

    /** 像素数组 → JPEG（两条颜色来源共用，避免压缩参数两处各写一遍而漂移）。 */
    private fun jpegEncode(px: IntArray, tw: Int, th: Int): ByteArray? {
        val bmp = android.graphics.Bitmap.createBitmap(px, tw, th,
            android.graphics.Bitmap.Config.ARGB_8888)
        return try {
            val bos = java.io.ByteArrayOutputStream(128 * 1024)
            if (bmp.compress(android.graphics.Bitmap.CompressFormat.JPEG, liveQuality, bos))
                bos.toByteArray() else null
        } finally {
            bmp.recycle()
        }
    }

    /**
     * 实时道的一帧：**阻塞** POST（串行闭环的另一半）。返回是否真的发出去了。
     *
     * 为什么用 HTTP 二进制而不是复用 WS：`live` 是"尽力发"，必然占满链路 ——
     * 同一条 WS 上跑这个量级，`start`/`stop` 的时延会被挤；且 WS 走文本帧 + base64
     * （**+33% 膨胀**）。`live` 模式下 App 侧的 WS 缩略图通道**关闭**，
     * 控制道只剩指令与事件，两端都干净。
     *
     * ⚠ 失败**不重试**：本道是"看现在"，重发一帧陈画面没有意义（旧帧越积越偏）。
     * 丢就丢，但**必须计数**（`liveDropped`）—— 否则"画面卡住"查不出原因。
     */
    private fun postLiveFrame(jpg: ByteArray, ts: Long): Boolean {
        val base = httpBase
        if (base.isEmpty()) return false
        var c: HttpURLConnection? = null
        return try {
            c = (URL("$base/api/v1/live/frame").openConnection() as HttpURLConnection)
            c.requestMethod = "POST"
            c.doOutput = true
            c.connectTimeout = 4000
            c.readTimeout = LIVE_HTTP_READ_TIMEOUT_MS
            c.setFixedLengthStreamingMode(jpg.size)
            c.setRequestProperty("Content-Type", "image/jpeg")
            c.setRequestProperty("X-Session-Id", sessionId)
            c.setRequestProperty("X-Seq", liveSent.toString())
            c.setRequestProperty("X-Sensor-Timestamp-Ns", ts.toString())
            c.outputStream.use { it.write(jpg) }
            val code = c.responseCode
            if (code in 200..299) {
                // 安全旋钮（默认 0 = 不限，符合用户"阻塞发送"的口径）：
                // 现场若发现控制指令变慢，用它限流，而不是改架构。
                if (liveMaxMbps > 0) {
                    val ms = (jpg.size / (liveMaxMbps * 1024 * 1024) * 1000).toLong()
                    if (ms > 0) Thread.sleep(ms)
                }
                true
            } else {
                if (liveDropLogged < 3) {
                    liveDropLogged++
                    log("实时道被拒：HTTP $code ${c.responseMessage}（会话 $sessionId）")
                }
                false
            }
        } catch (e: Exception) {
            if (liveDropLogged < 3) {
                liveDropLogged++
                log("实时道发送失败: ${e.message}")
            }
            false
        } finally {
            try { c?.disconnect() } catch (e: Exception) { }
        }
    }

    /** 实时道账本（进 `status`；PC 侧与界面读的是**同一份**）。 */
    private fun liveStatusJson(): JSONObject = JSONObject().apply {
        val elapsed = if (liveFirstAtMs > 0 && liveLastAtMs > liveFirstAtMs)
            (liveLastAtMs - liveFirstAtMs) / 1000.0 else 0.0
        put("mode", captureMode)
        put("color", liveColor)
        // ⚠ 报**实际生效**的，不是请求的。二者在 HAL 不支持时会不同 ——
        //   只报请求值，PC 面板会显示"我在跑 rgba_hal"，而设备其实在跑 yuv_bt601，
        //   这次二分就白做了，且**表面上看不出任何异常**。
        put("color_source", liveColorSource)
        put("color_source_requested", colorSourceRequested)
        put("rgba_supported", rgbaSupported)          // null = 本会话未探测/已降级
        put("reader_format", readerFmt)
        put("width", liveWidth)
        put("quality", liveQuality)
        put("sent", liveSent)
        put("skipped_est", liveSkippedEst)
        put("dropped", liveDropped)
        put("bytes", liveBytes)
        put("mbps", if (elapsed > 0.5) liveBytes / elapsed / 1024 / 1024 else -1.0)
        put("fps_actual", if (elapsed > 0.5) liveSent / elapsed else -1.0)
        put("latency_ms", liveLastLatencyMs)
        put("latency_avg_ms", if (liveLatencyN > 0) liveLatencySumMs / liveLatencyN else -1L)
        put("max_mbps", liveMaxMbps)
        put("path_id", LIVE_PATH_ID)
        put("path_rev", LIVE_PATH_REV)
        put("note", "**设备零留存**：不落盘、不停采排空、不回传；" +
                    "`skipped_est`（陈帧，估算，设计如此）与 `dropped`（异常）**不许合并**")
    }

    // ------------------------------------------------------------------ 队列
    /**
     * 入队：元数据在**相机线程**上组好（纯内存、快），文件写盘交给 [writer] 线程。
     *
     * 为什么拆开：写一帧 2.7 MB（全幅 18.9 MB）到闪存会阻塞相机回调线程，
     * 而**图回调与结果回调跑在同一个 Handler 上**（M0 第 12 轮 R28 的教训）——
     * 在那里做 I/O 会让"图晚到"变成常态，甚至把抽帧节奏拖歪。
     */
    private fun enqueue(ts: Long, y: ByteArray, result: TotalCaptureResult?) {
        val meta = try { buildMeta(ts, y, result) } catch (e: Exception) {
            noteStoreGap(ts, "meta_build_failed")
            log("组元数据失败（该帧没留）: ${e.message}")
            return
        }
        val seq = meta.optInt("seq", -1)
        if (seq >= 0) {
            if (storeSeqFirst < 0) storeSeqFirst = seq
            storeSeqLast = seq
        }
        // 结果对象缺席：**照样留存这一帧**（像素是原始数据，不该因为元数据缺失就丢），
        // 但必须**自描述 + 计数**，否则下游会把"没有曝光值"当成"曝光值就是这个"。
        if (result == null) {
            storeResultMissing++
            if (storeResultMissing == 1) {
                log("⚠ 有帧的结果对象缺席 ⇒ 该帧缺 skew/镜头/曝光；已标 result_missing 并计数")
            }
        }
        // ⚠ **必须看返回值**。这里以前是 `writeQueue.offer(...)` 而不看结果 ⇒ 队列（容量 32）
        // 满时这一帧**静默消失**，而且**三方一致地看不出来**：
        //   · App 的 `deliveredCount` 只在写盘成功后 `++`（在写线程里）；
        //   · `queue_len` 是数磁盘上的 `.yuv`（也只反映写成功的）；
        //   · PC 的"收全了吗"就靠这两个数（`stop_ack` 的 frames_delivered / queue_len）。
        // ⇒ 少一帧时三方一致地少一帧，没有任何一方会报 —— 与"每一帧都留"直接冲突。
        if (!writeQueue.offer(meta.toString() to y)) {
            noteStoreGap(ts, "write_queue_full")
        }
    }

    private val writeQueue = java.util.concurrent.ArrayBlockingQueue<Pair<String, ByteArray>>(32)
    private var writer: Thread? = null

    private fun startWriter() {
        if (writer != null) return
        writer = Thread {
            var sinceSpaceCheck = 0
            var bytesSince = 0L
            var lastRateAt = SystemClock.elapsedRealtime()
            while (true) {
                val item = try { writeQueue.take() } catch (e: InterruptedException) { return@Thread }
                var gapTs = 0L
                try {
                    val dir = queueDir
                    if (!dir.exists()) dir.mkdirs()
                    val meta = JSONObject(item.first)
                    val seq = meta.optInt("seq", -1)
                    if (seq < 0) continue                     // 元数据没组出来，丢弃（下一帧照常）
                    gapTs = meta.optLong("sensor_timestamp_ns", 0L)
                    val sid = meta.optString("session_id", "s")
                    val base = "${sid}__${String.format("%06d", seq)}"
                    // 原子序：先写 .part 再改名 ⇒ 上传侧**只会看到完整的 .yuv**；
                    // 再写 .json。反过来（先 yuv 后 json）会让上传侧读到"有图无元数据"的中间态。
                    val tmp = File(dir, "$base.yuv.part")
                    tmp.writeBytes(item.second)
                    if (!tmp.renameTo(File(dir, "$base.yuv"))) {
                        File(dir, "$base.yuv").writeBytes(item.second); tmp.delete()
                    }
                    File(dir, "$base.json").writeText(item.first)
                    deliveredCount++
                    // 账本按**会话**记：`deliveredCount` 是全局的（还要当 seq 用），不能当账本用
                    if (sid == sessionId) {
                        storeStored++
                        storeBytes += item.second.size.toLong()
                    }
                    bytesSince += item.second.size.toLong()
                    val nowMs = SystemClock.elapsedRealtime()
                    if (nowMs - lastRateAt >= 2000) {
                        storeWriteMbps = bytesSince / 1024.0 / 1024.0 /
                            ((nowMs - lastRateAt) / 1000.0)
                        bytesSince = 0L
                        lastRateAt = nowMs
                    }
                    noteEnqueued()
                    log("入队 $base ${item.second.size / 1024}KB")
                    uploader?.kick()
                    // ---- 熔断：剩余空间低于余量 / 超过预算 ⇒ 停留存（**不能把手机写满**）----
                    // 检查点放在**写盘之后**：放之前会漏掉"最后一帧恰好把空间吃光"这一下。
                    if (++sinceSpaceCheck >= STORE_SPACE_CHECK_EVERY) {
                        sinceSpaceCheck = 0
                        val free = dir.usableSpace
                        if (free < storeReserveBytes()) {
                            abortStore("no_space",
                                "剩余空间 ${free / 1024 / 1024} MB < 余量保护 ⇒ 停留存。" +
                                "**已存下的帧照常回传**（这一步就是为了保住 App 与回传）")
                            return@Thread
                        }
                        if (storeBytes > storeBudgetBytes()) {
                            abortStore("budget_exhausted",
                                "留存已达预算 ${storeBytes / 1024 / 1024} MB ⇒ 停留存")
                            return@Thread
                        }
                    }
                } catch (e: Exception) {
                    // 写盘失败 = 这一帧**确实没留**（只能重采）⇒ 进账本，不许静默
                    noteStoreGap(gapTs, "write_failed")
                    log("写盘失败（该帧没留）: ${e.message}")
                }
            }
        }.also { it.isDaemon = true; it.start() }
    }

    /**
     * 逐帧结果元数据（**只含 `CaptureResult` 派生字段**，由 `buildMeta` 合并进帧元数据）。
     *
     * ⚠ **不能直接调 `buildMeta`**：它会 `nextSeq()` —— 在 `onResult` 里调用会**多消耗一个
     * 序号**（同一帧在 `onImage` 侧还要再组一次），于是 seq 出现空档，看起来像"丢了帧"。
     * 所以结果侧只留"纯派生字段"，**序号由 `onImage` 侧那一处统一分配**。
     */
    private fun resultMetaJson(rs: TotalCaptureResult): JSONObject = JSONObject().apply {
        put("focus_distance_diopter", num(rs.get(CaptureResult.LENS_FOCUS_DISTANCE)))
        put("zoom_ratio", num(rs.get(CaptureResult.CONTROL_ZOOM_RATIO)))
        put("af_mode", name(rs.get(CaptureResult.CONTROL_AF_MODE), "af"))
        put("ois_mode", name(rs.get(CaptureResult.LENS_OPTICAL_STABILIZATION_MODE), "ois"))
        put("eis_mode", name(rs.get(CaptureResult.CONTROL_VIDEO_STABILIZATION_MODE), "eis"))
        put("edge_mode", name(rs.get(CaptureResult.EDGE_MODE), "edge"))
        put("noise_reduction_mode", name(rs.get(CaptureResult.NOISE_REDUCTION_MODE), "nr"))
        put("control_enable_zsl", rs.get(CaptureResult.CONTROL_ENABLE_ZSL))
        put("exposure_time_ns", num(rs.get(CaptureResult.SENSOR_EXPOSURE_TIME)))
        put("iso", num(rs.get(CaptureResult.SENSOR_SENSITIVITY)))
        put("sensor_pixel_mode", num(rs.get(CaptureResult.SENSOR_PIXEL_MODE)))
        put("rolling_shutter_skew_ns", num(rs.get(CaptureResult.SENSOR_ROLLING_SHUTTER_SKEW)))
        put("active_physical_id",
            name(rs.get(CaptureResult.LOGICAL_MULTI_CAMERA_ACTIVE_PHYSICAL_ID), "physical_id"))
        put("lens_changes_so_far", lensChanges)
        rs.get(CaptureResult.SCALER_CROP_REGION)?.let { c ->
            put("crop_region", JSONArray(listOf(c.left, c.top, c.width(), c.height())))
        }
        put("battery_temperature_c", batteryTemp())
        put("requested", JSONObject().apply {
            put("focus_diopter", profile.optDouble("focus_diopter", 1.001001))
            put("zoom_ratio", reqZoom)
            put("zoom_source", zoomSource)
            put("delivery_fps", currentIntervalNs() / 1_000_000)   // 0 = 不抽帧（每帧都留）
        })
    }

    private fun buildMeta(ts: Long, y: ByteArray, result: TotalCaptureResult?): JSONObject {
        return try {
            val meta = JSONObject().apply {
                put("seq", nextSeq())
                put("session_id", sessionId)
                put("calib_id", calibId())
                put("capture_path_id", PATH_ID)
                put("capture_path_rev", PATH_REV)
                put("delivery", DELIVERY)
                put("format", FORMAT)
                put("sensor_timestamp_ns", ts)
                put("wall_clock_ns", System.currentTimeMillis() * 1_000_000L)
                put("boot_time_ns", 0)
                put("stream_start_elapsed_ns", streamStartElapsedNs)
                // 语义说明：连续流只下发过一次重复请求，"每帧的请求时刻"没有定义 ⇒
                // 这里记的是**距开流时刻**，与拍照路径的 ts−req 不是同一个量，不可混用。
                put("sensor_ts_minus_stream_start_ns", ts - streamStartElapsedNs)
                put("sensor_ts_minus_request_ns", JSONObject.NULL)
                put("stream_mode_note", "重复请求：帧↔请求一一对应关系不存在，本字段为空是语义正确")
                put("warmup_frames_dropped", warmupDropped)
                put("frames_seen_at_sample", framesSeen)
                put("sample_rule", if (currentIntervalNs() > 0)
                    "按 sensor_timestamp 间隔抽帧（plan.interval_ms / profile.deliver_fps）" +
                        " —— ⚠ 已启用降载：**不再是\"每一帧都留\"**"
                    else "不抽帧：**每一帧都留**（sensor 输出多少就留多少）")
                put("width", reader?.width ?: 0)
                put("height", reader?.height ?: 0)
                put("y_stride", reader?.width ?: 0)      // 已重排为紧密排布
                put("source_row_stride", lastRowStride)
                put("repacked", lastRowStride != (reader?.width ?: 0))
                put("size_bytes", y.size)
                result?.let { rs ->
                    put("focus_distance_diopter", num(rs.get(CaptureResult.LENS_FOCUS_DISTANCE)))
                    put("zoom_ratio", num(rs.get(CaptureResult.CONTROL_ZOOM_RATIO)))
                    put("af_mode", name(rs.get(CaptureResult.CONTROL_AF_MODE), "af"))
                    put("ois_mode", name(rs.get(CaptureResult.LENS_OPTICAL_STABILIZATION_MODE), "ois"))
                    put("eis_mode", name(rs.get(CaptureResult.CONTROL_VIDEO_STABILIZATION_MODE), "eis"))
                    put("edge_mode", name(rs.get(CaptureResult.EDGE_MODE), "edge"))
                    put("noise_reduction_mode", name(rs.get(CaptureResult.NOISE_REDUCTION_MODE), "nr"))
                    put("control_enable_zsl", rs.get(CaptureResult.CONTROL_ENABLE_ZSL))
                    put("exposure_time_ns", num(rs.get(CaptureResult.SENSOR_EXPOSURE_TIME)))
                    put("iso", num(rs.get(CaptureResult.SENSOR_SENSITIVITY)))
                    put("sensor_pixel_mode", num(rs.get(CaptureResult.SENSOR_PIXEL_MODE)))
                    put("rolling_shutter_skew_ns", num(rs.get(CaptureResult.SENSOR_ROLLING_SHUTTER_SKEW)))
                    put("active_physical_id",
                        name(rs.get(CaptureResult.LOGICAL_MULTI_CAMERA_ACTIVE_PHYSICAL_ID), "physical_id"))
                    put("lens_changes_so_far", lensChanges)      // 该帧之前本会话换过几次镜
                    put("callback_latency_ns",
                        (SystemClock.elapsedRealtimeNanos() - ts))
                    rs.get(CaptureResult.SCALER_CROP_REGION)?.let { c ->
                        put("crop_region", JSONArray(listOf(c.left, c.top, c.width(), c.height())))
                    }
                }
                // 结果缺席时**明确标出来**：字段"没有"与"没测"是两件事
                //（本项目铁律：判不了 ≠ 没问题；下游也不该把 null 当"值为 null"）。
                if (result == null) put("result_missing", true)
                put("battery_temperature_c", batteryTemp())
                put("requested", JSONObject().apply {
                    put("focus_diopter", profile.optDouble("focus_diopter", 1.001001))
                    put("zoom_ratio", reqZoom)
                    put("zoom_source", zoomSource)          // app_ui | pc_profile
                    put("exposure_time_ns", profile.optLong("exposure_time_ns", 10_000_000L))
                    put("iso", profile.optInt("iso", 200))
                    put("output_size", profile.optJSONArray("output_size"))
                    put("deliver_fps", currentIntervalNs() / 1_000_000)  // 0 = 不抽帧
                })
            }
            meta
        } catch (e: Exception) {
            log("组元数据失败: ${e.message}")
            JSONObject().put("seq", -1)
        }
    }

    /**
     * 自增序号（幂等键的一半：`session_id + device_serial + seq`）。
     *
     * 序号**持久化**在队列目录里：App 被系统杀掉再起来时，序号不能从头开始 ——
     * 否则新帧会撞上旧帧的幂等键，服务端会按"同键不同内容"直接 409 拒收（这是**对的**，
     * 但现场表现为"传不上去"）。所以每次分配都落一次盘（几字节，开销可忽略）。
     */
    private val seqCounter = java.util.concurrent.atomic.AtomicInteger(-1)

    private fun nextSeq(): Int {
        val dir = queueDir
        if (!dir.exists()) dir.mkdirs()
        val f = File(dir, "next_seq.txt")
        if (seqCounter.get() < 0) {
            seqCounter.set(f.takeIf { it.exists() }?.readText()?.trim()?.toIntOrNull() ?: 1)
        }
        val n = seqCounter.getAndIncrement()
        f.writeText((n + 1).toString())
        return n
    }

    private fun calibId(): String {
        // calib_id 由标定流程生成并冻结（§9）；M1 用"会话不变、标定未闭环"的占位符，
        // 并在日志里明说 —— 免得下游把它当成一个已标定的指纹。
        val f = File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "calib_id.txt")
        return if (f.exists()) f.readText().trim()
        else { val v = "calib_UNSET"; f.writeText(v); log("⚠ calib_id 尚未冻结，先用 $v"); v }
    }

    private fun batteryTemp(): Double {
        try {
            val i = ctx.registerReceiver(null,
                android.content.IntentFilter(android.content.Intent.ACTION_BATTERY_CHANGED))
            val t = i?.getIntExtra(android.os.BatteryManager.EXTRA_TEMPERATURE, -1) ?: -1
            return if (t > 0) t / 10.0 else 0.0
        } catch (e: Exception) { return 0.0 }
    }

    private fun num(v: Any?): Any = when (v) {
        null -> JSONObject.NULL
        is Int, is Long, is Double, is Boolean -> v
        is Float -> v.toDouble()
        else -> v.toString()
    }

    /** 枚举值转名字：记成 `"名字(整数码)"` —— **码一定在括号里**，所以即使名字认错，
     *  下游仍能拿到原始值（自描述：不依赖这张表是否正确，只依赖格式稳定）。
     *
     *  ⚠ v1.10.7 起**按字段各用一张表**。此前用一张"通用表"（0=OFF/1=ON/2=AUTO/…），
     *  而同一个整数在不同枚举里含义不同：`CONTROL_VIDEO_STABILIZATION_MODE=2` 是
     *  `PREVIEW_STABILIZATION`，通用表却把它记成 `"AUTO(2)"`。
     *  这种错标是**静默**的：数值没丢、字段没缺、计数器全正常，只是标签在骗人 ——
     *  下游（漂移检查、PC 开流门控）拿标签去比对就会得出错误结论。
     *  ⇒ 认不出的码保留裸整数，不猜名字。
     */
    private val ENUM_TABLES: Map<String, Map<Int, String>> = mapOf(
        // CaptureResult.CONTROL_AF_MODE
        "af" to mapOf(0 to "OFF", 1 to "AUTO", 2 to "MACRO", 3 to "CONTINUOUS_VIDEO",
                      4 to "CONTINUOUS_PICTURE", 5 to "EDOF"),
        // CaptureResult.LENS_OPTICAL_STABILIZATION_MODE
        "ois" to mapOf(0 to "OFF", 1 to "ON"),
        // CaptureResult.CONTROL_VIDEO_STABILIZATION_MODE（2 不是 AUTO！）
        "eis" to mapOf(0 to "OFF", 1 to "ON", 2 to "PREVIEW_STABILIZATION"),
        // CaptureResult.EDGE_MODE
        "edge" to mapOf(0 to "OFF", 1 to "FAST", 2 to "HIGH_QUALITY", 3 to "ZERO_SHUTTER_LAG"),
        // CaptureResult.NOISE_REDUCTION_MODE
        "nr" to mapOf(0 to "OFF", 1 to "FAST", 2 to "HIGH_QUALITY", 3 to "MINIMAL",
                      4 to "ZERO_SHUTTER_LAG"),
        // LOGICAL_MULTI_CAMERA_ACTIVE_PHYSICAL_ID 回读是字符串（"0"/"1"/"2"/"3"），
        // 不参与枚举映射；留一张空表让类型分支统一走同一个函数。
        "physical_id" to emptyMap(),
    )

    private fun name(v: Any?, kind: String): Any {
        if (v == null) return JSONObject.NULL
        val i = (v as? Int) ?: return v.toString()
        return ENUM_TABLES[kind]?.get(i)?.let { "$it($i)" } ?: i
    }

    /** 队列长度采样（时刻 ms, 帧数），用于算积压趋势；只留最近 60 s。 */
    private val queueHist = java.util.ArrayDeque<Pair<Long, Int>>()
    private var lastBacklogWarnMs = 0L

    // ------------------------------------------------------------------ 交付速率计
    //
    // **为什么必须有它**：现场实测只有 5.5 MB/s（≈2 帧/s），而要求的交付节奏可能是
    // 5 fps —— 采集率高于交付率时队列会**无界增长**（实测：开流期间 queue_len
    // 从 162 一路涨到 249，停流后才回落）。这类问题以前只能靠人读日志发现，
    // 现在它必须是**常驻可见的量**（进 status，PC 控制台直接看得到）。
    private val rateLock = Any()
    private val rateSlotSec = LongArray(10) { -1L }
    private val rateSlotBytes = LongArray(10)
    @Volatile private var rateTotalBytes = 0L
    @Volatile private var lastLinkMbps = -1.0        // 最近一次链路测速结果（-1=没测过）

    private fun noteUploaded(n: Int) {
        if (n <= 0) return
        val sec = SystemClock.elapsedRealtime() / 1000
        synchronized(rateLock) {
            val i = (sec % 10).toInt()
            if (rateSlotSec[i] != sec) { rateSlotSec[i] = sec; rateSlotBytes[i] = 0L }
            rateSlotBytes[i] += n.toLong()
            rateTotalBytes += n.toLong()
        }
    }

    private val sampleSlotSec = LongArray(10) { -1L }
    private val sampleSlotCount = IntArray(10)

    /**
     * 记一次"决定抽这一帧"。
     *
     * **为什么要单独记"决定"**：交付链路上有三段会吃帧 ——
     * ① 按 sensor 时间戳决定抽（期望 5 fps，实测受 33 ms 帧栅格相位影响 ≈4.6 fps）；
     * ② 决定后要等到与时间戳配对的图（图迟迟不来 ⇒ 静默少一帧）；
     * ③ 入队后被上传线程送出（链路限速）。
     * 只报 `upload_mbps` 的话，**② 的丢失完全看不见**（队列长度只反映"采到没传走"，
     * 反映不了"决定抽但根本没入队"）。
     */
    private fun noteSampled() {
        val sec = SystemClock.elapsedRealtime() / 1000
        synchronized(rateLock) {
            val i = (sec % 10).toInt()
            if (sampleSlotSec[i] != sec) { sampleSlotSec[i] = sec; sampleSlotCount[i] = 0 }
            sampleSlotCount[i]++
        }
    }

    /** 最近 10 秒"决定抽帧"的速率（帧/s）。 */
    fun sampleFps(): Double {
        val sec = SystemClock.elapsedRealtime() / 1000
        synchronized(rateLock) {
            var n = 0
            for (i in 0 until 10) if (sampleSlotSec[i] in (sec - 9)..sec) n += sampleSlotCount[i]
            return n / 10.0
        }
    }

    /** 最近 10 秒的平均上传速率（MB/s）。格子上带秒标记，过期格子自动不计入。 */
    fun uploadRateMbps(): Double {
        val sec = SystemClock.elapsedRealtime() / 1000
        synchronized(rateLock) {
            var sum = 0L
            for (i in 0 until 10) if (rateSlotSec[i] in (sec - 9)..sec) sum += rateSlotBytes[i]
            return sum / 1048576.0 / 10.0
        }
    }

    /**
     * 链路测速：往 `/api/v1/linktest` 灌 [mb] MB（服务端只收不落盘）。
     *
     * 作用是把**链路能力**与**采集循环开销**分开：相机不开时测出来的是这条
     * WiFi + 手机 HTTP 栈的上限。若它也很低，问题在链路/手机，改协议无益。
     * 对照值：PC 回环协议天花板 ≈43 MB/s（`pc/probe_throughput.py`）。
     */
    fun linkTest(mb: Int = 20) {
        if (!connected) { log("✗ 未连接，无法测速"); return }
        Thread {
            try {
                val block = ByteArray(1 shl 20).also { java.util.Random().nextBytes(it) }
                val total = mb * (1 shl 20)
                val c = (java.net.URL("$httpBase/api/v1/linktest")
                    .openConnection() as java.net.HttpURLConnection)
                c.requestMethod = "POST"
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/octet-stream")
                c.setFixedLengthStreamingMode(total)
                c.connectTimeout = 5000
                c.readTimeout = 120000
                val t0 = SystemClock.elapsedRealtime()
                c.outputStream.use { os -> repeat(mb) { os.write(block) } }
                val code = c.responseCode
                try { c.inputStream.bufferedReader().readText() } catch (e: Exception) { }
                val dt = maxOf((SystemClock.elapsedRealtime() - t0) / 1000.0, 1e-3)
                val rate = mb / dt
                lastLinkMbps = rate
                log(String.format("链路测速：%d MB / %.2f s = %.2f MB/s（%.0f Mbps，HTTP %d）",
                    mb, dt, rate, rate * 8 * 1.048576, code))
                log("  对照：回环协议天花板 ≈43 MB/s；真实交付率看 status 的 upload_mbps" +
                    "（两者都低 ⇒ 瓶颈在链路或手机端，改协议无效）")
            } catch (e: Exception) {
                log("链路测速失败: ${e.javaClass.simpleName}: ${e.message}")
            }
        }.also { it.isDaemon = true; it.start() }
    }

    // ------------------------------------------------------------------ 上传
    private var uploader: Uploader? = null

    private fun startStatusLoop() {
        if (statusThread != null) return
        statusThread = Thread {
            while (connected) {
                try {
                    val q = queueDir
                    val qs = q.listFiles { f -> f.name.endsWith(".yuv") } ?: emptyArray()
                    val n = qs.size
                    val qBytes = qs.sumOf { it.length() }
                    // 会话数按文件名前缀 `__` 之前那段统计；**旧命名（frame_000123.yuv，
                    // 没有 __）要单独计数** —— 否则每个旧文件名都被当成一个"不同会话"
                    // （实测：211 帧报出 queue_sessions=151，纯属指标算错）。
                    val qSess = qs.mapNotNull { f ->
                        f.name.substringBefore("__").takeIf { f.name.contains("__") }
                    }.toSet().size
                    val qLegacy = qs.count { !it.name.contains("__") }
                    val sf = StatFs(Environment.getExternalStorageDirectory().path)
                    // 积压趋势：采样 (时刻, 队列长度)，只留最近 60 s
                    val nowMs = SystemClock.elapsedRealtime()
                    queueHist.addLast(nowMs to n)
                    while (queueHist.isNotEmpty() && nowMs - queueHist.first().first > 60_000) {
                        queueHist.removeFirst()
                    }
                    val growFps = if (queueHist.size >= 2) {
                        val (t0, n0) = queueHist.first()
                        val span = (nowMs - t0) / 1000.0
                        if (span > 5) (n - n0) / span else 0.0
                    } else 0.0
                    val avgFrameB = if (n > 0) qBytes.toDouble() / n else 2_764_800.0
                    // 按当前积压速度，剩余空间还能撑多久 —— 这是"会不会静默写满磁盘"的答案
                    val etaMin = if (growFps > 0.05)
                        (sf.availableBytes / (growFps * avgFrameB)) / 60.0 else -1.0
                    // **一份字段清单，两个出口**（本地状态栏 + 发给 PC）。
                    // 曾经写成两份并列的 `JSONObject().apply {…}`，于是新增字段只加进一份、
                    // 另一份静默落后（实测：sample_fps / deliver_fps / samples_lost /
                    // plan_interval_ms / active_physical_id 只进了本地那份，
                    // 而现场是看 PC 控制台 —— 新仪器等于没上）。
                    val st = JSONObject().apply {
                        put("type", "status")
                        put("state", if (running.get()) "running" else "idle")
                        put("session_id", sessionId)
                        put("queue_len", n)
                        put("queue_bytes", qBytes)
                        put("queue_sessions", qSess)
                        put("queue_legacy_names", qLegacy)
                        put("storage_free_mb", sf.availableBytes / 1024 / 1024)
                        put("temperature_c", batteryTemp())
                        put("frames_delivered", deliveredCount)
                        put("upload_mbps", String.format("%.2f", uploadRateMbps()).toDouble())
                        put("linktest_mbps", String.format("%.2f", lastLinkMbps).toDouble())
                        put("backlog_fps", String.format("%.2f", growFps).toDouble())
                        put("eta_full_min", String.format("%.1f", etaMin).toDouble())
                        put("thumbs_enqueued", thumbsEnqueued)
                        put("thumbs_sent", thumbsSent)
                        put("thumbs_dropped", thumbsDropped)
                        put("thumbs_send_failed", thumbsSendFailed)
                        put("thumbs_gated", thumbsGated)
                        put("thumbs_rejected", thumbsRejected)
                        put("session_confirmed", sessionConfirmed)
                        // 交付收尾（"停采集 ≠ 停交付"的效果要看得到）：
                        //   drain_state  drained=已传完 / draining=正在传 / blocked=传不动 / idle=没活
                        //   delivery_alive  交付道线程是否仍在工作（与 connected 无关）
                        put("drain_state", drainState())
                        put("delivery_alive", deliveryAlive)
                        put("uploaded_frames", uploadedBySession[sessionId] ?: 0)
                        put("upload_fail_streak", uploadFailStreak)
                        put("delivery_done_announced", deliveryDoneAnnounced)
                        put("sample_fps", String.format("%.2f", sampleFps()).toDouble())
                        put("deliver_fps", String.format("%.2f", deliveredRateFps()).toDouble())
                        put("samples_lost", samplesLost)
                        put("meta_missing_result", metaMissingResult)
                        put("plan_interval_ms", plan.optInt("interval_ms", 0))
                        put("active_physical_id", lensId)
                        put("lens_changes", lensChanges)
                        put("zoom_choice", zoomChoice)
                        put("zoom_effective", reqZoom)
                        put("zoom_source", zoomSource)
                        // 留存账本（**每一帧都留**）——与 `frames_seen` 一起看：
                        // 若 `frames_expected` 明显小于 `frames_seen`，说明**写盘把回调拖慢了
                        // 或帧在进入留存器之前就丢了**（这是"默认不再抽帧"后最该盯的一组数）。
                        put("capture_mode", captureMode)
                        put("store", storeStatusJson())
                        put("live", liveStatusJson())
                        put("frames_seen", framesSeen)
                    }
                    onStatus(st)
                    // 采集率 > 交付率必须**出声**：后果是队列无界增长直到写满磁盘（采集静默失败）。
                    if (running.get() && growFps > 0.5 && nowMs - lastBacklogWarnMs > 60_000) {
                        lastBacklogWarnMs = nowMs
                        log(String.format(
                            "⚠ 积压：采集快于交付（+%.1f 帧/s；队列 %d 帧 %.0f MB；交付 %.2f MB/s）。" +
                            "按此速度约 %.0f 分钟后写满剩余空间 ⇒ 降低抽帧率 / 换 5 GHz 或网线 / 减小输出尺寸",
                            growFps, n, qBytes / 1048576.0, uploadRateMbps(),
                            if (etaMin > 0) etaMin else 0.0))
                    }
                    ws?.send(st.toString())
                } catch (e: Exception) { }
                Thread.sleep(STATUS_INTERVAL_MS)
            }
            statusThread = null
        }.also { it.isDaemon = true; it.start() }

        // ⚠ 判据必须是 **isAlive**，不是"引用非空"：收尾模式下线程会正常退出
        // （`deliveryAlive=false`），而引用还留着 ⇒ 重连后 `uploader == null` 不成立、
        // **不会重建新线程**，上传从此静默失效（上一轮实测踩到：三次重连后
        // 一帧都没传出去，界面还看不出异常）。这条坑换个形状又回来了一次。
        val u = uploader
        if (u == null || !u.isAlive) {
            uploader = Uploader().also { it.isDaemon = true; it.start() }
        }
    }

    /**
     * 上传线程：扫描本地队列 → init/分片/complete → **complete 说 verified 才删本地**。
     *
     * 关于"ACK 才删"（§5.6）的落地口径：删除依据是**与该文件绑定的 HTTP complete 响应**
     * （`file_id` 一一对应、携带同一份 `verified` 判定）。WS 的 `ack` 广播是**面板副本**。
     * 若硬性要求"必须收到 WS ack 才删"，WS 一断队列就会无限增长 —— 那正是 §5.6 要避免的。
     */
    private inner class Uploader : Thread() {
        private val kickFlag = AtomicBoolean(false)

        fun kick() { kickFlag.set(true); synchronized(this) { (this as Object).notifyAll() } }

        override fun run() {
            // ⚠ 循环条件曾经是 `connected` —— 那等于把交付道的命脉交给控制道：
            // WS 一断，队列里已抽好的帧就停在手机上不动（HTTP 明明还通）。
            // 现在改为 `deliveryAlive`：**只有用户显式断开且队列已清空**才收工。
            while (deliveryAlive) {
                try {
                    val dir = queueDir
                    val items = (dir.listFiles { f -> f.name.endsWith(".yuv") } ?: emptyArray())
                        .sortedBy { it.name }
                    if (items.isEmpty()) {
                        // 队列空了 ⇒ 若"采集已停 + 本轮传过帧"，这就是**收尾完成**，
                        // 由交付道自己宣告（PC 侧无法从"没有新帧"推断出这件事）。
                        maybeAnnounceDeliveryDone()
                        synchronized(this) { (this as Object).wait(1500) }
                        continue
                    }
                    for (yf in items) {
                        if (!deliveryAlive) break
                        val name = yf.nameWithoutExtension
                        val jf = File(dir, "$name.json")
                        // 元数据还没落盘 ⇒ **等下一轮**，绝不删。
                        // （旧实现这里 `yf.delete()`：一次竞态就永久丢一帧，而且是静默的。）
                        if (!jf.exists() || yf.name.endsWith(".part")) continue
                        val frameSession = metaSessionId(jf)
                        if (uploadOne(name, yf, jf)) {
                            uploadFailStreak = 0
                            yf.delete(); jf.delete()
                            // 按**会话键**累计已回传帧数：收尾宣告要用它，而队列里
                            // 常常混着旧会话的积压帧（那些不能算进本会话）。
                            uploadedBySession[frameSession] =
                                (uploadedBySession[frameSession] ?: 0) + 1
                            // 只有**本会话**的帧 verified 才能开闸。
                            // ⚠ 之前的实现是"任何一帧 verified 就开闸"，而队列里常有**旧会话**
                            // 的积压帧（实测：连接后 16 s 内补传的全是上一轮的帧）⇒ 闸门被旧会话
                            // 的 verified 打开，而当前会话在 PC 端还不存在，缩略图照样被拒
                            // （现象：`thumbs_gated: 0` 但 PC 打印"未知会话"）。
                            if (frameSession == sessionId && !sessionConfirmed) {
                                sessionConfirmed = true
                                log("交付道已确认本会话（首帧 verified）⇒ 监视道开始发送缩略图")
                            }
                            log("已交付并删除本地 $name" +
                                "（$frameSession 累计 ${uploadedBySession[frameSession]} 帧）")
                        } else {
                            // 失败只**保留文件并退避**，绝不删。连续失败要出声：
                            // 否则"交付道堵住了"在界面上与"传完了"长得一样。
                            uploadFailStreak++
                            if (uploadFailStreak == 1 || uploadFailStreak % 10 == 0) {
                                log("⚠ 交付受阻：连续 $uploadFailStreak 次未完成" +
                                    "（HTTP 不通或 SHA 不符）；**文件一律保留**，恢复后自动续传")
                            }
                            synchronized(this) { (this as Object).wait(3000) }   // 失败退避
                        }
                    }
                } catch (e: InterruptedException) {
                    return
                } catch (e: Exception) {
                    log("上传线程异常: ${e.message}")
                    try { Thread.sleep(2000) } catch (ie: InterruptedException) { return }
                }
            }
        }

        /** @return true 表示服务端已 verified（可以删本地文件） */
        private fun uploadOne(name: String, yf: File, jf: File): Boolean {
            return try {
                val meta = JSONObject(jf.readText())
                val bytes = yf.readBytes()
                val sha = sha256(bytes)
                // **用帧自己记录的 session_id**（不是当前会话）：这样跨会话补传能正确归位
                // 到它原本的会话里，而不是把旧帧塞进新会话（那会污染 manifest 的断言）。
                val frameSession = meta.optString("session_id").ifBlank { sessionId }
                val frameSeq = meta.optInt("seq", -1)
                val frameName = String.format("frame_%06d.yuv", frameSeq)
                val init = postJson("$httpBase/api/v1/files/init", JSONObject().apply {
                    put("session_id", frameSession)
                    put("device_serial", Build.MODEL + "-" + Build.ID)
                    put("model", Build.MODEL)
                    put("app_version", APP_VERSION)
                    put("calib_id", meta.optString("calib_id"))
                    put("delivery", DELIVERY)
                    put("capture_path_id", PATH_ID)
                    put("capture_path_rev", PATH_REV)
                    put("seq", frameSeq)
                    put("name", frameName)
                    put("size", bytes.size)
                    put("sha256", sha)
                    put("meta", meta)
                })
                val fid = init.optString("file_id")
                if (fid.isEmpty()) { log("init 被拒: $init"); return false }
                var off = init.optInt("resume_offset", 0)
                while (off < bytes.size) {
                    val end = minOf(off + CHUNK, bytes.size)
                    val part = bytes.copyOfRange(off, end)
                    val sent = end - off
                    val (code, body) = putChunk(fid, off, end - 1, bytes.size, part)
                    if (code == 200) {
                        noteUploaded(sent)               // 只统计真正被服务端收下的字节
                        off = body.optInt("received", end)
                    } else if (code == 409 || code == 412) {
                        off = body.optInt("resume_offset", 0)   // 服务端告诉我们真实偏移
                        log("服务端要求从 $off 续传（$code）")
                    } else {
                        log("分片失败 $code: ${body.optString("message", body.toString())}")
                        return false
                    }
                }
                val done = postJson("$httpBase/api/v1/files/$fid/complete",
                    JSONObject().put("sha256", sha))
                val ok = done.optString("state") == "verified"
                if (!ok) log("complete 未通过: $done")
                ok
            } catch (e: Exception) {
                log("上传异常 $name: ${e.message}")
                false
            }
        }

    }

    // ---------------------------------------------------------------- HTTP 辅助
    //
    // ⚠ 这四个函数原本是 `Uploader` 的内部私有方法。归档道回传阶段（Archiver）
    // 需要**完全相同**的语义（分片 + 偏移纠偏 + CRC + SHA-256），所以把它们
    // 提到外层类共用 —— 复制一份的风险不是"多写 30 行"，而是**两份实现会分叉**：
    // 一旦只在其中一份里修了偏移纠偏，另一条道就会静默丢数据。
    // 纯搬移，调用点与行为都没变。

    private fun postJson(url: String, body: JSONObject): JSONObject {
        val c = (URL(url).openConnection() as HttpURLConnection)
        c.requestMethod = "POST"
        c.doOutput = true
        c.connectTimeout = 8000
        c.readTimeout = 20000
        c.setRequestProperty("Content-Type", "application/json")
        c.outputStream.use { it.write(body.toString().toByteArray()) }
        return readJson(c)
    }

    private fun putChunk(fid: String, start: Int, end: Int, total: Int, data: ByteArray):
        Pair<Int, JSONObject> {
        val c = (URL("$httpBase/api/v1/files/$fid").openConnection() as HttpURLConnection)
        c.requestMethod = "PUT"
        c.doOutput = true
        c.connectTimeout = 8000
        c.readTimeout = 30000
        c.setFixedLengthStreamingMode(data.size)
        c.setRequestProperty("Content-Range", "bytes $start-$end/$total")
        val crc = CRC32().apply { update(data) }
        c.setRequestProperty("X-Chunk-Crc32", String.format("%08x", crc.value))
        c.outputStream.use { it.write(data) }
        return c.responseCode to readJson(c)
    }

    private fun readJson(c: HttpURLConnection): JSONObject {
        val code = c.responseCode
        val stream: InputStream = if (code in 200..299) c.inputStream else (c.errorStream ?: c.inputStream)
        val text = stream.bufferedReader().use(BufferedReader::readText)
        return try { JSONObject(text) } catch (e: Exception) { JSONObject().put("raw", text) }
    }

    private fun sha256(b: ByteArray): String {
        val d = MessageDigest.getInstance("SHA-256").digest(b)
        return d.joinToString("") { String.format("%02x", it) }
    }

    /**
     * 交付收尾宣告（§7.1 `delivery_done`）。
     *
     * **为什么必须由 App 宣告、不能让 PC 推断**：PC 只看得见"帧不再来了"，
     * 而"不再来"有太多原因（链路卡顿、App 被系统冻结、旧会话串台）——
     * 拿它当"传完了"就是让判据强于事实。收尾这件事只有**队列的持有者**知道。
     *
     * 三条**同时**成立才宣告，少任何一条，宣告都是过度结论：
     *  ① 采集已停（`!running`）—— 否则"队列空"只是暂时的，马上又会有新帧；
     *  ② 本地队列已空（`queueCount() == 0`）—— 没有待传的文件；
     *  ③ 本轮**至少成功交付过 1 帧** —— 否则空跑也会"宣告传完了"（真空通过）。
     */
    private fun maybeAnnounceDeliveryDone() {
        // ⚠ `live` 模式**没有"交付收尾"这个概念**：没有队列、没有留存。
        // 在这里发 `delivery_done` 会让 PC 把 `delivery_complete` 从 `null` 改成 `true`
        // —— 那是在宣称"全部收全了"，而本模式的正确答案是**"不适用"**。
        if (captureMode == MODE_LIVE) return
        if (deliveryDoneAnnounced) return
        if (running.get()) return
        if (queueCount() > 0) return
        val uploaded = uploadedBySession[sessionId] ?: 0
        if (uploaded <= 0) return
        val w = ws
        if (w == null || !connected) {
            // 控制道不通 ⇒ 宣告发不出去。**不假装发过、也不记成"已宣告"**：
            // 保持未宣告，等重连后（队列仍空、采集仍停）下一轮就会补发。
            log("交付已收尾（队列空、$uploaded 帧 verified），但控制道未连接 ⇒ 收尾宣告待重连补发")
            return
        }
        val drainedMs = System.currentTimeMillis() -
            (if (stoppedAtMs > 0) stoppedAtMs else System.currentTimeMillis())
        // 封口（把"实存"定下来；`stopCapture` 已调过则这里是空操作），再把**账本整体**发出去。
        // ⚠ 以前这里拿全局计数器 `deliveredCount` 当"抽帧数"用 —— 那是**全局**的，
        //   会把**别的会话的补传**算进本会话（跨会话队列只有一个）⇒ 现在按 session_id 记。
        finishStoreRecording()
        val stored = storeStored
        try {
            w.send(JSONObject().apply {
                put("type", "delivery_done")
                put("session_id", sessionId)
                put("frames", uploaded)                 // PC 能核对的：本会话已验证回传的帧数
                put("frames_expected", storeExpected)   // 账本分母
                put("frames_stored_on_device", stored)  // 本地实存
                put("gaps_on_device", storeGaps)        // **只能重采**
                put("gap_ts", storeGapTsJson())
                put("seq_first", storeSeqFirst)
                put("seq_last", storeSeqLast)
                put("warmup_frames_skipped", storeWarmupSkipped)
                put("result_missing", storeResultMissing)
                put("bytes", storeBytes)
                put("queue_len", queueCount())
                put("drain_seconds", Math.max(0, drainedMs / 1000))
                put("at_ms", System.currentTimeMillis())
            }.toString())
            deliveryDoneAnnounced = true
            storeState = "done"
            log("✓ 已宣告交付收尾：应有 $storeExpected / 本地实存 $stored / PC 已校验 $uploaded"
                + "（采集缺口 $storeGaps 帧，回传缺口 ${(stored - uploaded).coerceAtLeast(0)} 帧）"
                + "；收尾耗时 ${drainedMs / 1000} s")
        } catch (e: Exception) {
            log("交付收尾宣告发送失败: ${e.message}")
        }
    }

    /** 交付道状态机（进 status，让 PC/界面一眼看出"卡在哪"）。
     *
     *  ⚠ `drained` 与 `idle` 必须分开：前者是"**已经传完了**"，
     *  后者是"本来就没活"。合成一个值，用户就分不清"全部回传完成"与"什么都没发生"
     *  —— 这正是用户这一轮要看到的那句话。
     */
    private fun drainState(): String {
        val q = queueCount()
        return when {
            q > 0 && uploadFailStreak > 0 -> "blocked"     // 有积压且传不动（HTTP 不通 / SHA 不符）
            q > 0 -> "draining"                            // 有积压且正在传
            !running.get() && (uploadedBySession[sessionId] ?: 0) > 0 -> "drained"
            else -> "idle"
        }
    }

    // ================================================== 本地留存账本（**每一帧都留**）
    //
    // 第 24 轮把两条道**合并成一条**。用户口径："我只想要确保实际记录的内容能够原封不动
    // 地传到电脑上" + "每一帧都留。交付道的目标就是这个，把归档和交付两个合并"。
    //
    // 于是"抽帧"这件事本身被取消：**sensor 输出多少帧，就留多少帧**。
    // 为什么以前会分成两条道、为什么现在能合：
    //   · 交付道早就"先落手机本地（`delivery/queue/*.yuv`）→ HTTP 分片上传"；
    //     归档道那套 writer / 队列 / 上传是它的**复制品**（只多了"不抽帧"和"配额熔断"）；
    //   · 合并后只剩一条道：不抽帧 → 先落本地 → 边采边传 → 传不完的停采后排空。
    //
    // ⚠ 合并**不是**把两条道叠起来：`kind` 字段、"归档目录"、"归档表"全部删除，PC 侧
    //   只留一张 `s.frames`。但历史教训在这里仍然有效（第 23 轮）：将来**再出现两条共用
    //   seq 空间的流**时，幂等键必须把"道的名字"加回去，否则一定会静默覆盖。
    //
    // ⚠ **三条硬约束**（都是算出来的，不是拍的）：
    //   ① 全帧率 = 30.04 fps × 2 764 800 B ≈ **83.0 MB/s**，而手机→PC 实测链路只有
    //      **5.5 MB/s**（差 15.1 倍）⇒ 采集期**不可能**边采边传完，必须先落本地；
    //   ② 21 GB 可用空间按 83.0 MB/s 算约 **4.2 分钟**写满，而写满会把 **App 自己**
    //      （元数据、队列、日志、还有回传）一起拖死 ⇒ 必须有**预算 + 余量 + 熔断**；
    //   ③ 熔断**必须显式命名**（`aborted_no_space`）—— 静默停止会让人以为还在录，
    //      而"以为还在录"的代价是**整段数据作废**。
    //
    // ⚠ **采集行为影响（逐项）**：不新增 ImageReader、不改 CaptureRequest、不改锁定配方、
    //   不改输出格式与尺寸、不改丢弃规则（仍是跳过前 `WARMUP_FRAMES` 帧）⇒ 请求层无变化。
    //   但**默认不再抽帧**是**事实层**的巨大变化（5 fps → 30 fps、写盘量 ×6）⇒
    //   必须开/关 A/B 实测；`frames_seen` 与 `storeExpected` 两个数就是为这件事准备的。
    //
    // ⚠ **两个缺口必须分开命名**（现场处置完全不同）：
    //   · `gaps_on_device`：采集期**确实没写进本地**（写队列满 / 图缺席 / 写盘失败）
    //     ⇒ **只能重采**，重传多少次都没用；
    //   · `upload_gaps`：写进了本地但**没传到 PC**（由 PC 自己数，不采信 App 自报）
    //     ⇒ **可以重传**。
    //   把两者合成一个数，现场**必然选错处置**。

    /** 账本状态：`idle` → `recording` → `uploading` → `done` / `aborted_no_space`。 */
    @Volatile private var storeState = "idle"
    /** 账本分母：**已决定留存**的帧数（= 通过预热、且通过抽帧判定）。 */
    @Volatile private var storeExpected = 0
    /** 本会话**已写进本地**的帧数（在写线程里按 session_id 记，不含别的会话的补传）。 */
    @Volatile private var storeStored = 0
    @Volatile private var storeWarmupSkipped = 0
    /** 应留存但确实没留的帧数（**只能重采**）。 */
    @Volatile private var storeGaps = 0
    /** 结果对象缺席的帧数（自描述 + 计数：下游不能把"没有曝光值"当成"曝光值就是这个"）。 */
    @Volatile private var storeResultMissing = 0
    @Volatile private var storeBytes = 0L
    @Volatile private var storeWriteMbps = 0.0
    @Volatile private var storeAbortedReason = ""
    /** 本会话 seq 区间。seq 由 `nextSeq()` 逐帧分配，**写失败的帧也占了号** ⇒
     *  区间里的洞就是缺口（PC 侧据此点出具体缺了哪些帧）。 */
    @Volatile private var storeSeqFirst = -1
    @Volatile private var storeSeqLast = -1
    /** 缺口位置按 **sensor timestamp** 记，不按 seq：被丢的帧若连元数据都没组出来就没有 seq。 */
    private val storeGapTs = java.util.Collections.synchronizedList(java.util.ArrayList<Long>())
    /** 逐帧的 `CaptureResult` 派生元数据缓存（只有小 JSON，不含像素）。 */
    private val storeMetaCache = java.util.LinkedHashMap<Long, JSONObject>()

    private val storeRoot: File
        get() = File(ctx.getExternalFilesDir(null) ?: ctx.filesDir, "delivery")

    /**
     * 当前抽帧间隔（ns）。**0 = 不抽帧 = "每一帧都留"**（默认）。
     *
     * `plan.interval_ms`（PC 下发的 plan，现场可调）优先；否则看 `profile.deliver_fps`。
     * 两个都是**降载旋钮**：全帧率写不动时用它把负载降下来 —— 但账本里的 `every_frame`
     * 会因此变 false，**语义变化必须可见**（改默认值/语义都要能被一眼看出来）。
     */
    private fun currentIntervalNs(): Long {
        val planMs = plan.optInt("interval_ms", 0)
        if (planMs > 0) return planMs * 1_000_000L
        val fps = profile.optDouble("deliver_fps", 0.0)
        return if (fps > 0) (1000.0 / fps * 1e6).toLong() else 0L
    }

    /** 留存预算与余量。默认：预算 = 剩余空间 50%，余量 2 GB（`store_*_gb` 可覆盖）。 */
    private fun storeBudgetBytes(): Long {
        val budgetGb = profile.optDouble("store_budget_gb", 0.0)
        val free = storeRoot.let { if (it.exists() || it.mkdirs()) it.usableSpace else 0L }
        val byRatio = (free * 0.5).toLong()
        return if (budgetGb > 0) minOf((budgetGb * 1024 * 1024 * 1024).toLong(), byRatio)
               else byRatio
    }

    private fun storeReserveBytes(): Long =
        (profile.optDouble("store_reserve_gb", 2.0) * 1024 * 1024 * 1024).toLong()

    /**
     * 记一次"应留存但确实没留"的缺口。
     *
     * **为什么必须记**：这些帧**只能重采**（重传再多次也没用），而在第 24 轮以前它是
     * **完全静默**的 —— 见 [enqueue] 的注释（写队列满 ⇒ 帧消失，App 与 PC 三方一致地
     * "少一帧"，谁都发现不了）。这与"确保实际记录的内容原封不动传到电脑"直接冲突，
     * 所以它是本轮**第一优先**修的缺陷。
     *
     * @param ts sensor 时间戳；**0 表示连时间戳都取不到**（只计数、不点位置）
     */
    private fun noteStoreGap(ts: Long, why: String) {
        storeGaps++
        if (ts > 0L) {
            synchronized(storeGapTs) {
                if (storeGapTs.size < STORE_GAP_KEEP) storeGapTs.add(ts)
            }
        }
        if (storeGaps == 1 || storeGaps % 50 == 0) {
            log("⚠ 留存缺口：已 $storeGaps 帧**确实没写进本地**（最近原因 $why）"
                + " —— 这些帧**只能重采**；降帧率 / 减小输出尺寸可缓解")
        }
    }

    private fun storeGapTsJson(): org.json.JSONArray {
        val a = org.json.JSONArray()
        synchronized(storeGapTs) { for (t in storeGapTs) a.put(t) }
        return a
    }

    /**
     * 熔断：**显式命名状态 + 明说原因**，绝不静默停止。
     *
     * 停的只是**写盘**（保住剩下的空间），**上传线程继续** —— 已经存下来的帧还要传完。
     * 这条很重要：熔断是为了保住"已经拿到的数据"，而不是把未完成的一起丢掉。
     */
    private fun abortStore(reason: String, message: String) {
        if (storeState.startsWith("aborted")) return
        storeState = if (reason == "no_space") "aborted_no_space" else "aborted"
        storeAbortedReason = reason
        log("⚠ 留存中止（$reason）：$message")
        writer?.interrupt()
        writer = null
        val w = ws
        if (w != null && connected) {
            try {
                w.send(JSONObject().apply {
                    put("type", "delivery_aborted")
                    put("session_id", sessionId)
                    put("reason", reason)
                    put("message", message)
                    put("frames_expected", storeExpected)
                    put("frames_stored_on_device", storeStored)
                    put("gaps_on_device", storeGaps)
                    put("gap_ts", storeGapTsJson())
                    put("bytes", storeBytes)
                    put("at_ms", System.currentTimeMillis())
                }.toString())
            } catch (e: Exception) {
                log("delivery_aborted 发送失败: ${e.message}")
            }
        }
    }

    /** 采集开始：重置账本 + 打印预算。写线程与上传线程由既有路径启动（不重复造机器）。 */
    private fun startStore() {
        storeState = "recording"
        storeExpected = 0
        storeStored = 0
        storeWarmupSkipped = 0
        storeGaps = 0
        storeResultMissing = 0
        storeBytes = 0L
        storeWriteMbps = 0.0
        storeAbortedReason = ""
        storeSeqFirst = -1
        storeSeqLast = -1
        storeGapTs.clear()
        storeMetaCache.clear()
        val budget = storeBudgetBytes()
        val reserve = storeReserveBytes()
        val perFrame = 1920L * 1440L
        val budgetFrames = if (perFrame > 0) budget / perFrame else 0L
        val interval = currentIntervalNs()
        log(if (interval > 0)
                "留存：**降载模式**（抽帧间隔 ${interval / 1_000_000} ms）" +
                    " —— ⚠ 已不再是\"每一帧都留\""
            else "留存：**每一帧都留**（不抽帧、不转码；先写本地，再边采边传）")
        log("  预算 ${budget / 1024 / 1024} MB ≈ $budgetFrames 帧；余量保护 ${reserve / 1024 / 1024} MB")
        log("  ⚠ 全帧率约 83 MB/s ⇒ 21 GB 约 4 分钟；写满会拖死 App，触线自动停留存并明说原因")
    }

    /** 采集停止时封口：记录"应有 / 实存 / 缺口"，但**不删本地**（还没回传完）。 */
    private fun finishStoreRecording() {
        if (storeState != "recording") return
        storeState = "uploading"
        // 等写线程把队列里的最后几帧落完（否则"实存"会少算，看起来像丢了帧）
        val deadline = SystemClock.elapsedRealtime() + 8000
        while (writeQueue.isNotEmpty() && SystemClock.elapsedRealtime() < deadline) {
            try { Thread.sleep(120) } catch (e: InterruptedException) { break }
        }
        log("留存封口：应有 $storeExpected 帧 / 本地实存 $storeStored 帧 / 缺口 $storeGaps 帧"
            + " / ${storeBytes / 1024 / 1024} MB / ${"%.2f".format(storeWriteMbps)} MB/s"
            + (if (storeGaps > 0) "  ⚠ 缺口**只能重采**（不是传输问题）" else ""))
    }

    /**
     * 留存账本（进 `status`；界面、PC 面板、`host/status` 读的是**同一份**）。
     *
     * ⚠ `upload_gaps` **不在 App 侧算**：那是"没传到 PC"的缺口，必须由 **PC 自己数**
     * （App 自报"我传完了"没有证明力）。这里只给 App 自己知道的那一半。
     */
    private fun storeStatusJson(): JSONObject {
        if (captureMode == MODE_LIVE) {
            // ⚠ **"不适用" 与 "判不了" 与 "0 帧" 是三件事**，这里必须把第一种说清楚：
            //   · 0 帧   = 有留存机制但一帧都没写成（**缺陷**）；
            //   · null   = 有机制、但缺证据判不了（**取证不足**）；
            //   · 不适用 = 本模式里根本没有留存这个概念（**设计**）。
            // PC 侧据此把 `store.applicable` 置 false、`delivery_complete` 判 null，
            // 但那两个 null 的含义与"判不了"不同 —— 靠 `mode` 字段区分。
            return JSONObject().apply {
                put("applicable", false)
                put("mode", MODE_LIVE)
                put("state", "not_applicable")
                put("note", "live 模式：设备零留存（不落盘、不停采排空、不回传）—— 不是「丢帧」")
            }
        }
        return JSONObject().apply {
            put("applicable", true)
            put("mode", captureMode)
            put("state", storeState)
            put("every_frame", currentIntervalNs() <= 0)
            put("interval_ms", currentIntervalNs() / 1_000_000)
            put("frames_expected", storeExpected)
            put("frames_stored_on_device", storeStored)
            put("gaps_on_device", storeGaps)          // **只能重采**
            put("gap_ts", storeGapTsJson())
            put("seq_first", storeSeqFirst)
            put("seq_last", storeSeqLast)
            put("bytes", storeBytes)
            put("write_mbps", storeWriteMbps)
            put("result_missing", storeResultMissing)
            put("warmup_frames_skipped", storeWarmupSkipped)
            put("aborted_reason", storeAbortedReason)
            put("free_mb", storeRoot.let { if (it.exists()) it.usableSpace / 1024 / 1024 else -1 })
            // ⚠「还能录多久」按**本会话实测写入速率**算，不按理论 83 MB/s —— 实测速率才是这台设备的事实
            val rate = if (storeWriteMbps > 0.01) storeWriteMbps else 0.0
            put("eta_full_min", if (rate > 0) {
                val free = storeRoot.let { if (it.exists()) it.usableSpace else 0L }
                ((free / 1024.0 / 1024.0) / rate / 60.0)
            } else -1.0)
        }
    }

}

/**
 * 极简 WebSocket 客户端（RFC 6455 客户端侧）。
 *
 * 为什么不用 OkHttp 等库：本工程是**无依赖、离线可构建**的探针工程
 * （`gradle assembleDebug` 不需要联网拉依赖，见 §7 构建环境）。为一个控制道
 * 引入网络依赖，会把"离线可重建"这个已经验证过的性质搭进去。
 * 只用到文本帧 + ping/pong，够 §7.1 的消息集。
 */
class WsClient(
    private val host: String,
    private val port: Int,
    private val token: String,
    private val onMessage: (String) -> Unit,
    /** 握手成功（HTTP 101）时回调。**没有它，调用方只能"猜"连上没有** —— 实测踩到。 */
    private val onOpen: () -> Unit = {},
    /** 连接**意外**结束时回调（对端关闭/EOF/读写异常）。用户主动 close() 不触发。 */
    private val onClosed: (String) -> Unit = {},
) {
    private var socket: Socket? = null
    private var out: OutputStream? = null
    private val lock = Any()
    @Volatile private var alive = false
    @Volatile private var handshakeOk = false
    @Volatile private var userClosed = false
    private var closeReason = "对端关闭了连接"

    fun start() {
        Thread {
            try {
                val s = Socket()
                s.connect(InetSocketAddress(host, port), 8000)
                socket = s
                out = s.getOutputStream()
                val key = android.util.Base64.encodeToString(
                    ByteArray(16).also { java.util.Random().nextBytes(it) },
                    android.util.Base64.NO_WRAP)
                val req = "GET /ws?token=$token HTTP/1.1\r\n" +
                    "Host: $host:$port\r\n" +
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n" +
                    "Sec-WebSocket-Key: $key\r\nSec-WebSocket-Version: 13\r\n\r\n"
                out!!.write(req.toByteArray()); out!!.flush()
                val input = s.getInputStream()
                val head = readHeaders(input)
                if (!head.startsWith("HTTP/1.1 101")) {
                    // 403 = token 不符；这条最容易现场踩到，直接说清楚
                    onMessageCheck("""{"type":"error","code":"ws_handshake","message":"$head"}""")
                    return@Thread
                }
                alive = true
                handshakeOk = true
                try { onOpen() } catch (e: Exception) { }
                // 帧循环
                while (alive) {
                    val f = readFrame(input)
                    if (f == null) { closeReason = "对端关闭了连接（读到 EOF）"; break }
                    when (f.opcode) {
                        1 -> onMessageCheck(String(f.payload, Charsets.UTF_8))
                        9 -> sendFrame(10, f.payload)          // ping → pong
                        8 -> { closeReason = "对端发来 close 帧"; break }
                    }
                }
            } catch (e: Exception) {
                closeReason = "读写异常：${e.javaClass.simpleName}: ${e.message}"
                if (alive) onMessageCheck(
                    """{"type":"error","code":"ws_io","message":"${e.message}"}""")
            } finally {
                // 只在"握手成功过 + 不是用户主动关"时通知：否则用户点「断开」
                // 也会弹一次"连接已断开"，把正常操作说成故障。
                val lost = handshakeOk && !userClosed
                alive = false
                if (lost) try { onClosed(closeReason) } catch (e: Exception) { }
            }
        }.also { it.isDaemon = true; it.start() }
    }

    fun send(text: String) {
        if (!alive) return
        try { sendFrame(1, text.toByteArray(Charsets.UTF_8)) } catch (e: Exception) { }
    }

    fun close() {
        userClosed = true
        alive = false
        try { socket?.close() } catch (e: Exception) { }
    }

    private fun onMessageCheck(m: String) = try { onMessage(m) } catch (e: Exception) { }

    private fun readHeaders(input: InputStream): String {
        val sb = StringBuilder()
        var last4 = ""
        while (true) {
            val b = input.read()
            if (b < 0) break
            sb.append(b.toChar())
            last4 = (last4 + b.toChar()).takeLast(4)
            if (last4 == "\r\n\r\n") break
            if (sb.length > 8192) break
        }
        return sb.toString().lines().firstOrNull() ?: ""
    }

    private class Frame(val opcode: Int, val payload: ByteArray)

    private fun readFrame(input: InputStream): Frame? {
        val b0 = input.read(); if (b0 < 0) return null
        val b1 = input.read(); if (b1 < 0) return null
        val opcode = b0 and 0x0F
        var len = (b1 and 0x7F).toLong()
        if (len == 126L) len = ((input.read() shl 8) or input.read()).toLong()
        else if (len == 127L) {
            len = 0
            for (i in 0 until 8) len = (len shl 8) or input.read().toLong()
        }
        val payload = ByteArray(len.toInt())
        var read = 0
        while (read < payload.size) {
            val n = input.read(payload, read, payload.size - read)
            if (n < 0) return null
            read += n
        }
        return Frame(opcode, payload)
    }

    private fun sendFrame(opcode: Int, payload: ByteArray) {
        synchronized(lock) {
            val o = out ?: return
            val b = java.io.ByteArrayOutputStream()
            b.write(0x80 or opcode)
            val maskBit = 0x80
            when {
                payload.size < 126 -> b.write(maskBit or payload.size)
                payload.size < 65536 -> {
                    b.write(maskBit or 126); b.write((payload.size shr 8) and 0xFF)
                    b.write(payload.size and 0xFF)
                }
                else -> {
                    b.write(maskBit or 127)
                    for (i in 7 downTo 0) b.write(((payload.size.toLong() shr (8 * i)) and 0xFF).toInt())
                }
            }
            val mask = ByteArray(4).also { java.util.Random().nextBytes(it) }
            b.write(mask)
            for (i in payload.indices) b.write((payload[i].toInt() xor mask[i % 4].toInt()) and 0xFF)
            o.write(b.toByteArray()); o.flush()
        }
    }
}

package com.mouse.phoneprobe

import android.annotation.SuppressLint
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
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
import android.media.ExifInterface
import android.media.ImageReader
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Range
import android.util.Size
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayInputStream
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * 动态锁定验证：把相机参数锁死后采集 N 帧，逐帧采集 CaptureResult。
 *
 * 要回答四个问题：
 *  1. 参数到底有没有锁住（每个字段的不同取值个数）
 *  2. 帧间隔是否等于请求值、抖动有多大（SENSOR_TIMESTAMP 差分）
 *  3. 请求值与 HAL 回读值差多少（HAL 对 focus/frame_duration 会量化）
 *  4. 镜头机械状态是否已收敛（预热帧之后不得再出现 MOVING）
 *
 * **v1.8 起支持两种采集模式（这是两条不同的采集路径，证据不能互相外推）：**
 *  - `singleShot = false`（②③④⑤）：`setRepeatingRequest` 连续流，测节奏与稳定性
 *  - `singleShot = true`（⑥）：`session.capture()` 一次一张，**这才是"拍照"**
 *
 * 为什么必须分开测：题面/交付要求是"回传使用拍照"，而全部既有证据都来自
 * 重复请求的连续流。模板、格式、调度方式三样都不同，其中任何一样都可能改变
 * 时间戳的语义（例如 `TEMPLATE_STILL_CAPTURE` 默认可能触发零快门延迟/多帧合成，
 * 此时"这张照片的时刻"就不再是单值）。**集成路径变了，就等于证据基础变了。**
 *
 * 三个已踩过的坑记在这里，改动前先读：
 *  - AE 关闭后 CONTROL_AE_TARGET_FPS_RANGE 会被 HAL 忽略，帧间隔只由
 *    SENSOR_FRAME_DURATION 决定。两者都下发，用回读值判定哪个真生效。
 *  - LENS_INFO_HYPERFOCAL_DISTANCE 是"泛焦"选择，近距目标会严重离焦。
 *    默认按工作距离算 diopter，超焦距只作兜底。
 *  - session 配置后前若干帧镜头仍在机械收敛（LENS_STATE=MOVING），
 *    这些帧几何不可信，统计必须排除，交付数据也必须丢弃。
 */
class LockedCaptureProbe(
    private val context: Context,
    private val cameraId: String,
    /** 工作距离（米）。>0 时按该距离设 LENS_FOCUS_DISTANCE；<=0 时退回超焦距泛焦。 */
    private val focusDistanceM: Float = 0f,
    /** 目标帧率。AE 关闭时由 SENSOR_FRAME_DURATION 决定实际帧间隔。 */
    private val targetFps: Int = 30,
    /** 采集总帧数（含预热帧）。singleShot 模式下即"要拍几张"。 */
    private val targetFrames: Int = 150,
    /** 预热帧数：session 起始的机械收敛期，几何不可信，统计时排除。 */
    private val warmupFrames: Int = 5,
    /**
     * 是否额外下发 CONTROL_AE_TARGET_FPS_RANGE（取第一个 min==max 的档位）。
     *
     * 首轮实测：AE 关闭时它被 HAL 忽略，且与 SENSOR_FRAME_DURATION 同时下发时
     * 帧时长也未被采纳（回读 33287995 与我们请求的 33333333 不同）。
     * 因此无法排除"两者冲突导致双双失效"的可能。帧率权威性测验要分别试这两种下发方式。
     */
    private val legacyFpsRange: Boolean = false,
    /** 报告类型标记，供帧率权威性测验的多轮报告区分。 */
    private val reportKind: String = "m0_locked_capture",
    /**
     * 强制采集尺寸（null = 按纵横比+面积上限自动选）。
     *
     * 存在的理由：输出尺寸是**内参的一部分**，而自动选档依赖一个人为的面积上限，
     * 那个上限本身没有实测依据。要回答"该定在哪一档"，只能把候选尺寸逐个跑一遍，
     * 量出各档的实际可达帧率与卷帘读出时间 —— 这就是⑤尺寸扫描。
     */
    private val forcedCaptureSize: Size? = null,
    /**
     * 强制 crop = 整个活动阵列（跳过 `aspectSafeCrop` 的纵横比收紧）。
     *
     * 存在的理由：**判别 skew 的归属**需要一个"一次只动一个变量"的受控实验。
     * 已有数据里"输出纵横比"与"crop 高度"始终一起变（16:9 ⇒ aspectSafeCrop 会把
     * crop 从 3072 收紧到 2304），所以无法分离。若把 16:9 输出配上**全阵列 crop**：
     *   - 若 skew 仍是 6.91 ms（按 16:9 算出的行数）⇒ skew 由**输出纵横比**决定；
     *   - 若 skew 变成 9.21 ms（按 3072 行算出）⇒ skew 由 **crop 高度**决定。
     * 注意这会**故意**制造各向异性映射（fx != fy），因此该 trial 的几何不可用，
     * 只用于读 skew —— 报告里用 `crop_shrunk_for_aspect=false` 与 `forced_full_crop=true`
     * 双标记，避免下游误把它当成合格配置。
     */
    private val forceFullCrop: Boolean = false,
    /**
     * **单张捕获模式**：`session.capture()` 一次下一条请求、拿一张，而不是
     * `setRepeatingRequest` 连续流。启用后帧间隔由本函数设定的节奏决定，
     * 与 sensor 帧率无关 —— 这正是"拍照"的行为。
     *
     * 默认 false，②③④⑤ 一律不传 ⇒ 它们的采集行为与 v1.5~1.7 **完全相同**。
     */
    private val singleShot: Boolean = false,
    /** 单张捕获之间的间隔（毫秒）。给管线留出"停一拍再启动"的余地。 */
    private val shotIntervalMs: Long = 300L,
    /**
     * 输出格式（`ImageFormat.*`）。默认 YUV_420_888。
     *
     * 拍照路径要试 JPEG：JPEG 由 ISP 完整处理后写出，几何与时间戳语义都可能变，
     * 而且它的 EXIF 时间字段是**系统墙钟**而不是 sensor 时间 —— 这条必须实测量化，
     * 因为"把 JPEG 文件回传到 PC 再读 EXIF 当时间戳"是一个非常容易犯的错。
     */
    private val outputFormat: Int = ImageFormat.YUV_420_888,
    /**
     * 请求模板。默认 `TEMPLATE_MANUAL`；拍照路径用 `TEMPLATE_STILL_CAPTURE`。
     *
     * 模板会改变 HAL 的默认行为（AE/AF 触发方式、ZSL、时域降噪与边缘增强档位），
     * 所以它是采集路径的一部分，必须随报告记录并被判据比对。
     */
    private val template: Int = CameraDevice.TEMPLATE_MANUAL,
    /** 采集路径标识（与 [capturePathRev] 配对）。见 companion 里的 PATH_* 常量。 */
    private val capturePathId: String = PATH_BURST_YUV_MANUAL,
    /** 该采集路径的修订号。见 [CAPTURE_PATH_REV] 的注释：**这是按路径算的**。 */
    private val capturePathRev: Int = CAPTURE_PATH_REV,
    /**
     * 是否**刻意不发** `SENSOR_FRAME_DURATION`。
     *
     * 拍照交付档要发 true：拍照是一张一张按需触发，帧率没有意义；而帧时长约束会把
     * sensor 拉进"视频节奏"，可能连带改变读出模式（进而改变 skew 与几何）。
     * 对照档必须发 false —— 否则它相对 ② 一次动了**两个**变量
     * （单张 vs 重复请求、有帧时长约束 vs 无），那就不是受控实验，
     * 得出的"时间戳语义相同/不同"无法归因。这正是混淆变量那个老坑。
     */
    private val suppressFrameDuration: Boolean = false
) {

    interface Callback {
        fun onProgress(msg: String)
        fun onDone(report: JSONObject?, error: String?)
    }

    companion object {
        const val DEFAULT_FRAMES = 150
        const val DEFAULT_WARMUP = 5

        /**
         * 报告/采集逻辑版本 —— **只用来标识代码，不用来判"采集行为是否相同"**。
         *
         * 为什么要专门写这句：判据曾经拿版本号当"两份报告是不是同一套采集"的依据，
         * 结果每改一行代码（哪怕只加一个报告字段）都会让 ③ 的跨会话证据作废、要求重跑；
         * 而重跑又会让 ② 与 ③ 分属不同版本，**无限循环**。实测踩过：
         * 第 6 轮让人重跑 ②，第 7 轮判据就要求再重跑 ③ —— 而两者采集路径完全相同。
         *
         * 所以：发版时**每次都必须 +1**（哪怕只改注释），守住"版本号与代码一一对应"；
         * 而"采集行为是否相同"由 [CAPTURE_PATH_REV] 回答。
         */
        // ⚠ 一个 APK 只能有**一个**版本号：探针报告与交付 hello 都从这里取。
        // 之前 DeliverySession 自带一份 "m0probe-1.9.0"，同一个安装包会报出两个版本
        // ⇒ 采集路径登记表对不上号（同一构建要登记两次）。1.9.1 起统一。
        // ⚠ 1.13.0 → 1.14.0：新增 `capture_mode=live`（只看不留 + 彩色实时道）。
        // 这是**采集行为变更**（多了一套"不储存"的形态，且实时道产出彩色 JPEG），
        // 所以 +1；登记表里新增一条路径 `live_stream_color`，
        // **既有路径的 rev 一个都没动**（`stream_deliver_yuv` 仍为 3）。
        // ⚠ v1.15.0：只**改版本标签**。`LockedCaptureProbe` 的**采集行为一字未改**
        //   （本轮新增的 `color_source` 只作用于 `DeliverySession` 的 live 道）⇒
        //   M0 证据链里 8 份报告仍按各自登记版本有效，不受此标签影响。
        const val APP_VERSION = "m0probe-1.15.0"

        /**
         * 单张模式结算前，给"末帧的图"留下的到达窗口。
         *
         * 实测（第 12 轮）：8 张下发、结果回调全部到齐时只取到 7 张图 —— 第 8 张
         * 还在管道里。图回调与结果回调跑在**同一个 Handler** 上，所以等待只能靠
         * `postDelayed` 让出线程，不能 sleep（sleep 会把图回调一起堵死）。
         */
        const val SETTLE_MAX_MS = 1200L
        const val SETTLE_POLL_MS = 150L

        /**
         * 采集路径修订号 —— 判断"两份报告是不是同一条采集路径"的依据。
         *
         * ⚠ 修订号是**按路径**算的，不是按 App 版本算的（v1.8 起明确）：
         * 同一版 App 里可以并存多条采集路径（②③④⑤ 的连拍路径、⑥ 的两条拍照路径），
         * 各路径各有自己的修订号。否则"给 App 加一条新路径"会让**其它路径**的证据
         * 一起作废 —— 那正是第 6/7 轮的无限返工循环换了个形式再犯一次。
         * 所以本版新增 ⑥ 拍照路径时，**这条连拍路径的 rev 保持 1 不变**。
         *
         * 只在**会改变采集行为**的改动时 +1：
         *   请求参数 / 会话配置 / crop 与尺寸选择规则 / 帧率下发方式 /
         *   预热与丢弃规则 / 锁定字段集合 / 模板 / 输出格式 / 单张 vs 重复请求
         * 下列改动 **不 +1**：报告字段增删改名、日志、注释、UI 布局。
         *
         * 它补的是"请求参数相同、但代码不同"这一层残余风险：请求参数本身已由
         * config_fingerprint 独立校验，但指纹证明不了"是同一份代码在采集"。
         */
        const val CAPTURE_PATH_REV = 1

        /**
         * 采集路径标识。与 [CAPTURE_PATH_REV] 配对使用：**只有 (id, rev) 这对量
         * 相同才说明是同一路径**。只比 rev 会犯错 —— 两条完全不同的路径各自的
         * 第 1 版都是 rev=1，单看数字无法区分。
         */
        const val PATH_BURST_YUV_MANUAL = "burst_yuv_manual"      // ②③④⑤
        const val PATH_STILL_YUV_SINGLE = "still_yuv_single"      // ⑥ 对照档
        const val PATH_STILL_JPEG_TEMPLATE = "still_jpeg_template" // ⑥ 交付档

        /**
         * 两条拍照路径的修订号（各自独立计数）。
         *
         * **v1.8.1：1 → 2。** 理由：取图方式从"回调时刻最后拿到的那张"改成
         * "按 `Image.timestamp` 与结果配对"。这不是纯报告字段改动 —— 它改变了
         * **哪张图被记在哪一张结果上**，也就是改变了测出来的量，所以要 +1。
         * （连续流那条路径的取图方式没变，[CAPTURE_PATH_REV] 保持 1。）
         */
        /**
         * 两条拍照路径的修订号。
         *
         * 1→2（v1.8.1）：取图从"回调时刻最后拿到的那张"改成按 `SENSOR_TIMESTAMP` 配对。
         * 2→3（v1.8.3）：配对从**在线**（结果回调里查表）改成**结算时离线按集合配对**，
         *   并新增结算前的等待窗口。为什么算采集行为变更、而不是纯报告字段：
         *   同一次采集在两个版本下会被判成**不同的配对结果**（在线版把"图比结果晚到"
         *   记成丢图，离线版记成配对成功）—— 与 1.8.1 那次是同一判据：
         *   "哪张图被记在哪一张结果上"变了，就是测出来的量变了。
         */
        const val STILL_PATH_REV = 3

        /**
         * 打开摄像头遭遇**设备级瞬时故障**时的重试退避（毫秒）。
         *
         * 第 11 轮实测：② 主连拍成功后的约 20 秒窗口内，⑥⑤④ 的 12 次打开里
         * 9 次失败（onDisconnected「摄像头被断开」×8、openCamera 抛
         * CameraAccessException CAMERA_ERROR/createDefaultRequest ENOSYS ×1），
         * 随后**自行恢复**（③ 三轮全部成功）—— 是相机服务的瞬时劣化窗口，
         * 不是档位或路径不支持。此类故障的正确处置是**等它过去再打开**，
         * 而不是把整组证据报废。
         *
         * 为什么这不构成 rev 变更：重试只改变"**什么时候**打开成功"，
         * 打开成功后的会话配置、请求参数、丢弃规则与逐帧记录完全不变；
         * 且每次失败都在报告 `open_retry_errors` 里留痕（可审阅）。
         */
        val OPEN_RETRY_BACKOFF_MS = longArrayOf(2000L, 5000L, 10000L)

        /**
         * 采集尺寸的像素面积上限（YUV_420_888）。
         *
         * 取 2048x1536 = 3.15 MP 而不是 1920x1080 = 2.07 MP：后者会把所有 4:3 尺寸
         * 排除在外（1600x1200 面积更小），从而被迫选中 16:9，破坏 fx==fy。
         * 这个上限只影响 YUV 连拍的带宽，不影响后续 RAW 标定的选档。
         */
        const val CAPTURE_AREA_CAP_PX = 2048L * 1536L
    }

    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var reader: ImageReader? = null
    private var cb: Callback? = null
    private var finished = false
    private var abort = false

    private val frames = ArrayList<JSONObject>()
    private val requestedInfo = JSONObject()
    private val appliedInfo = JSONObject()
    private val captureConfig = JSONObject()
    private var readoutSize = JSONObject()
    private var lockFields = listOf<String>()
    private var sizeCandidates = JSONArray()
    private var sizeChoiceNote = ""
    private var requestCrop: Rect? = null

    // ---- v1.8.2：打开摄像头的瞬时故障重试（背景见 OPEN_RETRY_BACKOFF_MS 注释）----
    private var cameraManager: CameraManager? = null
    private var openAttempts = 0
    private val openErrors = ArrayList<String>()
    private var settleWaitMs = 0L

    // ---- v1.8：单张捕获模式的状态 ----
    /** 单张模式下复用的请求对象（构建一次，逐张下发）。 */
    private var pendingRequest: CaptureRequest? = null
    /** 已下发的张数。单张模式下用它推进节奏、判断何时收工。 */
    private var shotsDispatched = 0
    /** 最近一次下发请求的 `elapsedRealtimeNanos` —— 用来量"从下发到拿到结果"的延迟。 */
    private var lastRequestSentElapsedNs = 0L

    // ---- v1.8.1：图与「结果」的配对 ----
    //
    // 图像走 `ImageReader` 回调、`CaptureResult` 走 `onCaptureCompleted`，**两条
    // 独立通路，到达顺序不保证**。v1.8.0 的写法是"回调时刻最后拿到的那张图"
    // （单个 `lastJpeg` 变量 + 用完置空），它有两个看不出问题的失败模式：
    //
    //   (a) 图晚到 → 被记到**下一张**结果头上。图和时间戳都是真的，只是错配，
    //       报告里任何字段都不会自相矛盾。
    //   (b) 图没到位 → 只留一个 `null`，无法区分"管线根本没给图"与"读字节失败"。
    //
    // 现在按**图像自带的时间戳**（`Image.timestamp`，与 `SENSOR_TIMESTAMP` 同一
    // 时间基）配对：配不上就是配不上，还会把"多余的图"单列出来。
    // 只有单张模式启用（连续流 150 帧没必要留字节，也没有这个配对需求）。
    private val imagesByTs = HashMap<Long, ByteArray?>()
    private var imagesAcquired = 0     // 成功取到 Image 的次数
    private var acquireNullCount = 0   // acquireLatestImage 返回 null（队列里没有图）
    private var imageReadErrors = 0    // 取到 Image，但读字节抛异常
    // ⚠ v1.8.3：`imageMatched`/`imageMissing` 两个在线计数器已删除。
    // 它们在**结果回调里**查表，会把"图比结果晚到"误判成丢图（第 12 轮实测），
    // 真丢与晚到区分不开。配对改为结算时离线做（见 buildStats 与 finishAfterImagesArrive），
    // 计数也就在那里现算成 matched/missing_valid/missing_warmup。

    private fun templateName(t: Int): String = when (t) {
        CameraDevice.TEMPLATE_MANUAL -> "TEMPLATE_MANUAL"
        CameraDevice.TEMPLATE_STILL_CAPTURE -> "TEMPLATE_STILL_CAPTURE"
        CameraDevice.TEMPLATE_PREVIEW -> "TEMPLATE_PREVIEW"
        CameraDevice.TEMPLATE_RECORD -> "TEMPLATE_RECORD"
        CameraDevice.TEMPLATE_VIDEO_SNAPSHOT -> "TEMPLATE_VIDEO_SNAPSHOT"
        CameraDevice.TEMPLATE_ZERO_SHUTTER_LAG -> "TEMPLATE_ZERO_SHUTTER_LAG"
        else -> "TEMPLATE_$t"
    }

    private fun formatName(f: Int): String = when (f) {
        ImageFormat.YUV_420_888 -> "YUV_420_888"
        ImageFormat.JPEG -> "JPEG"
        ImageFormat.RAW_SENSOR -> "RAW_SENSOR"
        ImageFormat.PRIVATE -> "PRIVATE"
        else -> "FORMAT_$f"
    }

    fun run(callback: Callback) {
        cb = callback
        val t = HandlerThread("m0probe"); t.start()
        thread = t
        handler = Handler(t.looper)
        val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        callback.onProgress("打开摄像头 $cameraId ...")

        // 超时预算：连续流按帧周期算；**单张模式不能按帧周期算** —— 每张都要
        // 走一次完整的下发→曝光→读出→ISP→回调，全幅 JPEG 单张可达 1 s 以上，
        // 按 30 fps 算预算会立刻超时，而"超时"会被读成"设备不支持"，把方向带偏。
        // 预算里还要计入打开重试的退避时间，否则重试会被自己的超时判死。
        val retryExtraMs = OPEN_RETRY_BACKOFF_MS.sum() + OPEN_RETRY_BACKOFF_MS.size * 3000L
            + SETTLE_MAX_MS   // 单张结算前等末帧图到达的窗口
        val budgetMs = (if (singleShot)
            targetFrames.toLong() * (shotIntervalMs + 4000L) + 15000L
        else
            targetFrames.toLong() * 1000L / targetFps.coerceAtLeast(1) + 8000L) + retryExtraMs
        handler?.postDelayed({
            if (!finished) finish(null, "超时：${budgetMs}ms 内未收到 $targetFrames 帧（已收 ${frames.size}）")
        }, budgetMs)

        cameraManager = manager
        openCameraOnce()
    }

    /** 打开一次摄像头（失败时由 [onDeviceLevelFailure] 决定是否退避重试）。 */
    @SuppressLint("MissingPermission")
    private fun openCameraOnce() {
        val manager = cameraManager ?: return
        openAttempts++
        try {
            manager.openCamera(cameraId, deviceStateCallback, handler)
        } catch (e: SecurityException) {
            // 权限问题重试无意义，直接失败
            finish(null, "无 CAMERA 权限: ${e.message}")
        } catch (e: Exception) {
            // CameraAccessException（含 MAX_CAMERAS_IN_USE / CAMERA_ERROR）按瞬时故障处理
            onDeviceLevelFailure(null, "openCamera 失败: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    /**
     * 设备级故障的统一处置：**尚未采到任何帧**时按退避表重试打开；
     * 采到帧之后（或重试耗尽）才判失败。
     *
     * 为什么只在"零帧"时重试：重试会重建会话，帧序号与时间轴从头开始 ——
     * 采到一半重建，报告就是两个会话的拼接，证据含义全变。而打开阶段的
     * 失败什么数据都还没产生，重试不改变任何采集语义（见 OPEN_RETRY_BACKOFF_MS）。
     */
    private fun onDeviceLevelFailure(d: CameraDevice?, what: String) {
        if (finished || abort) return
        try { d?.close() } catch (e: Exception) { }
        if (device === d) device = null
        openErrors.add(what)
        val retryIdx = openAttempts - 1  // 已失败的是第 openAttempts 次打开
        if (frames.isEmpty() && retryIdx < OPEN_RETRY_BACKOFF_MS.size) {
            val delay = OPEN_RETRY_BACKOFF_MS[retryIdx]
            cb?.onProgress("$what —— ${delay / 1000}s 后重试打开（第 ${openAttempts + 1} 次）")
            handler?.postDelayed({ if (!finished && !abort) openCameraOnce() }, delay)
        } else {
            val hist = if (openErrors.size > 1) "（打开尝试 ${openAttempts} 次均失败: $openErrors）" else ""
            finish(null, what + hist)
        }
    }

    private val deviceStateCallback = object : CameraDevice.StateCallback() {
        override fun onOpened(d: CameraDevice) {
            if (finished || abort) {
                try { d.close() } catch (e: Exception) { }
                return
            }
            device = d
            try {
                startSession(d)
            } catch (e: Exception) {
                finish(null, "创建会话失败: ${e.javaClass.simpleName}: ${e.message}")
            }
        }

        override fun onDisconnected(d: CameraDevice) {
            onDeviceLevelFailure(d, "摄像头被断开")
        }

        override fun onError(d: CameraDevice, error: Int) {
            onDeviceLevelFailure(d, "摄像头错误 code=$error")
        }
    }

    fun close() {
        abort = true
        finished = true
        try { session?.close() } catch (e: Exception) { }
        try { device?.close() } catch (e: Exception) { }
        try { reader?.close() } catch (e: Exception) { }
        // 释放配对用的图字节（交付档 8 张全幅 JPEG 可占几十 MB）
        imagesByTs.clear()
        session = null; device = null; reader = null
        thread?.quitSafely(); thread = null; handler = null
    }

    @SuppressLint("MissingPermission")
    private fun startSession(d: CameraDevice) {
        val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val chars = manager.getCameraCharacteristics(cameraId)
        val map = chars.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            ?: throw IllegalStateException("无 StreamConfigurationMap")

        val size = pickCaptureSize(map, chars)
        readoutSize = JSONObject().apply { put("w", size.width); put("h", size.height) }
        cb?.onProgress("采集尺寸 ${size.width}x${size.height} (${formatName(outputFormat)})"
            + if (singleShot) "，单张捕获 ×$targetFrames" else "")
        captureConfig.put("size_choice_note", sizeChoiceNote)
        captureConfig.put("size_candidates", sizeCandidates)

        // 标定得到的内参是针对输出尺寸的，必须记录它与传感器全幅之间的缩放关系
        captureConfig.put("output_size", JSONObject().apply { put("w", size.width); put("h", size.height) })
        captureConfig.put("output_format", formatName(outputFormat))
        // 模板与调度方式也是采集路径的一部分：同一套锁定参数配不同模板，
        // HAL 的默认行为（ZSL/时域降噪/边缘增强档位）可能完全不同。
        captureConfig.put("template_name", templateName(template))
        captureConfig.put("single_shot", singleShot)
        if (singleShot) captureConfig.put("shot_interval_ms", shotIntervalMs)
        val active = chars.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
        if (active != null) {
            captureConfig.put("active_array", JSONObject().apply {
                put("w", active.width()); put("h", active.height())
            })
            captureConfig.put("output_to_active_scale_x", size.width.toDouble() / active.width())
            captureConfig.put("output_to_active_scale_y", size.height.toDouble() / active.height())

            // crop region 必须与输出同纵横比，否则映射是各向异性的（fx != fy）。
            // 见 aspectSafeCrop 的注释；这里把最终采用的值与推导出的缩放一起记进报告，
            // 让 PC 端能算准 fx/fy，而不是拿 output/active 的比例去猜。
            val crop = if (forceFullCrop) active else aspectSafeCrop(active, size.width, size.height)
            requestCrop = crop
            captureConfig.put("forced_full_crop", forceFullCrop)
            captureConfig.put("crop_region", JSONObject().apply {
                put("left", crop.left); put("top", crop.top)
                put("width", crop.width()); put("height", crop.height())
            })
            captureConfig.put("output_to_crop_scale_x", size.width.toDouble() / crop.width())
            captureConfig.put("output_to_crop_scale_y", size.height.toDouble() / crop.height())
            captureConfig.put("crop_shrunk_for_aspect",
                crop.width() != active.width() || crop.height() != active.height())
        }
        map.getOutputSizes(ImageFormat.YUV_420_888)
            ?.maxByOrNull { it.width.toLong() * it.height.toLong() }
            ?.let { captureConfig.put("max_yuv_size", JSONObject().apply { put("w", it.width); put("h", it.height) }) }
        map.getOutputSizes(ImageFormat.JPEG)
            ?.maxByOrNull { it.width.toLong() * it.height.toLong() }
            ?.let { captureConfig.put("max_jpeg_size", JSONObject().apply { put("w", it.width); put("h", it.height) }) }
        map.getOutputSizes(ImageFormat.RAW_SENSOR)
            ?.maxByOrNull { it.width.toLong() * it.height.toLong() }
            ?.let { captureConfig.put("max_raw_size", JSONObject().apply { put("w", it.width); put("h", it.height) }) }

        val r = ImageReader.newInstance(size.width, size.height, outputFormat,
            if (singleShot) 2 else 4)
        reader = r
        // 必须持续取走并释放缓冲，否则 ImageReader 队列满会阻塞整个采集管线。
        // JPEG 格式额外把字节拷出来：EXIF 时间字段的分辨率要实测，不能凭常识断言。
        r.setOnImageAvailableListener({ rd ->
            try {
                val img = rd.acquireLatestImage()
                if (img == null) {
                    acquireNullCount++
                } else {
                    // Image.timestamp 与 SENSOR_TIMESTAMP 同一时间基（REALTIME），
                    // 所以可以拿它当配对键，把"图属于哪张结果"这件事变成可判的。
                    val ts = img.timestamp
                    var bytes: ByteArray? = null
                    if (outputFormat == ImageFormat.JPEG) {
                        bytes = imageBytes(img)
                        if (bytes == null) imageReadErrors++
                    }
                    imagesAcquired++
                    // 只有单张模式留字节：连续流 150 帧会把内存撑爆，而那里不核配对
                    if (singleShot) imagesByTs[ts] = bytes
                    img.close()
                }
            } catch (e: Exception) { imageReadErrors++ }
        }, handler)

        @Suppress("DEPRECATION")
        d.createCaptureSession(listOf(r.surface), object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(s: CameraCaptureSession) {
                session = s
                try {
                    val req = buildRequest(d, chars, r.surface)
                    pendingRequest = req
                    val cbObj = object : CameraCaptureSession.CaptureCallback() {
                        override fun onCaptureCompleted(
                            se: CameraCaptureSession,
                            request: CaptureRequest,
                            result: TotalCaptureResult
                        ) {
                            onFrame(result)
                        }
                    }
                    if (singleShot) {
                        // 拍照：一次一条请求，拿到结果后再发下一条
                        dispatchNextShot()
                    } else {
                        s.setRepeatingRequest(req, cbObj, handler)
                    }
                } catch (e: Exception) {
                    finish(null, "下发请求失败: ${e.javaClass.simpleName}: ${e.message}")
                }
            }

            override fun onConfigureFailed(s: CameraCaptureSession) {
                finish(null, "会话配置失败（可能该机型不支持该输出格式的手动会话）")
            }
        }, handler)
    }

    /**
     * 单张捕获：下发一条请求，等回调，再走下一张。
     *
     * **为什么要串行而不是连发**：连发多条 `capture()` 时 HAL 可以自由重排，
     * 于是"第 k 张的 SENSOR_TIMESTAMP 对应我第 k 次下发"这个对应关系就没了。
     * 而拍照路径要回答的恰恰是"一次下发 → 一张图 → 一个时间戳"这条链是否成立，
     * 所以必须一张一张来，把对应关系钉死。
     */
    private fun dispatchNextShot() {
        if (finished || abort) return
        if (shotsDispatched >= targetFrames) return
        val s = session ?: return
        val req = pendingRequest ?: return
        lastRequestSentElapsedNs = SystemClock.elapsedRealtimeNanos()
        shotsDispatched++
        try {
            s.capture(req, object : CameraCaptureSession.CaptureCallback() {
                override fun onCaptureCompleted(
                    se: CameraCaptureSession, request: CaptureRequest, result: TotalCaptureResult
                ) {
                    onFrame(result)
                }
            }, handler)
        } catch (e: Exception) {
            finish(null, "单张下发失败（第 $shotsDispatched 张）: ${e.javaClass.simpleName}: ${e.message}")
        }
    }

    /** 把 Image 的单个 plane 拷成字节数组（JPEG 只有一个 plane）。 */
    private fun imageBytes(img: android.media.Image): ByteArray? {
        return try {
            val p = img.planes[0]
            val b = p.buffer
            val out = ByteArray(b.remaining())
            b.get(out)
            out
        } catch (e: Exception) {
            null
        }
    }

    private fun onFrame(result: TotalCaptureResult) {
        if (finished || abort) return

        val f = JSONObject()
        f.put("frame_number", result.frameNumber)
        f.put("frame_index", frames.size)
        // 预热帧标记：session 起始镜头仍在机械收敛，这些帧的几何不可信
        f.put("is_warmup", frames.size < warmupFrames)
        f.put("sensor_timestamp_ns", numOrNull(result.get(CaptureResult.SENSOR_TIMESTAMP)))

        // ---- v1.8：时间戳"真不真"的直接仪器 ----
        // SENSOR_TIMESTAMP 与 elapsedRealtimeNanos 在 **同一个时间基**（REALTIME）上，
        // 所以这三个字段可以直接相减，且差值就是物理量：
        //   callback_latency_ns = 从"曝光开始"到"应用拿到结果"的延迟
        //   request_to_result_ns = 从"下发请求"到"拿到结果"的周转
        // 两个用途：
        //   ① 延迟稳定 ⇒ 可以用"回调时刻 − 延迟"反推曝光时刻，时间戳是可用的；
        //   ② 若某张的 sensor_timestamp 明显**早于** 上一条请求的下发时刻，
        //      说明它是从零快门延迟(ZSL)缓冲里翻出来的旧帧 —— 那时"这张照片的时刻"
        //      不是"我按下的时刻"，跨设备对齐会系统性错位。这是拍照路径特有的风险，
        //      连续流里根本不会出现，所以只有单张模式才测得到。
        val nowElapsed = SystemClock.elapsedRealtimeNanos()
        f.put("callback_elapsed_ns", nowElapsed)
        f.put("wall_clock_ms", System.currentTimeMillis())
        val sts = result.get(CaptureResult.SENSOR_TIMESTAMP)
        if (sts != null) f.put("callback_latency_ns", nowElapsed - sts)
        if (lastRequestSentElapsedNs > 0) {
            f.put("request_sent_elapsed_ns", lastRequestSentElapsedNs)
            f.put("request_to_result_ns", nowElapsed - lastRequestSentElapsedNs)
            // >0 表示时间戳晚于下发（正常：先按下后曝光）；<0 表示时间戳早于下发（ZSL 旧帧）
            if (sts != null) f.put("sensor_ts_minus_request_ns", sts - lastRequestSentElapsedNs)
        }
        // 回读的 ZSL 开关。它是"合成/缓存路径"的第一手证据：
        // 只有拍照路径会开它，而开了它时间戳语义就不再是"曝光起点"这么简单。
        f.put("control_enable_zsl", numOrNull(result.get(CaptureResult.CONTROL_ENABLE_ZSL)))

        // ⚠ 配对**不在这里做**（v1.8.3 起）。
        //
        // v1.8.1/v1.8.2 的做法是"在结果回调里按 SENSOR_TIMESTAMP 查图存在与否"，
        // 业务键选对了，但**时机**错了：图与结果是两条独立回调，谁先到不保证。
        // 第 12 轮实测（对照档 8 张）：5 张被判"没有配对到图"，而其中 **4 张的时间戳
        // 同时又出现在"没有被认领的图"里** —— 同一时间戳两处出现，只可能是
        // "图比结果晚到"。真丢图与晚到的图，在在线查表下**无法区分**。
        // ⇒ 改为报告生成时按**时间戳集合**离线配对（见 buildStats），与先后无关；
        //    并且 EXIF 也在那时读（它需要配对上的字节）。

        f.put("lens_focus_distance_diopter", numOrNull(result.get(CaptureResult.LENS_FOCUS_DISTANCE)))
        f.put("sensor_exposure_time_ns", numOrNull(result.get(CaptureResult.SENSOR_EXPOSURE_TIME)))
        f.put("sensor_sensitivity_iso", numOrNull(result.get(CaptureResult.SENSOR_SENSITIVITY)))
        f.put("sensor_frame_duration_ns", numOrNull(result.get(CaptureResult.SENSOR_FRAME_DURATION)))
        // 卷帘读出时间：逐帧可读（首行到「末行之后一行」曝光起点之差）= 读出窗口的读出时间。
        //
        // ⚠️ 这条注释改过两次，两次都错，别再凭"看起来成比例"下结论：
        //   v1.4 说"按整个活动阵列报告，输出只覆盖 N 行时须乘 N/H" —— 错。
        //   v1.5 说"是输出帧的读出时间，随输出高度变" —— 也错。
        // 判据是把**四个档位**放在一张表里算不变量（⑤尺寸扫描提供了受控实验）：
        //     输出         skew        skew/outH   skew/cropH  skew/(crop_w·outH/outW)
        //   1920x1080    6909696      6397.87     2249.25          2999.000
        //   1920x1440    9212928      6397.87     2999.00          2999.000
        //   2880x2160    9212928      4265.24     2999.00          2999.000
        //   4096x3072    9212928      2999.00     2999.00          2999.000
        // 只有最后一列恒定 ⇒ 真实的量是「**按输出纵横比取的读出窗口行数**」：
        //     H_read = crop_w · outH / outW        （即 crop_w / 输出纵横比）
        //     skew   = 2999.0 ns/行 · H_read
        // 物理上就是：读出窗口取满 crop 宽度、纵横比跟输出走 ⇒ 输出比 crop 更"宽"时读的行更少
        // （16:9 输出只读 2304 行 = 3/4，所以 6.91 ms 而不是 9.21 ms）。
        //
        // 操作结论：(1) **不要**再按输出行数或活动阵列行数自己缩放，直接用报出的值；
        //          (2) 交付配置（4:3 输出、crop=全阵列）下它就是 9.212928 ms，占帧周期 27.7%；
        //          (3) **改输出纵横比或改 crop 都必须重测** —— 只有这两件被证明会改变它。
        f.put("rolling_shutter_skew_ns",
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P)
                numOrNull(result.get(CaptureResult.SENSOR_ROLLING_SHUTTER_SKEW))
            else JSONObject.NULL)
        // 像素模式（0=BINNED/DEFAULT，1=MAXIMUM_RESOLUTION）：切换会改变内参，必须记录
        f.put("sensor_pixel_mode", numOrNull(result.get(CaptureResult.SENSOR_PIXEL_MODE)))
        // Camera2 公共 API 不提供传感器温度，只能用电池温度作代理（滞后量）
        f.put("battery_temperature_c", batteryTempC() ?: JSONObject.NULL)

        f.put("control_mode", CapabilityReport.controlMode(result.get(CaptureResult.CONTROL_MODE)))
        f.put("af_mode", CapabilityReport.afMode(result.get(CaptureResult.CONTROL_AF_MODE)))
        f.put("af_state", CapabilityReport.afState(result.get(CaptureResult.CONTROL_AF_STATE)))
        f.put("ae_mode", CapabilityReport.aeMode(result.get(CaptureResult.CONTROL_AE_MODE)))
        f.put("ae_state", CapabilityReport.aeState(result.get(CaptureResult.CONTROL_AE_STATE)))
        f.put("awb_mode", CapabilityReport.awbMode(result.get(CaptureResult.CONTROL_AWB_MODE)))
        f.put("lens_state", CapabilityReport.lensState(result.get(CaptureResult.LENS_STATE)))
        f.put("ois_mode", CapabilityReport.oisMode(result.get(CaptureResult.LENS_OPTICAL_STABILIZATION_MODE)))
        f.put("video_stab_mode", CapabilityReport.videoStabMode(result.get(CaptureResult.CONTROL_VIDEO_STABILIZATION_MODE)))
        f.put("edge_mode", CapabilityReport.edgeMode(result.get(CaptureResult.EDGE_MODE)))
        f.put("noise_reduction_mode", CapabilityReport.noiseReductionMode(result.get(CaptureResult.NOISE_REDUCTION_MODE)))
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            f.put("aberration_mode",
                CapabilityReport.aberrationMode(result.get(CaptureResult.COLOR_CORRECTION_ABERRATION_MODE)))
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            f.put("distortion_correction_mode",
                CapabilityReport.distortionMode(result.get(CaptureResult.DISTORTION_CORRECTION_MODE)))
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            f.put("zoom_ratio", numOrNull(result.get(CaptureResult.CONTROL_ZOOM_RATIO)))
        }
        // 当前生效的**物理摄像头** ID。这是判定"有没有中途换镜"的唯一直接仪器：
        // 逻辑摄像头的 zoom 锁在 1.0 并不能证明 HAL 内部没换物理镜头，
        // 而换镜会让焦距/主点/畸变同时跳变 —— 内参静默失效的最隐蔽来源。
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            f.put("active_physical_id",
                result.get(CaptureResult.LOGICAL_MULTI_CAMERA_ACTIVE_PHYSICAL_ID) ?: JSONObject.NULL)
        }
        val crop = result.get(CaptureResult.SCALER_CROP_REGION)
        f.put("crop_region", if (crop == null) JSONObject.NULL else JSONObject().apply {
            put("left", crop.left); put("top", crop.top); put("width", crop.width()); put("height", crop.height())
        })

        frames.add(f)
        if (frames.size % 25 == 0) cb?.onProgress("已采集 ${frames.size}/$targetFrames 帧")

        if (frames.size >= targetFrames) {
            try { session?.stopRepeating() } catch (e: Exception) { }
            // 单张模式：末帧的**图**可能还在飞（结果回调先到）。实测第 8 张如此。
            // 先给它一点时间，再结算；不能在这里 sleep（图回调与结果回调同线程）。
            if (singleShot) finishAfterImagesArrive() else finish(buildReport(), null)
        } else if (singleShot) {
            // 上一张已经拿到结果，隔一拍再下发下一张
            handler?.postDelayed({ dispatchNextShot() }, shotIntervalMs)
        }
    }

    /**
     * 读一张 JPEG 的 EXIF 时间字段。
     *
     * 为什么这件事值得单独做：`SENSOR_TIMESTAMP` 是 REALTIME 域的物理时刻，
     * 而 JPEG 的 `DateTimeOriginal/SubSecTimeOriginal` 是**系统墙钟**、精度由厂商决定。
     * 二者只要混用一次，时间戳就会错到秒级，而且**画面完全正常、不报任何错**。
     * 所以这里把"EXIF 到底有没有亚秒、几位"变成报告里的一个数，
     * 让"能不能拿 EXIF 当时间源"变成一条有证据的判断，而不是常识断言。
     */
    private fun exifInfo(jpeg: ByteArray): JSONObject {
        val o = JSONObject()
        o.put("bytes", jpeg.size)
        return try {
            val ex = ExifInterface(ByteArrayInputStream(jpeg))
            fun g(tag: String): Any {
                val v = try { ex.getAttribute(tag) } catch (e: Exception) { null }
                return v ?: JSONObject.NULL
            }
            o.put("datetime_original", g(ExifInterface.TAG_DATETIME_ORIGINAL))
            o.put("subsec_time_original", g(ExifInterface.TAG_SUBSEC_TIME_ORIGINAL))
            o.put("offset_time_original", g(ExifInterface.TAG_OFFSET_TIME_ORIGINAL))
            o.put("datetime", g(ExifInterface.TAG_DATETIME))
            val ss = try { ex.getAttribute(ExifInterface.TAG_SUBSEC_TIME_ORIGINAL) } catch (e: Exception) { null }
            // 亚秒字段的位数 = 该时间字段的分辨率量级（3 位 ≈ 1 ms，0 位 = 只有秒）
            o.put("subsec_digits", ss?.length ?: 0)
            o.put("has_subsec", ss != null && ss.isNotEmpty())
            o
        } catch (e: Exception) {
            o.put("error", "${e.javaClass.simpleName}: ${e.message}")
            o
        }
    }

    private fun buildRequest(
        d: CameraDevice,
        chars: CameraCharacteristics,
        surface: android.view.Surface
    ): CaptureRequest {
        val b = d.createCaptureRequest(template)
        b.addTarget(surface)

        // ---- CONTROL_MODE_OFF：关掉厂商场景优化 ----
        b.set(CaptureRequest.CONTROL_MODE, CameraMetadata.CONTROL_MODE_OFF)
        appliedInfo.put("control_mode_off", true)

        // ---- 对焦锁定 ----
        // 对焦距离决定景深落在哪。取超焦距会把近处目标整体虚掉且不报错，
        // 所以显式工作距离优先，超焦距只作兜底。
        val minFocus = chars.get(CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE) ?: 0f
        val hyperFocus = chars.get(CameraCharacteristics.LENS_INFO_HYPERFOCAL_DISTANCE) ?: 0f
        val afModes = chars.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES)
        val canManualFocus = minFocus > 0f && afModes?.contains(CameraMetadata.CONTROL_AF_MODE_OFF) == true

        val focusDiopter: Float
        val focusPolicy: String
        when {
            focusDistanceM > 0f && minFocus > 0f -> {
                val want = 1.0f / focusDistanceM
                focusDiopter = want.coerceIn(0.0f, minFocus)
                focusPolicy = if (want > minFocus) "working_distance_clamped_to_near_limit"
                               else "explicit_working_distance"
            }
            hyperFocus > 0f -> {
                focusDiopter = hyperFocus
                focusPolicy = "hyperfocal_fallback_NEAR_OBJECTS_WILL_BE_BLURRED"
            }
            else -> {
                focusDiopter = 0f
                focusPolicy = "infinity"
            }
        }
        requestedInfo.put("focus_policy", focusPolicy)
        requestedInfo.put("min_focus_distance_diopter", minFocus.toDouble())
        requestedInfo.put("hyperfocal_distance_diopter", hyperFocus.toDouble())
        requestedInfo.put("focus_diopter", focusDiopter.toDouble())
        if (focusDistanceM > 0f) requestedInfo.put("focus_distance_m", focusDistanceM.toDouble())
        if (focusDiopter > 0f) {
            requestedInfo.put("focus_requested_distance_m", (1.0 / focusDiopter))
        }
        appliedInfo.put("manual_focus_applied", canManualFocus)
        if (afModes?.contains(CameraMetadata.CONTROL_AF_MODE_OFF) == true) {
            b.set(CaptureRequest.CONTROL_AF_MODE, CameraMetadata.CONTROL_AF_MODE_OFF)
        }
        if (canManualFocus) {
            b.set(CaptureRequest.LENS_FOCUS_DISTANCE, focusDiopter)
        }

        // ---- 变焦锁定为 1.0；crop region 必须与输出同纵横比 ----
        // 不能用"整个活动阵列"当 crop region：SCALER_CROP_REGION 的语义是
        // "把这块区域映射到输出"，若它与输出纵横比不同，映射就是各向异性的
        // （活动阵列 4:3 → 输出 16:9 = 画面纵向压扁 25%，fy = 0.75·fx）。
        // 后果不是"画质差一点"，而是内参的方形像元假设直接失效。
        requestCrop?.let { b.set(CaptureRequest.SCALER_CROP_REGION, it) }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            val zr: Range<Float>? = chars.get(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE)
            if (zr != null && zr.lower <= 1.0f && zr.upper >= 1.0f) {
                b.set(CaptureRequest.CONTROL_ZOOM_RATIO, 1.0f)
                appliedInfo.put("zoom_ratio_lock_applied", true)
            }
            // 区间下界 < 1.0 说明逻辑摄像头里含超广角，存在多摄切换导致内参跳变的风险
            if (zr != null) {
                appliedInfo.put("zoom_range_lower_below_one", zr.lower < 1.0f)
                appliedInfo.put("zoom_range", JSONArray().apply { put(zr.lower.toDouble()); put(zr.upper.toDouble()) })
            }
        }

        // ---- 防抖全关 ----
        val oisModes = chars.get(CameraCharacteristics.LENS_INFO_AVAILABLE_OPTICAL_STABILIZATION)
        if (oisModes?.contains(CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF) == true) {
            b.set(CaptureRequest.LENS_OPTICAL_STABILIZATION_MODE,
                CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF)
            appliedInfo.put("ois_off_applied", true)
        }
        val vsModes = chars.get(CameraCharacteristics.CONTROL_AVAILABLE_VIDEO_STABILIZATION_MODES)
        if (vsModes?.contains(CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF) == true) {
            b.set(CaptureRequest.CONTROL_VIDEO_STABILIZATION_MODE,
                CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF)
            appliedInfo.put("eis_off_applied", true)
        }

        // ---- 手动曝光：取区间内一个安全值，避免全黑/全白 ----
        val expRange: Range<Long>? = chars.get(CameraCharacteristics.SENSOR_INFO_EXPOSURE_TIME_RANGE)
        val isoRange: Range<Int>? = chars.get(CameraCharacteristics.SENSOR_INFO_SENSITIVITY_RANGE)
        val aeModes = chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_MODES)
        val canManualExposure = expRange != null && isoRange != null &&
                aeModes?.contains(CameraMetadata.CONTROL_AE_MODE_OFF) == true
        var expRequestedNs = -1L
        appliedInfo.put("manual_exposure_applied", canManualExposure)
        if (canManualExposure && expRange != null && isoRange != null) {
            val exp = 10_000_000L.coerceIn(expRange.lower, expRange.upper)
            val iso = 200.coerceIn(isoRange.lower, isoRange.upper)
            b.set(CaptureRequest.CONTROL_AE_MODE, CameraMetadata.CONTROL_AE_MODE_OFF)
            b.set(CaptureRequest.SENSOR_EXPOSURE_TIME, exp)
            b.set(CaptureRequest.SENSOR_SENSITIVITY, iso)
            requestedInfo.put("exposure_time_ns", exp)
            requestedInfo.put("sensitivity_iso", iso)
            expRequestedNs = exp
        }
        if (expRange != null) {
            requestedInfo.put("exposure_range_ns",
                JSONArray().apply { put(expRange.lower); put(expRange.upper) })
        }

        // ---- 帧率与帧时长 ----
        // 已证实的坑：CONTROL_AE_MODE=OFF 时 CONTROL_AE_TARGET_FPS_RANGE 被 HAL 忽略，
        // 帧间隔只由 SENSOR_FRAME_DURATION 决定。两者都下发，用回读值判定谁真生效。
        val fpsRanges: Array<Range<Int>>? =
            chars.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)
        val avail = JSONArray()
        fpsRanges?.forEach { r -> avail.put(JSONArray().apply { put(r.lower); put(r.upper) }) }
        requestedInfo.put("ae_available_fps_ranges", avail)

        requestedInfo.put("target_fps", targetFps)
        requestedInfo.put("frame_duration_requested", !suppressFrameDuration)
        if (suppressFrameDuration) {
            // 只有拍照交付档走这里：显式不发，让 sensor 按自己的读出节奏走完一张。
            // 报告里必须留痕，否则"skew 与 4:3 档相同"这种结论就不知道是在
            // "有/无帧时长约束"哪一种条件下得到的。
            requestedInfo.put("sensor_frame_duration_ns", JSONObject.NULL)
            val maxFrameDurationNs: Long? =
                chars.get(CameraCharacteristics.SENSOR_INFO_MAX_FRAME_DURATION)
            if (maxFrameDurationNs != null) {
                requestedInfo.put("max_frame_duration_ns", maxFrameDurationNs)
            }
        }
        if (targetFps > 0 && !suppressFrameDuration) {
            val maxFrameDurationNs: Long? =
                chars.get(CameraCharacteristics.SENSOR_INFO_MAX_FRAME_DURATION)
            var fd = 1_000_000_000L / targetFps.toLong()
            if (maxFrameDurationNs != null && fd > maxFrameDurationNs) fd = maxFrameDurationNs
            // 帧时长不得短于曝光时间，否则 HAL 会改写曝光或丢帧
            if (expRequestedNs > 0 && fd < expRequestedNs) fd = expRequestedNs
            b.set(CaptureRequest.SENSOR_FRAME_DURATION, fd)
            requestedInfo.put("sensor_frame_duration_ns", fd)
            if (maxFrameDurationNs != null) {
                requestedInfo.put("max_frame_duration_ns", maxFrameDurationNs)
            }
        }
        // AE 开启时这个档位才有意义；AE 关闭时它会被 HAL 忽略。
        // 默认不再下发，避免与 SENSOR_FRAME_DURATION 冲突（帧率权威性测验里会单独试）。
        if (legacyFpsRange) {
            fpsRanges?.firstOrNull { it.lower == it.upper }?.let { fixed ->
                b.set(CaptureRequest.CONTROL_AE_TARGET_FPS_RANGE, fixed)
                requestedInfo.put("ae_target_fps_range_sent",
                    JSONArray().apply { put(fixed.lower); put(fixed.upper) })
            }
        } else {
            requestedInfo.put("ae_target_fps_range_sent", JSONObject.NULL)
        }

        // ---- 白平衡锁定（只影响颜色，不影响几何，但为一致性也锁死）----
        val awbModes = chars.get(CameraCharacteristics.CONTROL_AWB_AVAILABLE_MODES)
        if (awbModes?.contains(CameraMetadata.CONTROL_AWB_MODE_OFF) == true) {
            b.set(CaptureRequest.CONTROL_AWB_MODE, CameraMetadata.CONTROL_AWB_MODE_OFF)
            appliedInfo.put("awb_off_applied", true)
        }

        // ---- 关掉会改变纹理/几何的 ISP 处理 ----
        val edgeModes = chars.get(CameraCharacteristics.EDGE_AVAILABLE_EDGE_MODES)
        if (edgeModes?.contains(CameraMetadata.EDGE_MODE_OFF) == true) {
            b.set(CaptureRequest.EDGE_MODE, CameraMetadata.EDGE_MODE_OFF)
            appliedInfo.put("edge_off_applied", true)
        }
        val nrModes = chars.get(CameraCharacteristics.NOISE_REDUCTION_AVAILABLE_NOISE_REDUCTION_MODES)
        if (nrModes?.contains(CameraMetadata.NOISE_REDUCTION_MODE_OFF) == true) {
            b.set(CaptureRequest.NOISE_REDUCTION_MODE, CameraMetadata.NOISE_REDUCTION_MODE_OFF)
            appliedInfo.put("noise_reduction_off_applied", true)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val abModes = chars.get(CameraCharacteristics.COLOR_CORRECTION_AVAILABLE_ABERRATION_MODES)
            if (abModes?.contains(CameraMetadata.COLOR_CORRECTION_ABERRATION_MODE_OFF) == true) {
                b.set(CaptureRequest.COLOR_CORRECTION_ABERRATION_MODE,
                    CameraMetadata.COLOR_CORRECTION_ABERRATION_MODE_OFF)
                appliedInfo.put("aberration_off_applied", true)
            }
        }

        // ---- 畸变矫正：显式关闭，确认拿到的是未去畸变图像（R2）----
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            val dcModes = chars.get(CameraCharacteristics.DISTORTION_CORRECTION_AVAILABLE_MODES)
            appliedInfo.put("distortion_correction_modes_available",
                JSONArray(dcModes?.map { CapabilityReport.distortionMode(it) } ?: emptyList<String>()))
            if (dcModes?.contains(CameraMetadata.DISTORTION_CORRECTION_MODE_OFF) == true) {
                b.set(CaptureRequest.DISTORTION_CORRECTION_MODE, CameraMetadata.DISTORTION_CORRECTION_MODE_OFF)
                appliedInfo.put("distortion_correction_off_applied", true)
            } else {
                appliedInfo.put("distortion_correction_off_applied", false)
            }
        }

        // ---- 零快门延迟：拍照路径显式关掉，并要求回读验证 ----
        // 为什么这是**拍照路径的专属风险**：ZSL 会持续在环形缓冲里留存最近若干帧，
        // 按下快门时 HAL 可以**把缓冲里那张旧的**交出来。那时 `SENSOR_TIMESTAMP` 指向
        // 的是一张更早的帧，而画面和通知看起来完全正常 —— 于是"这张照片的时刻"
        // 与"我按下的时刻"之间就出现了一个**不受我控制、且逐张随机**的偏移。
        // 对定位来说这是致命的（时间戳不再对应图像内容），所以必须关掉并回读确认。
        // 连续流（②③④⑤）里不存在这个风险，所以只在拍照路径下发。
        if (template == CameraDevice.TEMPLATE_STILL_CAPTURE) {
            try {
                b.set(CaptureRequest.CONTROL_ENABLE_ZSL, false)
                appliedInfo.put("zsl_off_requested", true)
            } catch (e: Exception) {
                appliedInfo.put("zsl_off_requested", false)
            }
        } else {
            appliedInfo.put("zsl_off_requested", JSONObject.NULL)
        }

        lockFields = listOf(
            "lens_focus_distance_diopter", "zoom_ratio", "ois_mode", "video_stab_mode",
            "sensor_exposure_time_ns", "sensor_sensitivity_iso", "sensor_pixel_mode",
            "sensor_frame_duration_ns", "rolling_shutter_skew_ns",
            "distortion_correction_mode", "edge_mode", "noise_reduction_mode",
            // 换镜会让上面几乎所有项同时跳变，所以它本身必须是一条锁定字段
            "active_physical_id",
            // 拍照路径的命门：它一旦被 HAL 打开，时间戳就不再等于曝光起点
            "control_enable_zsl"
        )
        return b.build()
    }

    /**
     * 取一块**与输出同纵横比**的最大居中区域作为 crop region。
     *
     * 为什么必须这么做：`SCALER_CROP_REGION` 的语义是"把这块活动阵列区域映射到输出"。
     * 若它的纵横比与输出不一致，映射就是各向异性的 —— 例如活动阵列 4096x3072 (4:3)
     * 映射到 1920x1080 (16:9)，水平缩放 0.46875、垂直缩放 0.35156，
     * 于是 fy = 0.75·fx。这会同时破坏两件事：
     *   ① 内参里 fx==fy（方形像元）的假设 —— 标定模型的前提没了
     *   ② 画面本身的几何 —— 目标在图像里是被压扁的
     * 而且它**不会报错**：画面看着"正常"，标定照样收敛，只有精度和模型前提悄悄错掉。
     *
     * 做法：以输出纵横比在活动阵列里取最大居中矩形，宽高按 2 对齐（避开 ISP 边界插值）。
     * 若纵横比本来就一致（例如输出选了 4:3 尺寸），则原样返回整个活动阵列。
     */
    private fun aspectSafeCrop(active: Rect, outW: Int, outH: Int): Rect {
        if (outW <= 0 || outH <= 0 || active.height() <= 0) return active
        val outAspect = outW.toDouble() / outH
        val actAspect = active.width().toDouble() / active.height()
        if (abs(outAspect - actAspect) / actAspect < 1e-3) return active

        val w: Int
        val h: Int
        if (outAspect > actAspect) {
            w = active.width()
            h = (w / outAspect).toInt()
        } else {
            h = active.height()
            w = (h * outAspect).toInt()
        }
        val w2 = (w and -2).coerceAtLeast(2)
        val h2 = (h and -2).coerceAtLeast(2)
        val left = active.left + (active.width() - w2) / 2
        val top = active.top + (active.height() - h2) / 2
        return Rect(left, top, left + w2, top + h2)
    }

    /**
     * 选采集尺寸。**优先与活动阵列同纵横比，其次才比面积。**
     *
     * 旧实现只按 `面积 ≤ 1920*1080` 取最大者，结果必然挑中 1920x1080 ——
     * 它的面积刚好等于上限，是满足条件里面积最大的，而它偏偏是 16:9。
     * 于是 SCALER_CROP_REGION（整个 4:3 活动阵列）与输出（16:9）纵横比不一致，
     * 映射变成**各向异性**：fx 与 fy 不再是同一个值，方形像元假设直接失效，
     * 而且白扔 25% 垂直视场、fx 被砍掉一半以上。
     * 这个 bug 不是"选亏了"，是"选错了"：内参模型的前提被破坏。
     *
     * 顺带记录全部同纵横比候选，便于后续按带宽/帧率权衡换档。
     */
    private fun pickCaptureSize(
        map: android.hardware.camera2.params.StreamConfigurationMap,
        chars: CameraCharacteristics
    ): Size {
        val sizes = map.getOutputSizes(outputFormat) ?: emptyArray()
        if (sizes.isEmpty()) throw IllegalStateException("无可用 ${formatName(outputFormat)} 输出尺寸")

        val active = chars.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
        val activeAspect = if (active != null && active.height() > 0)
            active.width().toDouble() / active.height() else null

        val sorted = sizes.sortedByDescending { it.width.toLong() * it.height.toLong() }
        val sameAspect = if (activeAspect != null) sorted.filter {
            it.height > 0 &&
                abs(it.width.toDouble() / it.height - activeAspect) / activeAspect < 0.01
        } else emptyList()

        val cand = JSONArray()
        (if (sameAspect.isNotEmpty()) sameAspect else sorted).forEach {
            cand.put(JSONObject().apply {
                put("w", it.width); put("h", it.height)
                put("same_aspect_as_active", sameAspect.contains(it))
            })
        }
        sizeCandidates = cand

        // 强制尺寸：⑤尺寸扫描用。仍然要求它真的在设备的候选清单里，
        // 否则会拿一个 HAL 不支持的大小去开 ImageReader，失败原因会变得莫名其妙。
        val forced = forcedCaptureSize
        if (forced != null) {
            val inList = sizes.any { it.width == forced.width && it.height == forced.height }
            sizeChoiceNote = if (inList)
                "强制尺寸 ${forced.width}x${forced.height}（⑤尺寸扫描 / 人工指定），非自动选档"
            else
                "强制尺寸 ${forced.width}x${forced.height} **不在**设备的 ${formatName(outputFormat)} 输出清单里" +
                    "（清单里没有这个大小）-> 本次结果不可用，须改选清单内的尺寸"
            cb?.onProgress(sizeChoiceNote)
            if (inList) return forced
        }

        sizeChoiceNote = if (sameAspect.isEmpty())
            "找不到与活动阵列同纵横比的输出尺寸 -> 回落为按面积取最大（fx==fy 不再有保证）"
        else
            "在 $CAPTURE_AREA_CAP_PX px² 上限内取同纵横比尺寸中面积最大者；" +
                "同纵横比候选共 ${sameAspect.size} 个（见 capture_config.size_candidates）"

        return sameAspect.firstOrNull { it.width.toLong() * it.height.toLong() <= CAPTURE_AREA_CAP_PX }
            ?: sameAspect.firstOrNull()
            ?: sorted.firstOrNull { it.width.toLong() * it.height.toLong() <= CAPTURE_AREA_CAP_PX }
            ?: sorted.last()
    }

    /**
     * 采集配置指纹 —— 只描述"**采集参数**"，不含 App 版本。
     *
     * 为什么必须有：③ 的逐轮报告会被当成"一次标定覆盖多次采集"的证据。但如果某一份
     * 报告是**旧 App / 旧输出尺寸**跑出来的，它在报告上看起来与新一轮一模一样，
     * 直到你逐字段比对才发现。实测踩过：一批 16 小时前、1920x1080（旧版 App）的
     * 报告被当成本轮证据，而判据里"输出尺寸=1 种"只在那 3 轮内部比较，看不见差异。
     *
     * ⚠️ v1.6 修正：**不要**把 APP_VERSION 掺进这个键（v1.5 掺了，是个设计错误）。
     * "采集配置不同"与"代码版本不同"是两个独立的问题，混在一个键里就会得出错误处置：
     * 实测踩过 —— ② 那份报告只是由**上一版 APK** 写的（输出尺寸其实与 ③ 完全相同），
     * 结果判据报"报告集跨配置：输出尺寸/App 版本不同，请重跑 ③"，
     * 而正确处置应该是"**重跑 ②**"。所以现在分两个键：
     *   - `config_fingerprint`（本函数）= 输出几何＋活动阵列＋crop＋格式＋预热帧数；
     *   - `app_version`（顶层另记）= 代码版本，作为**来源标记**单独比。
     *
     * 注意 skew **刻意不进**这个键：它是"输出纵横比 / crop"的派生量，
     * 掺进来会把"改档确实生效"误读成"配置发生了未授权变更"（方向正好反）。
     *
     * ⚠ v1.8 补一条：**模板与调度方式也刻意不进这个键**。它们单独记在
     * `capture_config.template_name` 与 `capture_config.single_shot` 上。
     * 理由正是本键的用途 —— ⑥ 要问的问题是"**同一套几何、同一格式，只换了
     * 调度方式与模板**之后，时间戳语义还一样吗"。若把模板/调度并进本键，
     * 那两边的键必然不等，就永远无法回答这个问题了：
     * 键相等才是"受控对照"的前提。
     */
    private fun configFingerprint(): String {
        val out = captureConfig.optJSONObject("output_size")
        val crop = captureConfig.optJSONObject("crop_region")
        val act = captureConfig.optJSONObject("active_array")
        val raw = StringBuilder()
            .append(out?.optInt("w", 0) ?: 0).append('x').append(out?.optInt("h", 0) ?: 0)
            .append("|a").append(act?.optInt("w", 0) ?: 0).append('x')
            .append(act?.optInt("h", 0) ?: 0)
            .append("|c")
            .append(
                if (crop == null) "none"
                else
                    "${crop.optInt("width")}x${crop.optInt("height")}" +
                        "@${crop.optInt("left")},${crop.optInt("top")}"
            )
            .append('|').append(captureConfig.optString("output_format", "?"))
            .append("|w").append(warmupFrames)
            .toString()
        // FNV-1a 32bit：够短、够稳定、零依赖（只是给"是不是同一套配置"一个速查键）
        var h = 0x811c9dc5L
        for (b in raw.toByteArray(Charsets.UTF_8)) {
            h = (h xor (b.toLong() and 0xff)) and 0xffffffffL
            h = (h * 0x01000193L) and 0xffffffffL
        }
        return "$raw #${h.toString(16).padStart(8, '0')}"
    }

    private fun buildReport(): JSONObject {
        val o = JSONObject()
        o.put("report_kind", reportKind)
        o.put("generated_at_wall_ms", System.currentTimeMillis())
        o.put("app_version", APP_VERSION)
        // 设备身份（v1.8.1 起每份报告自带）。
        //
        // 为什么必须自带：这整套判据都在回答"**这台机器**能不能当测量相机"，
        // 而在此之前只有①能力报告带 device —— ②③④⑤⑥ 的产出里没有任何字段
        // 说明它来自哪台机器。于是"两台的报告混在一起"这种错误，所有判据都
        // 看不见（配置指纹与采集路径可以完全一致）。判据原先只能拿"时间跨度"
        // 当代理，那既会漏（快速换机）也会误（同一台隔夜重跑）。
        // `build_fingerprint` 连固件一起锁住：换固件同样会改内参与读出行为。
        o.put("device", JSONObject().apply {
            put("manufacturer", Build.MANUFACTURER)
            put("model", Build.MODEL)
            put("device", Build.DEVICE)
            put("build_fingerprint", Build.FINGERPRINT)
            put("android_sdk", Build.VERSION.SDK_INT)
        })
        // 打开摄像头的重试留痕（v1.8.2）：设备级瞬时故障（onDisconnected/onError/
        // openCamera 抛异常）会按退避表重试。>1 说明本次采集不是"第一次打开就成功"，
        // 审阅证据时应当知道 —— 但它不改变采集语义，所以不进指纹、不加 rev。
        o.put("open_attempts", openAttempts)
        if (openErrors.isNotEmpty()) {
            o.put("open_retry_errors", JSONArray(openErrors.toList()))
        }
        // 采集路径修订号：顶层与 capture_config 各写一份 —— 顶层便于人看，
        // capture_config 里那份保证"配置"这个对象自包含。
        o.put("capture_path_id", capturePathId)
        o.put("capture_path_rev", capturePathRev)
        o.put("camera_id", cameraId)
        o.put("capture_size", readoutSize)
        captureConfig.put("app_version", APP_VERSION)
        captureConfig.put("capture_path_id", capturePathId)
        captureConfig.put("capture_path_rev", capturePathRev)
        captureConfig.put("config_fingerprint", configFingerprint())
        o.put("capture_config", captureConfig)
        o.put("requested", requestedInfo)
        o.put("applied", appliedInfo)
        o.put("params", JSONObject().apply {
            put("focus_distance_m", focusDistanceM.toDouble())
            put("target_fps", targetFps)
            put("target_frames", targetFrames)
            put("warmup_frames", warmupFrames)
            put("legacy_fps_range", legacyFpsRange)
        })
        o.put("frame_count", frames.size)
        o.put("frames", JSONArray(frames.toList()))
        o.put("stats", buildStats())
        return o
    }

    private fun buildStats(): JSONObject {
        val s = JSONObject()

        // 预热帧的几何不可信，统计一律排除
        val valid = frames.filter { !it.optBoolean("is_warmup", false) }
        s.put("total_frames", frames.size)
        s.put("warmup_frames", warmupFrames)
        s.put("valid_frames", valid.size)

        val tsList = valid.mapNotNull { fr ->
            val v = fr.opt("sensor_timestamp_ns")
            if (v == null || fr.isNull("sensor_timestamp_ns")) null else fr.optLong("sensor_timestamp_ns", -1L)
        }.filter { it > 0 }

        val durationS = if (tsList.size >= 2) (tsList.last() - tsList.first()) / 1e9 else 0.0
        s.put("capture_duration_s", durationS)

        // 帧间隔统计
        if (tsList.size >= 2) {
            val intervals = ArrayList<Long>(tsList.size - 1)
            for (i in 1 until tsList.size) intervals.add(tsList[i] - tsList[i - 1])
            val sorted = intervals.sorted()
            val mean = intervals.sum().toDouble() / intervals.size
            val variance = intervals.sumOf { (it - mean) * (it - mean) } / intervals.size
            val std = sqrt(variance)
            s.put("interval_ns", JSONObject().apply {
                put("n", intervals.size)
                // 单张模式下的"间隔"是**下发节奏**（shotIntervalMs），不是 sensor 帧率。
                // 不留这句话的话，下游会拿它当帧率判据，得出"抖动 0.001%"这种
                // 毫无意义的"优秀"结论 —— 它量的是我们自己的 sleep。
                if (singleShot) put("note",
                    "单张捕获模式：这里的间隔是应用下发节奏（shot_interval_ms），" +
                        "不是 sensor 帧率，不可用于帧率判据")
                put("mean", mean)
                put("min", sorted.first().toDouble())
                put("max", sorted.last().toDouble())
                put("std", std)
                put("jitter_pp_percent", if (mean > 0) std / mean * 100.0 else JSONObject.NULL)
                put("p95", sorted[(sorted.size * 95 / 100).coerceAtMost(sorted.size - 1)].toDouble())
            })
        }

        // 锁定稳定性：每个锁定字段的不同取值（同样只看有效帧）
        val lock = JSONObject()
        for (name in lockFields) {
            val values = valid.mapNotNull { fr ->
                if (!fr.has(name) || fr.isNull(name)) null else fr.get(name).toString()
            }
            val distinct = values.distinct()
            lock.put(name, JSONObject().apply {
                put("samples", values.size)
                put("distinct_count", distinct.size)
                put("values", JSONArray(distinct.take(10)))
                put("locked", distinct.size == 1 && values.size > 0)
            })
        }
        s.put("lock_stability", lock)

        // 请求值 vs 回读值：HAL 会对 focus/frame_duration 做量化或夹取，
        // 标定与断言都必须基于回读值，所以偏差本身要成为判据
        val firstValid = valid.firstOrNull() ?: frames.firstOrNull()
        if (firstValid != null) {
            s.put("requested_vs_actual", JSONObject().apply {
                put("focus_diopter", cmpPair(
                    requestedInfo.optDouble("focus_diopter", Double.NaN),
                    readDouble(firstValid, "lens_focus_distance_diopter")))
                put("frame_duration_ns", cmpPair(
                    requestedInfo.optLong("sensor_frame_duration_ns", -1L).takeIf { it > 0 }?.toDouble(),
                    readDouble(firstValid, "sensor_frame_duration_ns")))
                put("interval_ns", cmpPair(
                    requestedInfo.optLong("sensor_frame_duration_ns", -1L).takeIf { it > 0 }?.toDouble(),
                    s.optJSONObject("interval_ns")?.takeIf { it.has("mean") }?.optDouble("mean")))
                put("exposure_time_ns", cmpPair(
                    requestedInfo.optLong("exposure_time_ns", -1L).takeIf { it > 0 }?.toDouble(),
                    readDouble(firstValid, "sensor_exposure_time_ns")))
                put("sensitivity_iso", cmpPair(
                    requestedInfo.optInt("sensitivity_iso", -1).takeIf { it > 0 }?.toDouble(),
                    readDouble(firstValid, "sensor_sensitivity_iso")))
            })
        }

        // 帧时长控制是否真的生效。
        // 关键：不能只看"偏差百分比"。首轮请求 [12,12] 得到 33.29ms、本轮请求 33.333ms
        // 得到 33.288ms —— 两次请求相差 2.5 倍，回读值却完全一样，说明 HAL 根本没采纳请求值，
        // 我们看到的只是 sensor 的原生帧率。而按百分比判，后者偏差仅 0.14%，会被误判为 PASS。
        // 唯一可靠的判据是**回读值是否逐字等于请求值**。
        val fdReqNs = requestedInfo.optLong("sensor_frame_duration_ns", -1L)
        val fdActObj = valid.firstOrNull { it.has("sensor_frame_duration_ns") && !it.isNull("sensor_frame_duration_ns") }
        val fdActNs = fdActObj?.optLong("sensor_frame_duration_ns", -1L) ?: -1L
        s.put("frame_duration_control", JSONObject().apply {
            put("requested_ns", if (fdReqNs > 0) fdReqNs else JSONObject.NULL)
            put("actual_ns", if (fdActNs > 0) fdActNs else JSONObject.NULL)
            // 逐字相等 => 请求被采纳；否则 HAL 自行决定了帧时长
            put("honored_verbatim", fdReqNs > 0 && fdActNs == fdReqNs)
            put("legacy_fps_range_sent", legacyFpsRange)
            if (fdActNs > 0) put("actual_fps", 1e9 / fdActNs)
        })

        // 状态类字段（全帧，含预热，便于看清起始瞬态）
        s.put("states", JSONObject().apply {
            for (name in listOf("af_state", "ae_state", "lens_state", "af_mode", "ae_mode", "control_mode")) {
                val v = frames.mapNotNull {
                    if (it.isNull(name)) null else it.optString(name).takeIf { x -> x.isNotEmpty() }
                }
                put(name, JSONArray(v.distinct()))
            }
        })

        // 镜头机械状态时间线：区分"session 起始的机械收敛"与"全程持续漂移"
        val lensStates = frames.map { if (it.isNull("lens_state")) "" else it.optString("lens_state") }
        val movingIdx = lensStates.indices.filter { lensStates[it] == "MOVING" }
        s.put("lens_state_timeline", JSONObject().apply {
            put("sequence_head", JSONArray(lensStates.take(12)))
            put("moving_frame_indices", JSONArray(movingIdx))
            put("moving_count", movingIdx.size)
            put("moving_after_warmup", movingIdx.count { it >= warmupFrames })
            put("moving_all_within_warmup", movingIdx.isNotEmpty() && movingIdx.all { it < warmupFrames })
        })

        // 温度（电池温度代理；Camera2 不提供传感器温度）
        val temps = frames.mapNotNull { fr ->
            if (!fr.has("battery_temperature_c") || fr.isNull("battery_temperature_c")) null
            else fr.getDouble("battery_temperature_c")
        }
        s.put("temperature_c", if (temps.isEmpty()) JSONObject.NULL else JSONObject().apply {
            put("source", "battery")
            put("n", temps.size)
            put("first", temps.first())
            put("last", temps.last())
            put("min", temps.minOrNull() ?: 0.0)
            put("max", temps.maxOrNull() ?: 0.0)
            put("delta", (temps.maxOrNull() ?: 0.0) - (temps.minOrNull() ?: 0.0))
            put("sampling_duration_s", durationS)
        })

        // 卷帘读出时间汇总（逐帧可读，应恒定）
        val skews = valid
            .filter { it.has("rolling_shutter_skew_ns") && !it.isNull("rolling_shutter_skew_ns") }
            .map { it.optLong("rolling_shutter_skew_ns", -1L) }
            .filter { it > 0 }
        if (skews.isEmpty()) {
            s.put("rolling_shutter_skew_ns", JSONObject.NULL)
        } else {
            s.put("rolling_shutter_skew_ns", JSONObject().apply {
                put("n", skews.size)
                put("value", skews.first().toDouble())
                put("min", skews.minOrNull()?.toDouble() ?: 0.0)
                put("max", skews.maxOrNull()?.toDouble() ?: 0.0)
                put("distinct_count", skews.distinct().size)
            })
            // 把 skew 分解成"读出窗口行数 × 行时间"，让 PC 端不必再自己猜该除以谁。
            // 读出窗口行数 = crop_w / 输出纵横比（实测四个档位的不变量，见 onFrame 注释）。
            val skew0 = skews.first().toDouble()
            val outW = runCatching { captureConfig.getJSONObject("output_size").getInt("w") }.getOrNull()
            val outH = runCatching { captureConfig.getJSONObject("output_size").getInt("h") }.getOrNull()
            val cropW = runCatching { captureConfig.getJSONObject("crop_region").getInt("width") }.getOrNull()
            val cropH = runCatching { captureConfig.getJSONObject("crop_region").getInt("height") }.getOrNull()
            if (outW != null && outH != null && outW > 0 && outH > 0) {
                val readRows = (cropW ?: outW).toDouble() * outH / outW
                s.put("skew_readout_rows", JSONObject().apply {
                    put("value", readRows)
                    put("formula", "crop_w * outH / outW  (= crop_w / 输出纵横比)")
                    put("note", "按输出纵横比、取满 crop 宽度得到的读出窗口行数")
                })
                s.put("skew_line_time_ns", skew0 / readRows)
                // 两个被证伪的口径也一起记录，便于在报告里直接看见它们不恒定
                s.put("skew_per_output_row_ns", skew0 / outH)
                if (cropH != null && cropH > 0) s.put("skew_per_crop_row_ns", skew0 / cropH)
            }
        }

        // ---- v1.8：单张（拍照）路径专有的汇总 ----
        // 目的只有一个：把"这张照片的时间戳可不可信"从一句话变成几个数。
        if (singleShot) {
            s.put("shot_path", JSONObject().apply {
                put("shots_requested", targetFrames)
                put("shots_dispatched", shotsDispatched)
                put("serial_dispatch", true)
                put("shot_interval_ms", shotIntervalMs)
                put("frame_duration_requested", !suppressFrameDuration)
            })

            // ② 图与结果的配对核验（v1.8.1 起；v1.8.3 改为**离线按集合配对**）。
            // 回答的是这一句：「交付的这张图，确实是这个时间戳的那张吗？」
            //
            // 为什么必须离线做：图与结果是两条独立回调，先后不保证。在结果回调里查
            // "图到了没有"，会把**图比结果晚到**误判成丢图（第 12 轮实测：5 张"丢图"
            // 里 4 张的时间戳同时出现在"多余图"里 —— 真丢与晚到区分不开）。
            // 离线配对的做法：把图的时间戳存成集合，结算时逐帧问"集合里有它吗"。
            var matchedTotal = 0
            var matchedValid = 0
            var missingValid = 0
            var missingWarmup = 0
            for (fr in frames) {
                if (!fr.has("sensor_timestamp_ns") || fr.isNull("sensor_timestamp_ns")) continue
                val sts = fr.getLong("sensor_timestamp_ns")
                val isWarm = fr.optBoolean("is_warmup")
                if (imagesByTs.containsKey(sts)) {
                    val bytes = imagesByTs.remove(sts)
                    matchedTotal++
                    if (!isWarm) matchedValid++
                    fr.put("image_matched", true)
                    // EXIF 需要配对上的字节，所以读取也放在离线阶段（v1.8.3）
                    if (outputFormat == ImageFormat.JPEG && bytes != null) {
                        fr.put("jpeg", exifInfo(bytes))
                    }
                } else {
                    fr.put("image_matched", false)
                    if (isWarm) missingWarmup++ else missingValid++
                }
            }
            s.put("image_pairing", JSONObject().apply {
                put("pairing_key", "Image.timestamp == SENSOR_TIMESTAMP（同一时间基）")
                put("pairing_mode", "离线：结算时按图时间戳集合逐帧配对（与回调先后无关）")
                put("shots_dispatched", shotsDispatched)
                put("images_acquired", imagesAcquired)
                put("images_matched", matchedTotal)
                // **有效张**（交付张）与预热张分开记：预热张的图丢了不影响交付，
                // 但把它混进"丢图"会把一次正常的管线预热读成交付事故。
                put("images_matched_valid", matchedValid)
                put("images_missing", missingValid + missingWarmup)
                put("images_missing_valid", missingValid)
                put("images_missing_warmup", missingWarmup)
                // 到结算时仍未被取到的图（下发数 − 已取到 − 取到 null）。>0 = 还在飞，
                // 与"丢了"不同：等待窗口见 settle_wait_ms。
                put("images_pending", maxOf(0, shotsDispatched - imagesAcquired - acquireNullCount))
                // 没有任何结果认领的图（多余图）。>0 在离线配对下意味着真有图对不上
                // —— 那个时间戳根本没出现在任何结果的 SENSOR_TIMESTAMP 里。
                put("images_surplus", imagesByTs.size)
                put("surplus_ts", JSONArray(imagesByTs.keys.sorted().map { it }))
                put("settle_wait_ms", settleWaitMs)
                put("acquire_null", acquireNullCount)
                put("read_errors", imageReadErrors)
            })

            // ① 曝光→回调 的延迟：稳定 ⇒ 时间戳与 REALTIME 同轴、可用
            val lats = valid.mapNotNull {
                val v = if (it.has("callback_latency_ns") && !it.isNull("callback_latency_ns"))
                    it.optLong("callback_latency_ns", -1L) else -1L
                if (v > 0) v else null
            }
            if (lats.isNotEmpty()) {
                val mn = lats.min(); val mx = lats.max()
                val mean = lats.sum().toDouble() / lats.size
                val std = sqrt(lats.sumOf { (it - mean) * (it - mean) } / lats.size)
                s.put("exposure_to_callback_ns", JSONObject().apply {
                    put("n", lats.size); put("min", mn.toDouble())
                    put("max", mx.toDouble()); put("mean", mean); put("std", std)
                    put("spread", (mx - mn).toDouble())
                    put("spread_ms", (mx - mn) / 1e6)
                })
            }

            // ② 时间戳是否"跟得上"：相邻两张的 sensor 时间差 vs 回调时间差。
            // 两者一致 ⇒ 时间戳逐张推进，是"按下那一刻"的帧；
            // sensor 差明显小于回调差 ⇒ 交出的是缓冲里的旧帧（ZSL），时间戳与操作脱钩。
            val seq = valid.mapNotNull {
                val st = it.opt("sensor_timestamp_ns")
                val ce = it.opt("callback_elapsed_ns")
                if (st == null || it.isNull("sensor_timestamp_ns")) null
                else if (ce == null || it.isNull("callback_elapsed_ns")) null
                else Pair(it.optLong("sensor_timestamp_ns", -1L),
                          it.optLong("callback_elapsed_ns", -1L))
            }.filter { it.first > 0 && it.second > 0 }
            if (seq.size >= 3) {
                val dS = ArrayList<Long>(); val dC = ArrayList<Long>(); val diff = ArrayList<Long>()
                for (i in 1 until seq.size) {
                    val a = seq[i].first - seq[i - 1].first
                    val c = seq[i].second - seq[i - 1].second
                    dS.add(a); dC.add(c); diff.add(a - c)
                }
                val dmin = diff.min(); val dmax = diff.max()
                s.put("sensor_vs_callback_advance", JSONObject().apply {
                    put("n", diff.size)
                    put("sensor_step_min", dS.min().toDouble()); put("sensor_step_max", dS.max().toDouble())
                    put("callback_step_min", dC.min().toDouble()); put("callback_step_max", dC.max().toDouble())
                    put("diff_min", dmin.toDouble()); put("diff_max", dmax.toDouble())
                    put("spread_ms", (dmax - dmin) / 1e6)
                    put("max_abs_diff_ms", maxOf(abs(dmin), abs(dmax)) / 1e6)
                })
            }

            // ③ 时间戳是否早于下发（ZSL 旧帧的直接判据）
            val lag = valid.mapNotNull {
                val v = if (it.has("sensor_ts_minus_request_ns") &&
                    !it.isNull("sensor_ts_minus_request_ns"))
                    it.optLong("sensor_ts_minus_request_ns", 0L) else null
                v
            }
            if (lag.isNotEmpty()) {
                val neg = lag.count { it < 0 }
                s.put("sensor_ts_minus_request", JSONObject().apply {
                    put("n", lag.size); put("min", lag.min().toDouble())
                    put("max", lag.max().toDouble())
                    put("negative_count", neg)
                    put("all_capture_after_request", neg == 0)
                })
            }

            // ④ JPEG：EXIF 时间字段到底能不能用（有没有亚秒、几位）
            val exifs = valid.mapNotNull { it.optJSONObject("jpeg") }
            if (exifs.isNotEmpty()) {
                val digits = exifs.mapNotNull {
                    if (it.has("subsec_digits")) it.optInt("subsec_digits") else null
                }
                s.put("exif", JSONObject().apply {
                    put("n", exifs.size)
                    put("subsec_digits_values", JSONArray(digits.distinct().sorted()))
                    put("with_subsec", exifs.count { it.optBoolean("has_subsec") })
                    put("sample_datetime_original",
                        exifs.firstOrNull()?.opt("datetime_original") ?: JSONObject.NULL)
                    put("sample_offset_time_original",
                        exifs.firstOrNull()?.opt("offset_time_original") ?: JSONObject.NULL)
                    put("first_bytes",
                        exifs.firstOrNull()?.optInt("bytes", -1) ?: -1)
                })
            }
        }

        return s
    }

    private fun readDouble(o: JSONObject, key: String): Double? =
        if (!o.has(key) || o.isNull(key)) null else o.optDouble(key)

    /** 把一个请求值和一个回读值配成一条对比记录，附相对偏差。 */
    private fun cmpPair(requested: Double?, actual: Double?): Any {
        if (requested == null || actual == null) return JSONObject.NULL
        val dev = if (requested != 0.0) abs(actual - requested) / abs(requested) * 100.0 else 0.0
        return JSONObject().apply {
            put("requested", requested)
            put("actual", actual)
            put("deviation_pp_percent", dev)
        }
    }

    /** 电池温度（摄氏度）。Camera2 公共 API 没有传感器温度，只能用它作发热代理。 */
    private fun batteryTempC(): Double? {
        return try {
            val i = context.registerReceiver(
                null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            val t = i?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, Int.MIN_VALUE) ?: Int.MIN_VALUE
            if (t == Int.MIN_VALUE) null else t / 10.0
        } catch (e: Exception) {
            null
        }
    }

    private fun numOrNull(v: Any?): Any = v ?: JSONObject.NULL

    /**
     * 单张模式收尾：等末帧的图到达再结算（最多 [SETTLE_MAX_MS]）。
     *
     * 为什么必须等：交付物是"图 + 时间戳"，而图是先到的那个还是后到的那个不由我们定。
     * 实测第 12 轮：8 张下发、结果回调全部到齐时只取到 7 张图 ⇒ 若立刻结算，
     * 第 8 张会被记成"丢图"。它其实只是还在管道里。
     * 未到达的若超过等待窗口，则记 `images_pending` —— **"还没到"与"丢了"必须分开**。
     */
    private fun finishAfterImagesArrive() {
        if (finished || abort) return
        val start = SystemClock.elapsedRealtime()
        fun check() {
            if (finished || abort) return
            val arrived = imagesAcquired + acquireNullCount
            val waited = SystemClock.elapsedRealtime() - start
            if (arrived >= shotsDispatched || waited >= SETTLE_MAX_MS) {
                settleWaitMs = waited
                finish(buildReport(), null)
            } else {
                handler?.postDelayed({ check() }, SETTLE_POLL_MS)
            }
        }
        handler?.postDelayed({ check() }, SETTLE_POLL_MS)
    }

    private fun finish(report: JSONObject?, error: String?) {
        if (finished) return
        finished = true
        val c = cb
        val r = report
        val e = error
        close()
        c?.onDone(r, e)
    }
}

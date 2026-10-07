package com.mouse.phoneprobe

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import kotlin.math.abs

/**
 * M0 能力探测器。
 *
 * 做三件事：
 *  1. 静态能力探测 —— 导出 CameraCharacteristics 关键字段
 *  2. 锁定连拍验证 —— 锁参数连拍 N 帧，验证是否真锁住、帧率是否等于请求、
 *     请求值与回读值差多少、预热帧之后镜头是否还在动
 *  3. 重复性测试 —— 连续跑多轮（每轮重新开关摄像头），验证同一请求参数
 *     在跨会话下是否落在同一个回读值上。标定的可复现性靠这一项。
 *  4. 帧率权威性测验 —— 多个非默认档位扫描，判定帧率到底可控否（单点不可证伪）
 *  5. 尺寸扫描 —— 各候选输出尺寸的实际帧率与卷帘读出时间（输出尺寸是内参的一部分，
 *     且 fx ∝ 输出宽度，所以"定哪一档"直接决定精度上限，必须实测）
 *  6. 拍照路径 —— 单张捕获（`session.capture()` 一张一张）的时间戳语义、
 *     单帧几何/skew、以及 JPEG 的 EXIF 时间可用性。
 *     **②③④⑤ 全是连续流，交付却是拍照：路径不同则证据不可外推。**
 *
 * 结果写入 /sdcard/Android/data/com.mouse.phoneprobe/files/，供 adb pull。
 */
class MainActivity : Activity() {

    private lateinit var statusView: TextView
    private lateinit var logView: TextView
    private lateinit var captureButton: Button
    private lateinit var repeatButton: Button
    private lateinit var sweepButton: Button
    private lateinit var sizeSweepButton: Button
    private lateinit var stillShotButton: Button
    private lateinit var distInput: EditText
    private lateinit var fpsInput: EditText
    private lateinit var framesInput: EditText
    private var backCameraId: String? = null
    private var probe: LockedCaptureProbe? = null
    // ---- M1 交付会话（D24：连续流 + 抽帧交付）----
    private lateinit var hostInput: EditText
    private lateinit var tokenInput: EditText
    private lateinit var scanButton: Button
    private lateinit var connectButton: Button
    private lateinit var disconnectButton: Button
    private lateinit var startDeliveryButton: Button
    private lateinit var stopDeliveryButton: Button
    private lateinit var linkTestButton: Button
    /** 留存的实时账本（应有/实存/两个缺口/写入速率/还能录多久）。
     *  ⚠ 第 24 轮**合并**后不再有"归档道开关"：交付道的语义就是"每一帧都留"。 */
    private lateinit var storeInfoView: TextView
    private lateinit var zoom06Button: Button
    private lateinit var zoom10Button: Button
    /** 本机变焦档位（连接后交给 DeliverySession；默认 0.6 = 定位需求）。 */
    private var zoomChoiceLocal = 0.6
    /** M0 探针（①③④⑤⑥）整块 —— 默认收起，见 buildUi 里的说明。 */
    private lateinit var probeSection: LinearLayout
    private lateinit var probeToggle: Button
    private lateinit var previewView: android.widget.ImageView
    private lateinit var previewInfo: TextView
    private var delivery: DeliverySession? = null
    private val ui = Handler(Looper.getMainLooper())

    /**
     * ⑤尺寸扫描要强制使用的采集尺寸（null = 自动选档）。
     *
     * 输出尺寸是内参的一部分，而自动选档依赖一个人为的面积上限 —— 那个上限
     * （2048x1536 = 3.15 MP）本身没有实测依据，是"感觉够用"定的。
     * fx 与输出宽度成正比（1920 -> 1282.5 px，4096 -> 2736 px），
     * 所以这个上限直接决定了亚像素定位的精度上限，不能靠感觉定。
     */
    private var forcedSize: android.util.Size? = null

    private var repeatRound = 0
    private val repeatRounds = 3
    private val repeatSummary = ArrayList<String>()

    // ---- 帧率权威性测验 ----
    /** 请求档位。刻意全部避开默认帧率附近，只有离默认值足够远才可能看出控制是否生效。 */
    private val sweepRates = listOf(12, 15, 24, 60)
    private val sweepFrames = 12
    private val sweepWarmup = 3
    private val sweepTrials = ArrayList<JSONObject>()
    private var sweepIndex = 0

    /** 单轮重复性测试关心的量。跨会话是否落在同一回读值上，靠这些数说话。 */
    private data class RoundStat(
        val focusActual: Double?,
        val focusDeviationPercent: Double?,
        val frameDurationMs: Double?,
        val movingAfterWarmup: Int
    )
    private val roundStats = ArrayList<RoundStat>()

    /** 日志保留上限：超过 LOG_MAX 行时一次裁到 LOG_KEEP 行（避免每行裁一次）。 */
    private val LOG_MAX = 400
    private val LOG_KEEP = 300

    private val capabilityFile = "m0_capability.json"
    private val captureFile = "m0_locked_capture.json"
    private val sweepFile = "m0_rate_sweep.json"
    private val sizeSweepFile = "m0_size_sweep.json"
    private val stillShotFile = "m0_still_shot.json"

    // ---- ⑥ 拍照路径 ----
    /**
     * 一条拍照路径的配置。
     *
     * **为什么必须做这一步**：②③④⑤ 全部走 `TEMPLATE_MANUAL` + `setRepeatingRequest`
     * 的连续流（YUV），而交付要求是"回传使用拍照"。模板、输出格式、调度方式三样
     * 都不同 —— 其中任何一样都可能改变时间戳的语义（`TEMPLATE_STILL_CAPTURE`
     * 默认可能开启零快门延迟/多帧合成，那时"这张照片的时刻"就不再是单值）。
     * **集成路径变了就是证据基础变了**：既有 PASS 一条也不能外推过来。
     *
     * 两条路径各司其职：
     *  - 对照档：与 ② **同几何、同格式、同模板**，**只**把"重复请求"换成"单张捕获"
     *    ⇒ 构成一次只动一个变量的受控实验，用来回答"既有证据能否外推到单张"
     *  - 交付档：真正要用的那条（JPEG + `TEMPLATE_STILL_CAPTURE` + 全幅）
     *    ⇒ 量它自己的 skew、几何，以及 EXIF 时间到底能不能用
     */
    private data class StillTrial(
        val id: String,
        val label: String,
        val format: Int,
        val template: Int,
        val forcedSize: android.util.Size?,
        val suppressFrameDuration: Boolean
    )

    private val stillShots = 8

    /**
     * 单张路径的预热张数。
     *
     * **必须与 ② 的 `warmupFrames` 相同**，否则⑥的"对照档"就不是单变量对照了：
     * 它相对 ② 会同时差"单张 vs 重复请求"和"丢弃规则"两件事，于是
     * 「单张不改变读出模式」这个结论无法严格归因。
     *
     * v1.8.0 取 1 是随手写的，结果第六轮实测报告里两份指纹差在 `w1` vs `w5`
     * 这一段 —— 判据只能给出"不止动了一个变量"。预热帧数本身不进入下发给 HAL
     * 的请求，理论上不影响读出模式；但**对照实验的纪律是只动一个变量**，
     * 不靠"理论上不影响"来省事。
     */
    private val stillWarmup = 5
    private val stillIntervalMs = 500L
    private val stillTrials = ArrayList<JSONObject>()
    private var stillIndex = 0
    private var stillTargets = ArrayList<StillTrial>()

    // ---- ⑤ 尺寸扫描 ----
    /**
     * 要扫的输出宽度阶梯。每档解析成设备候选清单里"同纵横比、宽度不超过该值且最接近"的那个尺寸。
     * 1920 = 现用档；2880/4096 = 精度更高但带宽更大的候选。fx 大致与宽度成正比。
     */
    private val sizeSweepWidths = listOf(1920, 2880, 4096)
    private val sizeSweepFrames = 45
    private val sizeSweepWarmup = 5
    private val sizeSweepTrials = ArrayList<JSONObject>()
    private var sizeSweepIndex = 0
    private var sizeSweepTargets = ArrayList<SizeTrial>()

    /**
     * 一个扫描档位 = 输出尺寸 + 是否强制全阵列 crop。
     *
     * 为什么需要 forceFullCrop：skew 的归属有两个互相竞争的假设（输出纵横比 vs crop 高度），
     * 而已有数据里两者总是同时变 —— 16:9 输出会让 `aspectSafeCrop` 把 crop 从 3072 收紧到
     * 2304，于是"16:9 ⇒ skew 6.91 ms"既可以用"纵横比"解释，也可以用"crop 高度"解释。
     * 加一条 **16:9 输出 + 全阵列 crop** 的档，就构成"一次只动一个变量"的受控实验：
     *   - skew 仍 6.91 ms ⇒ 由**输出纵横比**决定；
     *   - skew 变 9.21 ms ⇒ 由 **crop 高度**决定。
     */
    private data class SizeTrial(
        val size: android.util.Size,
        val forceFullCrop: Boolean,
        val label: String
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildUi()

        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            statusView.text = "等待相机权限…"
            requestPermissions(arrayOf(Manifest.permission.CAMERA), REQ_CAMERA)
        } else {
            runCapabilityProbe()
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_CAMERA) {
            if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                runCapabilityProbe()
            } else {
                statusView.text = "未授予相机权限，无法探测"
            }
        }
    }

    override fun onDestroy() {
        ui.removeCallbacksAndMessages(null)
        probe?.close()
        super.onDestroy()
    }

    // ---------------- 扫码配对 ----------------

    /**
     * 进入扫码界面。
     *
     * 采集运行中直接拦掉：扫码要开后摄，而采集正握着它 ⇒ 必然"相机打不开"。
     * 在入口说清楚，比让人对着黑屏猜要好（相机被占的错误也留着兜底，
     * 因为 M0 探针也可能会握着相机）。
     */
    private fun startScan() {
        if (delivery?.isCapturing == true) {
            log("✗ 正在采集，后摄被占用 ⇒ 请先「停流」再扫码")
            setStatus("先停流再扫码（后摄被采集占用）")
            return
        }
        startActivityForResult(Intent(this, QrScanActivity::class.java), REQ_SCAN)
    }

    /**
     * 扫码结果 → 解析 → 回填 → 连接。
     *
     * **扫码与手输共用 [PairCode] 这一个解析器**，所以这里没有任何"扫码专用"的宽容处理：
     * 解析器说什么就是什么。这样"扫到的能连、手输同一串也应该能连"是结构上成立的，
     * 而不是靠两处代码碰巧写得一样。
     *
     * 解析失败**必须把原因说出来**：扫到无关二维码时，用户需要知道"这不是配对码"，
     * 而不是面对一个不动的地址框。
     */
    @Suppress("DEPRECATION")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != REQ_SCAN) return
        val err = data?.getStringExtra(QrScanActivity.EXTRA_ERROR)
        if (resultCode != RESULT_OK) {
            if (err != null) {
                log("✗ 扫码未成：$err")
                setStatus(err)
            } else {
                log("已取消扫码")
            }
            return
        }
        val text = data?.getStringExtra(QrScanActivity.EXTRA_TEXT)
        if (text.isNullOrBlank()) {
            log("✗ 扫码返回了空内容")
            setStatus("扫码返回空内容")
            return
        }
        // 原文照记：解析出问题时，"扫到的到底是什么"是唯一的线索。
        log("扫码内容：$text")
        val pc = PairCode.parse(text)
        if (!pc.ok) {
            log("✗ ${pc.reason}")
            setStatus("不是配对码：${pc.reason}")
            return
        }
        val old = hostInput.text.toString().trim()
        hostInput.setText(pc.target())
        if (pc.token.isNotEmpty()) tokenInput.setText(pc.token)
        pc.warnings.forEach { log("  ⚠ $it") }
        log("✓ 配对码：${pc.describe()}" +
            if (old.isNotEmpty() && old != pc.target()) "（已覆盖原地址「$old」）" else "")
        if (pc.token.isEmpty()) {
            if (tokenInput.text.toString().isNotBlank()) {
                log("  ⚠ token 框里还是上一次的值，确认它属于这台 PC")
            }
            setStatus("已填入 ${pc.target()}，但码里没 token ⇒ 请在 token 框手输")
            return
        }
        if (delivery?.connected == true) {
            log("⚠ 当前已连接 ⇒ 只回填不重连（免得把正在跑的会话换掉）")
            setStatus("已填入配对码；要换目标请先「断开」")
            return
        }
        log("⇒ 自动连接（扫码即连；目标不对可点「断开」后手改）")
        connectButton.performClick()
    }

    // ---------------- UI ----------------

    private fun buildUi() {
        val dp = { v: Int -> (v * resources.displayMetrics.density).toInt() }
        val screenH = resources.displayMetrics.heightPixels
        // 等分一行的两个按钮
        val row = { _: View -> LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f) }
        fun lp(): LinearLayout.LayoutParams = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(14), dp(10), dp(14), dp(6))
            setBackgroundColor(Color.parseColor("#FFFFFF"))
        }

        root.addView(TextView(this).apply {
            text = "M0 探针 / M1 交付"
            textSize = 18f
            setTextColor(Color.parseColor("#111111"))
            setTypeface(typeface, Typeface.BOLD)
        })

        statusView = TextView(this).apply {
            text = "初始化…"
            textSize = 13f
            setTextColor(Color.parseColor("#444444"))
            setPadding(0, dp(4), 0, dp(6))
        }
        root.addView(statusView)

        // ============ 预览（**放最上面**） ============
        // 原来它插在"地址栏"与"连接按钮"之间、固定 240dp，把小屏上的
        // 连接/开流/停流按钮整个挤出屏幕 —— 现场正是要靠这几个按钮操作，
        // 却看不见。现在：预览置顶 + 高度封顶（不超过屏幕 30%），
        // 保证下面的按钮永远在首屏内。
        previewView = android.widget.ImageView(this).apply {
            adjustViewBounds = true          // 按采集纵横比缩放（1920×1440 等）
            setBackgroundColor(Color.parseColor("#111111"))
            setMaxHeight(minOf(dp(260), (screenH * 0.30).toInt()))
            minimumHeight = dp(140)          // 还没出画面时也占位，避免按钮位置跳动
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
        }
        root.addView(previewView)
        previewInfo = TextView(this).apply {
            text = "预览：开流后显示已采画面（灰度 Y 平面 + 中心十字，约 8 fps）" +
                " —— 纯显示，不进 HAL 请求/元数据/指纹"
            textSize = 10f
            setTextColor(Color.parseColor("#999999"))
            setPadding(0, dp(2), 0, dp(8))
        }
        root.addView(previewInfo)

        // ============ 交付会话 ============
        root.addView(TextView(this).apply {
            text = "交付会话（连 PC，按 sensor_timestamp 抽帧回传）"
            textSize = 12f
            setTextColor(Color.parseColor("#111111"))
            setTypeface(typeface, Typeface.BOLD)
        })
        root.addView(TextView(this).apply {
            text = "PC 先跑 pc/server.py；**扫码配对**可自动填地址与 token，" +
                "也可把打印的【首选】那一行整行（含 token）粘到地址框。" +
                "⚠ 别用标着「虚拟网卡」的地址。"
            textSize = 10f
            setTextColor(Color.parseColor("#999999"))
            setPadding(0, dp(2), 0, dp(4))
        })
        val netRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        hostInput = EditText(this).apply {
            hint = "PC 地址（可粘整行 http://ip:port/?token=…）"
            textSize = 14f
        }
        tokenInput = EditText(this).apply {
            hint = "token"
            textSize = 14f
        }
        netRow.addView(hostInput, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 2f))
        netRow.addView(tokenInput, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        root.addView(netRow)

        // 「扫码配对」单独一行：扫码是**填表的手段**，不是第三个连接动作，
        // 所以不跟"连接/断开"挤在一行 —— 挤在一起会让人以为它也是"连/断"的一种。
        scanButton = Button(this).apply {
            text = "扫码配对"
            setOnClickListener { startScan() }
            // 长按 = **真机解析自检**（跑 PairCode.SELFTEST 的正反用例，结果进日志）。
            // 为什么要这个入口：现场"扫码没反应"可能是三件事（相机没通 / 码没对准 /
            // 解析器坏了），长按一下就能把第三件排除掉，不必回来改代码重编一轮。
            setOnLongClickListener {
                val (total, fails) = PairCode.runSelfTest()
                log("配对码解析自检：$total 条用例，失败 ${fails.size} 条")
                fails.forEach { log("  ✗ $it") }
                if (fails.isEmpty()) {
                    log("  ✓ 全部通过（其中反例 ${PairCode.SELFTEST.count { !it.ok }} 条：" +
                        "必须被拒的都拒了）")
                }
                setStatus(if (fails.isEmpty()) "解析自检 $total/$total 通过"
                          else "解析自检失败 ${fails.size} 条（见日志）")
                true
            }
        }
        val scanRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        scanRow.addView(scanButton, row(scanButton))
        root.addView(scanRow)
        root.addView(TextView(this).apply {
            text = "扫码 = 读 PC 面板「扫码配对」里的二维码或终端里打印的那个，" +
                "自动填好地址与 token 并连接（长按「扫码配对」= 跑解析自检）"
            textSize = 10f
            setTextColor(Color.parseColor("#999999"))
            setPadding(0, dp(2), 0, dp(4))
        })

        // 连接 / 断开 = **两个独立按钮**。
        // 旧实现是"一个按钮改文字"：点一下写「连接中…」，而成功分支只改了状态栏、
        // 没把文字改成「断开」⇒ **连上之后根本看不到断开入口**（实测踩到）。
        // 用两个按钮就没有这种"状态藏在文字里"的问题。
        val connRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        connectButton = Button(this).apply {
            text = "连接 PC"
            setOnClickListener {
                val d = delivery ?: DeliverySession(
                    this@MainActivity,
                    onLog = { s -> ui.post { log(s) } },
                    onStatus = { st -> ui.post { setStatus(
                        "交付[${st.optString("state")}] 队列=${st.optInt("queue_len")}帧 " +
                        // **"全部回传完成"唯一看得见的地方**：drained = 队列已清空、本会话
                        // 抽的帧全部 verified。它必须与 idle（本来就没活）分开，
                        // 否则"传完了"和"什么都没发生"长得一样。
                        "收尾=${drainLabel(st.optString("drain_state"))} " +
                        // 档位与生效镜头放在最前：换档=换内参，"这段数据是哪颗镜头拍的"是首要信息
                        "镜头${st.optString("active_physical_id")}(" +
                        "zoom=${st.optDouble("zoom_effective")}/${st.optString("zoom_source")}) " +
                        // 上传速率与积压速度是这轮新增的**关键两个数**：
                        // "上传 5.5 MB/s + 积压 +3 帧/s" 一眼就知道交付跟不上采集。
                        "上传=${st.optDouble("upload_mbps")}MB/s " +
                        "积压=${st.optDouble("backlog_fps")}帧/s " +
                        "温度=${st.optDouble("temperature_c")}C " +
                        // ⚠ "已留存"≠"已回传"：一个是写进手机本地的，一个是 PC 已校验的。
                        "已留存=${st.optInt("frames_delivered")} " +
                        "已回传=${st.optInt("uploaded_frames")}"); setStoreLine(st) } },
                    onSessionId = { sid -> ui.post { setStatus("session=$sid") } },
                    // 连不上必须**说出来**：把原因写到状态栏 + 日志，按钮复位
                    // 连接结果 / 连接丢失都走这里。**文案由 DeliverySession 给全**
                    //（"连接失败：…" / "连接已断开：…"），界面直接显示，不再自己拼前缀
                    // —— 否则连接丢失会显示成"连接失败"，把两种情形说成一回事。
                    onConnectResult = { ok, why -> ui.post {
                        setDeliveryUiState(if (ok) "on" else "off")
                        setStatus(why)
                    } },
                    onPreview = { bmp, info -> ui.post {
                        previewView.setImageBitmap(bmp)
                        previewInfo.text = info
                    } }
                ).also { delivery = it }
                d.setZoom(zoomChoiceLocal)      // 把本机档位交给会话（默认 0.6）
                setDeliveryUiState("connecting")
                d.connect(hostInput.text.toString(), tokenInput.text.toString())
            }
        }
        disconnectButton = Button(this).apply {
            text = "断开"
            isEnabled = false
            setOnClickListener {
                delivery?.disconnect()
                setDeliveryUiState("off")
                setStatus("已断开")
            }
        }
        connRow.addView(connectButton, row(connectButton))
        connRow.addView(disconnectButton, row(disconnectButton))
        root.addView(connRow)

        val capRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        startDeliveryButton = Button(this).apply {
            text = "开流交付"
            isEnabled = false
            // 本地开流走 startCaptureLocal()：若当前会话号是 PC 上一次 start 分配的，
            // 先换一个新的（否则这批帧会被上传进 PC 那边已结束的会话目录）。
            setOnClickListener { delivery?.startCaptureLocal() }
        }
        stopDeliveryButton = Button(this).apply {
            text = "停流"
            isEnabled = false
            setOnClickListener { delivery?.stopCapture() }
        }
        linkTestButton = Button(this).apply {
            text = "链路测速"
            isEnabled = false
            // 20 MB 的纯上传测速：**不开相机**。用来回答"是链路慢还是采集循环慢"——
            // 现场实测交付只有 5.5 MB/s，而回环协议天花板是 43 MB/s，必须分清这两者。
            setOnClickListener { delivery?.linkTest(20) }
        }
        capRow.addView(startDeliveryButton, row(startDeliveryButton))
        capRow.addView(stopDeliveryButton, row(stopDeliveryButton))
        capRow.addView(linkTestButton, row(linkTestButton))
        root.addView(capRow)

        // ---- 本地留存：**每一帧都留**（第 24 轮合并：不再有"交付道 / 归档道"之分）----
        //
        // 为什么**不再给开关**：用户口径就是"每一帧都留"，抽帧已是历史写法。
        // 但它必须**一直被看清** —— 全帧率 83 MB/s，21 GB 空间大约只能录 4 分钟；
        // 写盘负载还可能拖慢相机回调。降载只能靠 PC 下发的 `plan.interval_ms`，
        // 一旦启用，账本里的 `every_frame` 会变 false（语义变化必须一眼可见）。
        val storeRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val storeHintButton = Button(this).apply {
            text = "留存说明"
            textSize = 12f
            setOnClickListener {
                log("留存：采集期间把**每一帧**的原始 Y 平面写手机本地（不抽帧、不转码），")
                log("  同时边采边传；传不完的停采后继续排空（分片 + 断点续传，幂等）。")
                log("  ⚠ 可录时长 = 手机可用空间 ÷ 实测写入速率（本机满帧率约 4 分钟/21 GB）。")
                log("  ⚠ 剩余空间低于余量保护会**自动停留存并明说原因**（已存下的帧照常回传）。")
                log("  ⚠ 两个缺口必须分开看：采集缺口**只能重采**，回传缺口**可以重传**。")
                log("  ⚠ 不新增 ImageReader、不改任何 HAL 请求 ⇒ 采集路径 rev 不变；")
                log("     但写盘是真负载，可能拖慢回调 ⇒ frames_seen 与 store.frames_expected 一起看。")
            }
        }
        storeRow.addView(storeHintButton, row(storeHintButton))
        root.addView(storeRow)
        storeInfoView = TextView(this).apply {
            textSize = 10f
            setTextColor(Color.parseColor("#666666"))
        }
        root.addView(storeInfoView)

        // ---- 变焦档位（0.6× 超广角 / 1.0× 主摄）----
        // 用户需求：定位默认用 0.6×（视场大 1.6 倍，好摆位）；必须能切回 1.0×（已标定）。
        root.addView(TextView(this).apply {
            text = "变焦档位（换档 = 换镜头 = 换内参：运行中切换会自动换新会话号）"
            textSize = 11f
            setTextColor(Color.parseColor("#666666"))
            setPadding(0, dp(6), 0, dp(2))
        })
        val zoomRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        zoom06Button = Button(this).apply {
            textSize = 12f
            setOnClickListener { pickZoom(0.6) }
        }
        zoom10Button = Button(this).apply {
            textSize = 12f
            setOnClickListener { pickZoom(1.0) }
        }
        zoomRow.addView(zoom06Button, row(zoom06Button))
        zoomRow.addView(zoom10Button, row(zoom10Button))
        root.addView(zoomRow)
        root.addView(TextView(this).apply {
            text = "0.6× = 超广角（定位默认；内参与主摄不同，需单独标定）；" +
                "1.0× = 主摄（已标定）。PC 若在 profile 里显式写了 zoom_ratio，则以后者为准。"
            textSize = 10f
            setTextColor(Color.parseColor("#999999"))
        })
        root.addView(TextView(this).apply {
            text = "也可由 PC 面板点「开始采集」远程下发 start（含抽帧间隔）。" +
                "队列只在收到服务端 verified 后才删除。"
            textSize = 10f
            setTextColor(Color.parseColor("#999999"))
            setPadding(0, 0, 0, dp(8))
        })

        // ============ M0 探针（默认收起） ============
        // 为什么不删掉：这六个按钮是 **M0 证据链的采集入口**（①②③④⑤⑥），
        // 而 M0 的结论全部挂在"哪份报告对应哪条采集路径 (id, rev)"上（tools/capture_path_versions.json）。
        // 代码删了就再也采不出等价证据 —— 将来只要动一次采集行为、或需要给第二台机器复测，
        // 就得把这套探针重写一遍。所以这里是**收起**（默认看不见、要点一下才展开），
        // 而不是删除；界面上不再占位置，功能随时可复采。
        probeToggle = Button(this).apply {
            text = "▸ 展开 M0 探针（①③④⑤⑥，复采用）"
            textSize = 12f
            setOnClickListener {
                val show = probeSection.visibility != View.VISIBLE
                probeSection.visibility = if (show) View.VISIBLE else View.GONE
                text = if (show) "▾ 收起 M0 探针（①③④⑤⑥）"
                       else "▸ 展开 M0 探针（①③④⑤⑥，复采用）"
            }
        }
        root.addView(probeToggle, lp())
        probeSection = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
        }

        probeSection.addView(TextView(this).apply {
            text = "工作距离(米)        帧率        帧数"
            textSize = 11f
            setTextColor(Color.parseColor("#666666"))
        })
        val params = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        distInput = EditText(this).apply {
            setText("1.0")
            textSize = 14f
            hint = "米"
        }
        fpsInput = EditText(this).apply {
            setText("30")
            textSize = 14f
        }
        framesInput = EditText(this).apply {
            setText(LockedCaptureProbe.DEFAULT_FRAMES.toString())
            textSize = 14f
        }
        for (v in listOf<EditText>(distInput, fpsInput, framesInput)) {
            params.addView(v, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        }
        probeSection.addView(params)
        probeSection.addView(TextView(this).apply {
            text = "工作距离留空或填 0 = 退回超焦距泛焦（近处目标会整体虚掉）"
            textSize = 10f
            setTextColor(Color.parseColor("#999999"))
            setPadding(0, 0, 0, dp(6))
        })

        captureButton = Button(this).apply {
            text = "① 静态能力探测完成"
            isEnabled = false
            setOnClickListener {
                forcedSize = null      // ②③④⑤ 一律自动选档，只有⑤才强制尺寸
                runLockedCaptureProbe(captureFile)
            }
        }
        probeSection.addView(captureButton, lp())

        repeatButton = Button(this).apply {
            text = "③ 重复性测试（连续 $repeatRounds 轮，每轮重开摄像头）"
            isEnabled = false
            setOnClickListener { startRepeatabilityTest() }
        }
        probeSection.addView(repeatButton, lp())

        sweepButton = Button(this).apply {
            text = "④ 帧率权威性测验（判定帧率到底可控否）"
            isEnabled = false
            setOnClickListener { startRateSweep() }
        }
        probeSection.addView(sweepButton, lp())

        sizeSweepButton = Button(this).apply {
            text = "⑤ 尺寸扫描（各候选尺寸的实际帧率与卷帘读出时间）"
            isEnabled = false
            setOnClickListener { startSizeSweep() }
        }
        probeSection.addView(sizeSweepButton, lp())

        stillShotButton = Button(this).apply {
            text = "⑥ 拍照路径（单张捕获：时间戳语义 + EXIF 实测）"
            isEnabled = false
            setOnClickListener { startStillShot() }
        }
        probeSection.addView(stillShotButton, lp())

        probeSection.addView(TextView(this).apply {
            text = "②③④⑤ 是连续流（重复请求）；⑥ 才是「拍照」。交付路线见方案 D24（连续流+抽帧）；" +
                "若改走拍照交付，必须重跑 ⑥ —— 模板/格式/调度方式任一不同，时间戳语义都可能不同。"
            textSize = 10f
            setTextColor(Color.parseColor("#999999"))
            setPadding(0, dp(2), 0, dp(6))
        })
        root.addView(probeSection, lp())

        val scroll = ScrollView(this).apply {
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f)
            setPadding(0, dp(8), 0, 0)
        }
        logView = TextView(this).apply {
            text = ""
            textSize = 11f
            setTextColor(Color.parseColor("#222222"))
            typeface = Typeface.MONOSPACE
        }
        scroll.addView(logView)
        root.addView(scroll)

        updateZoomButtons()
        setDeliveryUiState("off")
        setContentView(root)
    }

    /** 选择变焦档位：本地记一份（连接前就能选），连上后同步给会话。 */
    private fun pickZoom(z: Double) {
        zoomChoiceLocal = z
        updateZoomButtons()
        delivery?.setZoom(z)
        if (delivery?.connected != true) setStatus("变焦档位已选 ${zoomLabel(z)}（开流时生效）")
    }

    private fun zoomLabel(z: Double) = if (z < 0.9) "0.6× 超广角" else "1.0× 主摄"

    private fun updateZoomButtons() {
        zoom06Button.text = (if (zoomChoiceLocal < 0.9) "● " else "○ ") + "0.6× 超广角"
        zoom10Button.text = (if (zoomChoiceLocal < 0.9) "○ " else "● ") + "1.0× 主摄"
    }

    /**
     * 交付区按钮的可用状态：**"现在能做什么"必须由状态决定，不能靠用户猜**。
     *  - off        ：只能连（断开/开流/停流都无意义）
     *  - connecting ：可以取消（断开）
     *  - on         ：可以断开、开流、停流；再连无意义
     */
    private fun setDeliveryUiState(state: String) {
        when (state) {
            "connecting" -> {
                connectButton.text = "连接中…"
                connectButton.isEnabled = false
                disconnectButton.isEnabled = true
                startDeliveryButton.isEnabled = false
                stopDeliveryButton.isEnabled = false
                linkTestButton.isEnabled = false
            }
            "on" -> {
                connectButton.text = "连接 PC"
                connectButton.isEnabled = false
                disconnectButton.isEnabled = true
                startDeliveryButton.isEnabled = true
                stopDeliveryButton.isEnabled = true
                linkTestButton.isEnabled = true
            }
            else -> {
                connectButton.text = "连接 PC"
                connectButton.isEnabled = true
                disconnectButton.isEnabled = false
                startDeliveryButton.isEnabled = false
                stopDeliveryButton.isEnabled = false
                linkTestButton.isEnabled = false
            }
        }
    }

    private val logBuf = ArrayList<String>()

    /**
     * 日志：**只保留最近若干行**。
     *
     * 交付会话按 5 fps 抽帧、每帧至少两条日志 ⇒ 一小时就是几万行，
     * 而 TextView 每 append 一次都要重新布局，界面会越来越卡、内存也一直涨。
     * 这里把上限钉死在 LOG_MAX 行（超了就丢最旧的），代价是早期日志不可见 ——
     * 关键信息（连接结果、失败原因）本来就在最近几分钟内。
     */
    private fun log(s: String) {
        logBuf.add(s)
        if (logBuf.size > LOG_MAX) {
            logBuf.subList(0, logBuf.size - LOG_KEEP).clear()
            logView.text = logBuf.joinToString("\n")
        } else {
            logView.append(s + "\n")
        }
    }

    private fun setStatus(s: String) {
        statusView.text = s
    }

    /** 交付收尾状态的中文标签。**"已传完"必须与"没活"分开** —— 这正是用户要看到的那句话。 */
    private fun drainLabel(s: String): String = when (s) {
        "drained" -> "已传完✓"
        "draining" -> "回传中"
        "blocked" -> "传不动(HTTP不通?)"
        else -> "无"
    }

    private fun externalDir(): File {
        val d = getExternalFilesDir(null) ?: File(filesDir, "m0")
        if (!d.exists()) d.mkdirs()
        return d
    }

    /**
     * 留存账本行（应有 / 实存 / 两个缺口 / 写入速率 / 剩余空间）。
     *
     * 这几个数**必须同时显示**：只看"实存"会把"采集期没写进去"与"还没回传到 PC"
     * 混成一件事，而两者的处置完全不同（前者**只能重采**，后者**可以重传**）。
     * ⚠ `PC已收` 用 `uploaded_frames`（PC 侧 verified）—— 那个数只有 PC 说了算。
     */
    private fun setStoreLine(st: JSONObject) {
        if (!::storeInfoView.isInitialized) return
        val a = st.optJSONObject("store") ?: return
        val state = when (a.optString("state")) {
            "recording" -> "留存中"
            "uploading" -> "回传中"
            "done" -> "已完成✓"
            "aborted_no_space" -> "已中止(空间不足)"
            "aborted" -> "已中止(" + a.optString("aborted_reason") + ")"
            else -> "未开始"
        }
        val eta = a.optDouble("eta_full_min", -1.0)
        storeInfoView.text = buildString {
            append(if (a.optBoolean("every_frame", true)) "留存[每帧]" else "留存[降载]")
            append("[").append(state).append("] ")
            append("应有=").append(a.optInt("frames_expected"))
            append(" 手机实存=").append(a.optInt("frames_stored_on_device"))
            append(" 采集缺口=").append(a.optInt("gaps_on_device"))
            append(" PC已收=").append(st.optInt("uploaded_frames"))
            append("  ").append(a.optLong("bytes") / 1024 / 1024).append("MB")
            append(" 写=").append(String.format("%.1f", a.optDouble("write_mbps"))).append("MB/s")
            if (eta > 0) append(String.format(" (约 %.0f 分钟后需处理空间)", eta))
            append(" 剩余=").append(a.optLong("free_mb")).append("MB")
            if (a.optInt("result_missing") > 0) {
                append("  ⚠结果缺席=").append(a.optInt("result_missing"))
            }
        }
    }

    private fun sharePath(name: String): String =
        "/sdcard/Android/data/$packageName/files/$name"

    private fun readParams(): Triple<Float, Int, Int> {
        val d = distInput.text.toString().trim().toFloatOrNull() ?: 0f
        val f = fpsInput.text.toString().trim().toIntOrNull() ?: 30
        val n = framesInput.text.toString().trim().toIntOrNull() ?: LockedCaptureProbe.DEFAULT_FRAMES
        return Triple(d.coerceAtLeast(0f), f.coerceIn(1, 240), n.coerceIn(10, 3000))
    }

    // ---------------- ① 静态能力探测 ----------------

    private fun runCapabilityProbe() {
        Thread {
            try {
                val report = CapabilityReport.collect(this)
                val f = File(externalDir(), capabilityFile)
                f.writeText(report.toString(2))

                backCameraId = pickBackCamera(report)

                runOnUiThread {
                    renderCapabilitySummary(report)
                    log("")
                    log("已写出: ${sharePath(capabilityFile)}")
                    captureButton.text = "② 开始锁定连拍验证（摄像头 $backCameraId）"
                    captureButton.isEnabled = true
                    repeatButton.isEnabled = true
                    sweepButton.isEnabled = true
                    sizeSweepButton.isEnabled = true
                    stillShotButton.isEnabled = true
                    setStatus("静态探测完成。请把手机固定好对准场景，再点②")
                }
            } catch (e: Exception) {
                runOnUiThread {
                    setStatus("静态探测失败: ${e.javaClass.simpleName}: ${e.message}")
                    log("异常: $e")
                }
            }
        }.start()
    }

    private fun pickBackCamera(report: JSONObject): String? {
        val cams = report.optJSONArray("cameras") ?: return null
        for (i in 0 until cams.length()) {
            val c = cams.getJSONObject(i)
            if (c.optString("lens_facing") == "BACK" &&
                c.optJSONObject("sensor")?.optJSONObject("active_array_size") != null
            ) {
                return c.optString("id")
            }
        }
        for (i in 0 until cams.length()) {
            val c = cams.getJSONObject(i)
            if (c.optString("lens_facing") == "BACK") return c.optString("id")
        }
        return if (cams.length() > 0) cams.getJSONObject(0).optString("id") else null
    }

    private fun renderCapabilitySummary(report: JSONObject) {
        val dev = report.optJSONObject("device")
        log("===== 设备 =====")
        log("${dev?.optString("manufacturer")} ${dev?.optString("model")}")
        log("Android ${dev?.optString("android_release")} (API ${dev?.optInt("sdk_int")})")
        log("")

        val cams = report.optJSONArray("cameras") ?: return
        log("===== 摄像头 (${cams.length()}) =====")
        for (i in 0 until cams.length()) {
            val c = cams.getJSONObject(i)
            val focus = c.optJSONObject("focus")
            val timing = c.optJSONObject("timing")
            val dist = c.optJSONObject("distortion")
            val sizes = c.optJSONObject("output_sizes")
            val lens = c.optJSONObject("lens")

            log("--- id=${c.optString("id")} ${c.optString("lens_facing")} " +
                "level=${c.optString("supported_hardware_level")} ---")
            log("  手动对焦可用: ${focus?.optBoolean("manual_focus_supported")}")
            log("  minFocus=${focus?.optDouble("min_focus_distance_diopter")}diopt " +
                "(${focus?.opt("min_focus_distance_meters")}m)  " +
                "hyper=${focus?.optDouble("hyperfocal_distance_diopter")}diopt")
            log("  对焦距离标定: ${focus?.optString("focus_distance_calibration")}")
            log("  AF modes: ${focus?.optJSONArray("af_available_modes")}")
            log("  focal(mm)=${lens?.optJSONArray("focal_lengths_mm")} " +
                "aperture=${lens?.optJSONArray("apertures")}")
            log("  OIS modes: ${lens?.optJSONArray("optical_stabilization_available_modes")}  " +
                "EIS modes: ${c.optJSONObject("stabilization")?.optJSONArray("video_stabilization_available_modes")}")
            log("  zoom: max_digital=${c.optJSONObject("zoom")?.optDouble("max_digital_zoom")} " +
                "ratio_range=${c.optJSONObject("zoom")?.opt("zoom_ratio_range")}")
            log("  timestamp_source=${timing?.optString("timestamp_source")}  " +
                "(卷帘读出时间需逐帧读，见锁定连拍报告)")
            log("  distortion modes=${dist?.optJSONArray("distortion_correction_available_modes")}")
            log("  caps=${c.optJSONArray("capabilities")}")
            val op = c.optJSONObject("optics")
            log("  光学: f=${fmt2(op?.optDouble("focal_length_mm"))}mm " +
                "像元=${fmt3(op?.optDouble("pixel_pitch_um"))}um " +
                "裁切系数=${fmt3(op?.optDouble("crop_factor"))} " +
                "等效焦距=${fmt2(op?.optDouble("equivalent_focal_length_mm"))}mm " +
                "(全幅fx=${fmt2(op?.optDouble("fx_px_full_array"))}px)")
            val mc = c.optJSONObject("multi_camera")
            log("  逻辑多摄: is_logical=${mc?.optBoolean("is_logical")} " +
                "物理镜头数=${mc?.optInt("physical_camera_count")} " +
                "sync=${mc?.optString("sensor_sync_type")}")
            val pcs = mc?.optJSONArray("physical_cameras")
            if (pcs != null) {
                for (j in 0 until pcs.length()) {
                    val p = pcs.getJSONObject(j)
                    log("    · 物理 ${p.optString("id")}: " +
                        "f=${p.optJSONArray("focal_lengths_mm")}mm " +
                        "等效=${fmt2(p.optDouble("equivalent_focal_length_mm"))}mm " +
                        "active=${p.opt("active_array_size")} " +
                        "err=${p.optString("error").takeIf { it.isNotEmpty() && it != "null" } ?: "-"}")
                }
            }
            log("  manual_sensor=${c.optBoolean("manual_sensor_supported")} " +
                "burst=${c.optBoolean("burst_capture_supported")} " +
                "raw=${c.optBoolean("raw_supported")} " +
                "private_reprocessing=${c.optBoolean("private_reprocessing_supported")}")
            log("  largest YUV=${sizes?.optJSONObject("YUV_420_888")?.opt("largest")} " +
                "JPEG=${sizes?.optJSONObject("JPEG")?.opt("largest")} " +
                "RAW_SENSOR=${sizes?.optJSONObject("RAW_SENSOR")?.opt("largest")} " +
                "(RAW count=${sizes?.optJSONObject("RAW_SENSOR")?.optInt("count")})")
            log("")
        }
    }

    // ---------------- ② 锁定连拍验证 ----------------

    private fun runLockedCaptureProbe(saveAs: String) {
        val id = backCameraId
        if (id == null) {
            setStatus("未找到可用的后置摄像头")
            return
        }
        captureButton.isEnabled = false
        repeatButton.isEnabled = false
        sweepButton.isEnabled = false
        sizeSweepButton.isEnabled = false
        log("")
        val (dist, fps, n) = readParams()
        log("===== 锁定连拍 (camera $id, 工作距离=${if (dist > 0f) "${dist}m" else "超焦距"}, " +
            "${fps}fps, $n 帧${if (forcedSize != null) ", 强制尺寸 ${forcedSize}" else ""}) =====")

        val p = LockedCaptureProbe(
            context = this,
            cameraId = id,
            focusDistanceM = dist,
            targetFps = fps,
            targetFrames = n,
            forcedCaptureSize = forcedSize
        )
        probe = p
        p.run(object : LockedCaptureProbe.Callback {
            override fun onProgress(msg: String) {
                runOnUiThread { setStatus(msg) }
            }

            override fun onDone(report: JSONObject?, error: String?) {
                runOnUiThread {
                    if (error != null) {
                        setStatus("锁定连拍失败: $error")
                        log("失败: $error")
                    } else if (report != null) {
                        try {
                            File(externalDir(), saveAs).writeText(report.toString(2))
                        } catch (e: Exception) {
                            log("写文件失败: ${e.message}")
                        }
                        renderCaptureSummary(report)
                        setStatus("完成，已写出 $saveAs")
                        log("已写出: ${sharePath(saveAs)}")
                        recordRepeatability(report)
                    }
                    captureButton.isEnabled = true
                    repeatButton.isEnabled = true
                    sweepButton.isEnabled = true
                    sizeSweepButton.isEnabled = true
                    stillShotButton.isEnabled = true
                    probe = null
                    onRoundFinished()
                }
            }
        })
    }

    /** 从一轮报告里抽出跨会话重复性关心的几个量。 */
    private fun recordRepeatability(report: JSONObject) {
        if (repeatRound <= 0) return
        val rv = report.optJSONObject("stats")?.optJSONObject("requested_vs_actual")
        val focus = rv?.optJSONObject("focus_diopter")
        val fd = rv?.optJSONObject("frame_duration_ns")
        val lt = report.optJSONObject("stats")?.optJSONObject("lens_state_timeline")

        val fa = if (focus != null && focus.has("actual") && !focus.isNull("actual"))
            focus.optDouble("actual") else null
        val fdev = if (focus != null && focus.has("deviation_pp_percent"))
            focus.optDouble("deviation_pp_percent") else null
        val fdm = if (fd != null && fd.has("actual")) fd.optDouble("actual") / 1e6 else null
        val mv = lt?.optInt("moving_after_warmup", -1) ?: -1

        roundStats.add(RoundStat(fa, fdev, fdm, mv))
        repeatSummary.add(
            "  r$repeatRound  焦点回读=${fmt6(fa)}diopt 偏差=${fmt3(fdev)}%  " +
                "帧时长=${fmt3(fdm)}ms  预热区外MOVING=$mv"
        )
    }

    // ---------------- ③ 重复性测试 ----------------

    private fun startRepeatabilityTest() {
        if (backCameraId == null) {
            setStatus("未找到可用的后置摄像头")
            return
        }
        repeatSummary.clear()
        roundStats.clear()
        repeatRound = 1
        forcedSize = null          // ③ 必须跑"交付配置"的那一档，不能是扫描档
        setBusy(true)
        log("")
        log("===== 重复性测试：$repeatRounds 轮，每轮重新开关摄像头 =====")
        log("关注点：同一请求参数在各轮之间的回读值是否一致（标定可复现的前提）")
        runLockedCaptureProbe("m0_locked_capture_r$repeatRound.json")
    }

    private fun onRoundFinished() {
        if (repeatRound <= 0) return
        if (repeatRound < repeatRounds) {
            repeatRound++
            ui.postDelayed({
                // 关闭摄像头后需要一点时间才能真正释放，立刻重开会失败
                runLockedCaptureProbe("m0_locked_capture_r$repeatRound.json")
            }, 2000L)
        } else {
            log("")
            log("===== 重复性汇总 =====")
            repeatSummary.forEach { log(it) }
            val focuses = roundStats.mapNotNull { it.focusActual }
            if (focuses.size >= 2) {
                val spread = focuses.max() - focuses.min()
                val rel = if (focuses.min() > 0) spread / focuses.min() * 100 else 0.0
                log("焦点回读值极差=${fmt6(spread)}diopt 相对=${fmt3(rel)}%")
                log(if (rel < 0.5) "→ 跨会话重复性良好，一次标定可覆盖多次采集"
                    else "→ 跨会话回读值不一致，标定必须记录实际回读值并按其索引内参")
            }
            val badRounds = roundStats.count { it.movingAfterWarmup > 0 }
            log("预热区外仍出现 MOVING 的轮数: $badRounds/${roundStats.size}")
            repeatRound = 0
            setStatus("重复性测试完成")
        }
    }

    // ---------------- ④ 帧率权威性测验 ----------------

    /**
     * 判定帧率是否真的可控。
     *
     * 单个档位的测试是不可证伪的：请求 30fps 得到 30.04fps 会被判为"一致"，
     * 但那可能只是 sensor 的原生帧率恰好接近 30fps，请求根本没被采纳。
     * 唯一可靠的做法是**请求若干个离默认值足够远的档位**，
     * 看实测帧间隔是否跟着请求变。全都纹丝不动 => 帧率不可控。
     *
     * 同时交叉验证下发方式：只发 SENSOR_FRAME_DURATION / 额外再发 AE_TARGET_FPS_RANGE。
     * 首轮数据无法排除"两者冲突导致双双失效"，这一轮把变量分开。
     */
    private fun startRateSweep() {
        if (backCameraId == null) {
            setStatus("未找到可用的后置摄像头")
            return
        }
        sweepTrials.clear()
        sweepIndex = 0
        setBusy(true)
        log("")
        log("===== ④ 帧率权威性测验 =====")
        log("档位: ${sweepRates.joinToString("/")}fps × 两种下发方式 = ${sweepRates.size * 2} 轮")
        log("每轮 $sweepFrames 帧（预热 $sweepWarmup 帧，每轮重新开关摄像头）")
        log("判据：实测帧间隔是否随请求变化。全部相同 = 帧率不可控。")
        runNextSweepTrial()
    }

    private fun runNextSweepTrial() {
        val total = sweepRates.size * 2
        if (sweepIndex >= total) {
            finishRateSweep()
            return
        }
        val fps = sweepRates[sweepIndex % sweepRates.size]
        val legacy = sweepIndex >= sweepRates.size
        setStatus("④ ${sweepIndex + 1}/$total：请求 ${fps}fps" +
            (if (legacy) "（额外下发 AE fps 档位）" else "（只下发帧时长）"))

        val p = LockedCaptureProbe(
            context = this,
            cameraId = backCameraId!!,
            focusDistanceM = readParams().first,
            targetFps = fps,
            targetFrames = sweepFrames,
            warmupFrames = sweepWarmup,
            legacyFpsRange = legacy,
            reportKind = "m0_rate_sweep_trial"
        )
        probe = p
        p.run(object : LockedCaptureProbe.Callback {
            override fun onProgress(msg: String) {
                runOnUiThread { setStatus("④ ${sweepIndex + 1}/$total $msg") }
            }

            override fun onDone(report: JSONObject?, error: String?) {
                runOnUiThread {
                    val t = JSONObject()
                    t.put("requested_fps", fps)
                    t.put("legacy_fps_range", legacy)
                    if (report == null) {
                        t.put("error", error ?: "unknown")
                        log("  ${fps}fps${if (legacy) "+" else ""} → 失败: $error")
                    } else {
                        val stats = report.optJSONObject("stats")
                        val fdc = stats?.optJSONObject("frame_duration_control")
                        val iv = stats?.optJSONObject("interval_ns")
                        val ivMean = jsonDouble(iv, "mean")
                        val fdAct = jsonLong(fdc, "actual_ns")
                        t.put("requested_ns", jsonLong(report.optJSONObject("requested"), "sensor_frame_duration_ns"))
                        t.put("actual_frame_duration_ns", fdAct)
                        t.put("honored_verbatim", fdc?.optBoolean("honored_verbatim") ?: JSONObject.NULL)
                        t.put("actual_interval_ns", ivMean)
                        t.put("actual_fps", if (ivMean != null && ivMean > 0) 1e9 / ivMean else JSONObject.NULL)
                        t.put("valid_frames", stats?.optInt("valid_frames"))
                        // 必须记录本 trial 是在**哪个输出尺寸**下跑的：
                        // 帧率上限与输出尺寸绑定（面积越大上限越低），不记尺寸，
                        // "该档不可达"这个结论就不知道适用于哪一档，等于没有证据。
                        t.put("capture_size", report.optJSONObject("capture_size"))
                        t.put("config_fingerprint",
                            report.optJSONObject("capture_config")?.optString("config_fingerprint"))
                        log("  请求 %2dfps%-3s → 帧间隔 %s ms  帧时长回读 %s  逐字采纳=%s".format(
                            fps,
                            if (legacy) "(+fps档)" else "(仅时长)",
                            fmt3(ivMean?.div(1e6)),
                            if (fdAct != null) "${fmt3(fdAct / 1e6)}ms" else "?",
                            fdc?.optBoolean("honored_verbatim") ?: false))
                    }
                    sweepTrials.add(t)
                    sweepIndex++
                    probe = null
                    ui.postDelayed({ runNextSweepTrial() }, 900L)
                }
            }
        })
    }

    private fun finishRateSweep() {
        val trials = JSONArray()
        sweepTrials.forEach { trials.put(it) }

        // 逐轮实测帧间隔去重（取整到 1us，避免浮点噪声被当成不同档位）
        val actSet = LinkedHashMap<Long, Int>()
        for (t in sweepTrials) {
            val v = jsonDouble(t, "actual_interval_ns") ?: continue
            val k = Math.round(v / 1000.0)
            actSet[k] = (actSet[k] ?: 0) + 1
        }
        val distinct = actSet.keys.sorted()
        val authority = when {
            sweepTrials.none { jsonDouble(it, "actual_interval_ns") != null } -> "unknown"
            distinct.size == 1 -> "ignored"
            else -> "effective"
        }

        val o = JSONObject()
        o.put("report_kind", "m0_rate_sweep")
        o.put("generated_at_wall_ms", System.currentTimeMillis())
        // ④ 这份汇总原先**既没有 app_version 也没有输出尺寸**，于是判据无法回答
        // "这个上限是在哪一档、由哪版代码测出来的" —— 而帧率上限恰恰与这两者都相关。
        // 同一类"报告无法自我定位"的坑本轮在三处出现过，所以统一补齐。
        o.put("app_version", LockedCaptureProbe.APP_VERSION)
        o.put("capture_path_rev", LockedCaptureProbe.CAPTURE_PATH_REV)
        o.put("camera_id", backCameraId ?: JSONObject.NULL)
        o.put("requested_rates_fps", JSONArray(sweepRates))
        o.put("frames_per_trial", sweepFrames)
        o.put("warmup_frames", sweepWarmup)
        o.put("trials", trials)
        o.put("distinct_actual_interval_ns", JSONArray(distinct))
        o.put("control_authority", authority)
        if (distinct.size == 1) o.put("sensor_native_interval_ns", distinct[0] * 1000L)

        try {
            File(externalDir(), sweepFile).writeText(o.toString(2))
        } catch (e: Exception) {
            log("写文件失败: ${e.message}")
        }

        log("")
        log("实测帧间隔的**不同取值**个数: ${distinct.size}")
        distinct.forEach { log("  ${fmt3(it * 1000.0 / 1e6)} ms  (出现 ${actSet[it]} 次)") }
        when (authority) {
            "effective" -> log("→ 帧率可控：实测帧间隔随请求变化")
            "ignored" -> log("→ 帧率不可控：所有请求档位实测帧间隔完全相同，" +
                "该值即 sensor 原生帧率。采集节奏必须按回读值规划，不能按请求值规划")
            else -> log("→ 未取得任何有效数据")
        }
        log("已写出: ${sharePath(sweepFile)}")

        setBusy(false)
        setStatus("④ 完成：帧率控制权威性 = $authority")
    }

    // ---------------- ⑤ 尺寸扫描 ----------------

    /**
     * 逐档扫描候选输出尺寸，量出**实际可达帧率**与**卷帘读出时间**。
     *
     * 为什么要单独做这一步：输出尺寸是内参的一部分，而自动选档依赖一个人为的
     * 面积上限（[LockedCaptureProbe.CAPTURE_AREA_CAP_PX] = 2048x1536 ≈ 3.15 MP）。
     * 那个上限是"感觉带宽够用"定的，没有实测依据。而 **fx 与输出宽度成正比**
     * （1920 -> 1282.5 px，2880 -> 1923.8 px，4096 -> 2736 px），
     * 亚像素定位精度大致与 fx 成正比 —— 所以这个上限直接决定了方案的精度上限。
     *
     * 定档只能靠实测：候选尺寸下各自的实际帧间隔是多少、skew 是多少（它会随
     * 输出高度变，参考 G14/G23），然后按精度/带宽取舍。每档只采少量帧，
     * 目的是量节奏与读出时间，不是产出交付数据。
     */
    private fun startSizeSweep() {
        if (backCameraId == null) {
            setStatus("未找到可用的后置摄像头")
            return
        }
        val targets = buildSizeSweepTargets()
        if (targets.isEmpty()) {
            setStatus("读不到候选尺寸：请先跑①静态探测（需要输出的 sizes_same_aspect 清单）")
            return
        }
        sizeSweepTargets = targets
        sizeSweepTrials.clear()
        sizeSweepIndex = 0
        setBusy(true)
        log("")
        log("===== ⑤ 尺寸扫描 =====")
        log("档位 " + targets.size + " 个：")
        targets.forEach { log("  · ${it.size.width}x${it.size.height}" +
            (if (it.forceFullCrop) " [强制全阵列 crop]" else "") + "  —— ${it.label}") }
        log("每档 $sizeSweepFrames 帧（预热 $sizeSweepWarmup），每档重新开关摄像头")
        log("关注点：实测帧间隔、回读帧时长、以及 skew 该除以谁（读出窗口行数 = crop_w/输出纵横比）")
        runNextSizeTrial()
    }

    /**
     * 构建扫描档位：4:3 阶梯（回答"该定在哪一档"）+ 交叉纵横比判别档（回答"skew 归属"）。
     *
     * 两类问题的自变量不同，必须分开设计，否则又变成混淆实验：
     *   - **定档**：只动输出高度/面积，纵横比固定 4:3 ⇒ 带宽 vs fx 的取舍可用；
     *   - **skew 归属**：必须制造"纵横比变了但 crop 没变"的档 ⇒ 只有强制全阵列 crop 才做得到。
     */
    private fun buildSizeSweepTargets(): ArrayList<SizeTrial> {
        val out = ArrayList<SizeTrial>()
        val same = readSameAspectSizes()
        for (t in same) out.add(SizeTrial(t, false, "4:3 阶梯（与活动阵列同纵横比）"))

        // 交叉纵横比对照：挑一个 16:9 的 YUV 尺寸（优先 1920x1080）
        val all = readYuvSizes()
        val wide = all.filter { it.height > 0 && abs(it.width.toDouble() / it.height - 16.0 / 9) < 0.02 }
            .minByOrNull { abs(it.width - 1920) }
        if (wide != null) {
            out.add(SizeTrial(wide, false,
                "16:9 输出 + aspectSafeCrop（纵横比与 crop 高度同变，单看这条无法归因）"))
            out.add(SizeTrial(wide, true,
                "**判别档** 16:9 输出 + 强制全阵列 crop：只动纵横比、不动 crop。"
                    + "若 skew 仍按 16:9 的行数算 ⇒ 归属=输出纵横比；若按 3072 行算 ⇒ 归属=crop 高度。"
                    + "本档几何不可用（故意各向异性），只读 skew"))
        } else {
            log("  ⚠ 设备 YUV 清单里找不到 16:9 尺寸，无法加判别档 —— skew 归属只能继续悬着")
        }
        return out
    }

    /** 从①的静态报告里取该后置摄像头的**全部** YUV_420_888 输出尺寸。 */
    private fun readYuvSizes(): ArrayList<android.util.Size> {
        val out = ArrayList<android.util.Size>()
        val f = File(externalDir(), capabilityFile)
        if (!f.exists()) return out
        val all = try { JSONObject(f.readText()) } catch (e: Exception) { return out }
        val cams = all.optJSONArray("cameras") ?: return out
        var arr: JSONArray? = null
        for (i in 0 until cams.length()) {
            val c = cams.optJSONObject(i) ?: continue
            if (c.optString("id") != backCameraId) continue
            arr = c.optJSONObject("output_sizes")
                ?.optJSONObject("YUV_420_888")
                ?.optJSONArray("sizes")
            break
        }
        if (arr == null) return out
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val w = o.optInt("w"); val h = o.optInt("h")
            if (w > 0 && h > 0) out.add(android.util.Size(w, h))
        }
        return out
    }

    /** 从①的静态报告里取"与活动阵列同纵横比"的 YUV 输出尺寸，按宽度阶梯解析成具体档位。 */
    private fun readSameAspectSizes(): ArrayList<android.util.Size> {
        val out = ArrayList<android.util.Size>()
        val f = File(externalDir(), capabilityFile)
        if (!f.exists()) return out
        val all = try { JSONObject(f.readText()) } catch (e: Exception) { return out }
        val cams = all.optJSONArray("cameras") ?: return out
        var same: JSONArray? = null
        for (i in 0 until cams.length()) {
            val c = cams.optJSONObject(i) ?: continue
            if (c.optString("id") != backCameraId) continue
            same = c.optJSONObject("output_sizes")
                ?.optJSONObject("YUV_420_888")
                ?.optJSONArray("sizes_same_aspect")
            break
        }
        if (same == null) return out
        val cand = ArrayList<android.util.Size>()
        for (i in 0 until same.length()) {
            val o = same.optJSONObject(i) ?: continue
            val w = o.optInt("w"); val h = o.optInt("h")
            if (w > 0 && h > 0) cand.add(android.util.Size(w, h))
        }
        if (cand.isEmpty()) return out
        for (target in sizeSweepWidths) {
            // 取"不超过目标宽度"里最宽的；没有则取比目标略宽里最窄的
            val pick = cand.filter { it.width <= target }.maxByOrNull { it.width }
                ?: cand.filter { it.width > target }.minByOrNull { it.width }
                ?: continue
            if (out.none { it.width == pick.width && it.height == pick.height }) out.add(pick)
        }
        return out
    }

    private fun runNextSizeTrial() {
        if (sizeSweepIndex >= sizeSweepTargets.size) {
            finishSizeSweep()
            return
        }
        val trial = sizeSweepTargets[sizeSweepIndex]
        val sz = trial.size
        setStatus("⑤ ${sizeSweepIndex + 1}/${sizeSweepTargets.size}：${sz.width}x${sz.height}" +
            (if (trial.forceFullCrop) "（全阵列 crop）" else ""))
        val (dist, fps, _) = readParams()
        val p = LockedCaptureProbe(
            context = this,
            cameraId = backCameraId!!,
            focusDistanceM = dist,
            targetFps = fps,
            targetFrames = sizeSweepFrames,
            warmupFrames = sizeSweepWarmup,
            reportKind = "m0_size_sweep_trial",
            forcedCaptureSize = sz,
            forceFullCrop = trial.forceFullCrop
        )
        probe = p
        p.run(object : LockedCaptureProbe.Callback {
            override fun onProgress(msg: String) {
                runOnUiThread { setStatus("⑤ ${sz.width}x${sz.height} $msg") }
            }

            override fun onDone(report: JSONObject?, error: String?) {
                runOnUiThread {
                    val t = JSONObject()
                    t.put("label", trial.label)
                    t.put("target_width", sz.width)
                    t.put("forced_full_crop", trial.forceFullCrop)
                    t.put("requested_size", JSONObject().apply {
                        put("w", sz.width); put("h", sz.height)
                    })
                    if (report == null) {
                        t.put("error", error ?: "unknown")
                        log("  ${sz.width}x${sz.height} → 失败: $error")
                    } else {
                        val cfg = report.optJSONObject("capture_config")
                        val stats = report.optJSONObject("stats")
                        val actual = report.optJSONObject("capture_size")
                            ?: cfg?.optJSONObject("output_size")
                        val crop = cfg?.optJSONObject("crop_region")
                        val ivMean = jsonDouble(stats?.optJSONObject("interval_ns"), "mean")
                        val fd = jsonLong(stats?.optJSONObject("frame_duration_control"), "actual_ns")
                        val skew = jsonDouble(stats?.optJSONObject("rolling_shutter_skew_ns"), "value")
                        val aw = actual?.optInt("w") ?: 0
                        val ah = actual?.optInt("h") ?: 0
                        val cw = crop?.optInt("width") ?: 0
                        val ch = crop?.optInt("height") ?: 0
                        val fps = if (ivMean != null && ivMean > 0) 1e9 / ivMean else null
                        t.put("actual_size", actual)
                        // 必须记**请求**帧率：不记的话，"各档实测都 ≈30 fps"会被读成
                        // "帧率不随尺寸下降"，而真实原因只是"各档都只请求了默认的 30"。
                        // 从"达到了 30"推不出"上限就是 30"，更推不出"上限与尺寸无关"。
                        t.put("target_fps",
                            jsonDouble(report.optJSONObject("params"), "target_fps"))
                        t.put("crop_region", crop)
                        t.put("output_aspect", if (aw > 0 && ah > 0) aw.toDouble() / ah else JSONObject.NULL)
                        t.put("crop_shrunk_for_aspect", cfg?.optBoolean("crop_shrunk_for_aspect"))
                        t.put("sensor_pixel_mode", cfg?.optInt("sensor_pixel_mode"))
                        t.put("config_fingerprint", cfg?.optString("config_fingerprint"))
                        t.put("size_choice_note", cfg?.optString("size_choice_note"))
                        t.put("actual_interval_ns", ivMean)
                        t.put("actual_fps", fps ?: JSONObject.NULL)
                        t.put("frame_duration_ns", fd)
                        t.put("rolling_shutter_skew_ns", skew)
                        // 三种归一化口径都记下来，让"哪个是不变量"可以直接在报告里看出来，
                        // 而不是等下游猜。实测结论：只有最后一个是恒定的 2999.0 ns/行。
                        t.put("skew_per_output_row_ns",
                            if (skew != null && ah > 0) skew / ah else JSONObject.NULL)
                        t.put("skew_per_crop_row_ns",
                            if (skew != null && ch > 0) skew / ch else JSONObject.NULL)
                        val readRows = if (cw > 0 && aw > 0 && ah > 0) cw.toDouble() * ah / aw else null
                        t.put("skew_readout_rows", readRows ?: JSONObject.NULL)
                        t.put("skew_line_time_ns",
                            if (skew != null && readRows != null && readRows > 0) skew / readRows
                            else JSONObject.NULL)
                        t.put("valid_frames", stats?.optInt("valid_frames"))
                        t.put("output_to_crop_scale_x", jsonDouble(cfg, "output_to_crop_scale_x"))
                        t.put("output_to_crop_scale_y", jsonDouble(cfg, "output_to_crop_scale_y"))
                        log("  ${sz.width}x${sz.height}" +
                            (if (trial.forceFullCrop) " [全crop]" else "") +
                            " → ${fmt3(if (ivMean != null) ivMean / 1e6 else null)} ms" +
                            " (${fmt3(fps)} fps)  回读 ${fmt3(if (fd != null) fd / 1e6 else null)} ms" +
                            "  crop ${cw}x${ch}" +
                            "  skew ${fmt3(if (skew != null) skew / 1e6 else null)} ms" +
                            "  → " + fmt3(if (skew != null && readRows != null && readRows > 0) skew / readRows else null) +
                            " ns/读出窗口行")
                    }
                    sizeSweepTrials.add(t)
                    sizeSweepIndex++
                    probe = null
                    ui.postDelayed({ runNextSizeTrial() }, 1500L)
                }
            }
        })
    }

    private fun finishSizeSweep() {
        val trials = JSONArray()
        sizeSweepTrials.forEach { trials.put(it) }
        val report = JSONObject().apply {
            put("report_kind", "m0_size_sweep")
            put("generated_at_wall_ms", System.currentTimeMillis())
            put("app_version", LockedCaptureProbe.APP_VERSION)
            put("capture_path_rev", LockedCaptureProbe.CAPTURE_PATH_REV)
            put("camera_id", backCameraId ?: "")
            put("frames_per_trial", sizeSweepFrames)
            put("warmup_frames", sizeSweepWarmup)
            put("width_ladder", JSONArray(sizeSweepWidths))
            put("auto_pick_area_cap_px", LockedCaptureProbe.CAPTURE_AREA_CAP_PX)
            put("auto_pick_rule", "同纵横比、像素面积不超过 auto_pick_area_cap_px 中面积最大者")
            put("trials", trials)
        }
        try {
            File(externalDir(), sizeSweepFile).writeText(report.toString(2))
            log("已写出: ${sharePath(sizeSweepFile)}")
        } catch (e: Exception) {
            log("写文件失败: ${e.message}")
        }

        log("")
        log("===== 尺寸扫描汇总 =====")
        log("  输出            crop         请求fps   实测间隔    实测fps    回读时长    skew        读出窗口行  ns/该行")
        val colsOut = ArrayList<Double>()
        val colsCrop = ArrayList<Double>()
        val colsRead = ArrayList<Double>()
        for (t in sizeSweepTrials) {
            if (t.has("error")) {
                log("  ${t.optInt("target_width")}px 目标档 → 失败: ${t.optString("error")}")
                continue
            }
            val act = t.optJSONObject("actual_size")
            val w = act?.optInt("w") ?: 0
            val h = act?.optInt("h") ?: 0
            val crop = t.optJSONObject("crop_region")
            val cwt = crop?.optInt("width") ?: 0
            val cht = crop?.optInt("height") ?: 0
            val iv = jsonDouble(t, "actual_interval_ns")
            val fd = jsonDouble(t, "frame_duration_ns")
            val sk = jsonDouble(t, "rolling_shutter_skew_ns")
            val rr = jsonDouble(t, "skew_readout_rows")
            val lt = jsonDouble(t, "skew_line_time_ns")
            jsonDouble(t, "skew_per_output_row_ns")?.let { colsOut.add(it) }
            jsonDouble(t, "skew_per_crop_row_ns")?.let { colsCrop.add(it) }
            lt?.let { colsRead.add(it) }
            val mark = if (t.optBoolean("forced_full_crop")) "  <- 判别档：几何不可用，只读 skew" else ""
            log("  ${(w.toString() + "x" + h).padEnd(14)} " +
                "${(cwt.toString() + "x" + cht).padEnd(12)} " +
                "${fmt0(jsonDouble(t, "target_fps")).padEnd(10)} " +
                "${fmt3(if (iv != null) iv / 1e6 else null).padEnd(10)} " +
                "${fmt3(jsonDouble(t, "actual_fps")).padEnd(10)} " +
                "${fmt3(if (fd != null) fd / 1e6 else null).padEnd(10)} " +
                "${fmt3(if (sk != null) sk / 1e6 else null).padEnd(11)} " +
                "${fmt0(rr).padEnd(10)} ${fmt3(lt)}$mark")
        }
        // 直接给出"哪个归一化才是不变量"的结论，省得下次再靠肉眼找比例
        fun spread(v: List<Double>): Double? {
            if (v.size < 2) return null
            val mn = v.minOrNull() ?: return null
            return (v.maxOrNull()!! - mn) / mn * 1e6
        }
        log("")
        log("三套归一化口径的极差（越小越像「真正的自变量」）：")
        log("  skew/输出行数        : " + fmt3(spread(colsOut)) + " ppm" +
            "   ← v1.5 的说法，⑤ 扫描已把它证伪")
        log("  skew/crop 行数       : " + fmt3(spread(colsCrop)) + " ppm")
        log("  skew/读出窗口行数     : " + fmt3(spread(colsRead)) + " ppm" +
            "   ← 实测唯一恒定（crop_w × outH/outW 为分母）")
        log("定档时把这张表和带宽预算一起看：宽度翻倍 -> fx 翻倍，但每帧字节数约翻 4 倍")
        setBusy(false)
        setStatus("⑤ 完成")
    }

    // ---------------- ⑥ 拍照路径（单张捕获） ----------------

    /**
     * 量"拍照"这条路径自己的证据。
     *
     * 起因是一个范围变化：交付明确为"回传使用拍照"，而此前**所有**证据都来自
     * `TEMPLATE_MANUAL` + `setRepeatingRequest` 的连续流。帧率、内参、时间戳
     * 那几条 PASS 都是在**另一条采集路径**上得到的。
     *
     * 范围一缩小，盲区反而变大 —— 去掉的只是"帧率可控"（拍照不需要帧率），
     * 但换上来的是三个**从未测过**的量：
     *   1. 单张模式下 `SENSOR_TIMESTAMP` 是否仍等于曝光起点（ZSL 会把它变成"旧帧"）
     *   2. `TEMPLATE_STILL_CAPTURE` 是否触发多帧合成（那时"这张的时刻"不是单值）
     *   3. JPEG 的 EXIF 时间能不能当时间源（它是系统墙钟，且精度由厂商决定）
     * 所以本步骤不是"再跑一遍"，而是跑到一条新路径上去取证。
     *
     * v1.8.1 补第 4 项：**图与结果是否配对**。图走 ImageReader 回调、结果走
     * onCaptureCompleted，两条通路顺序不保证 —— "交付的这张图确实是这个时间戳
     * 那张"以前没有任何字段能回答（见 LockedCaptureProbe 里 imagesByTs 的注释）。
     */
    private fun startStillShot() {
        if (backCameraId == null) {
            setStatus("未找到可用的后置摄像头")
            return
        }
        stillTargets = ArrayList(listOf(
            StillTrial(
                id = LockedCaptureProbe.PATH_STILL_YUV_SINGLE,
                label = "对照档：YUV + TEMPLATE_MANUAL，与②同几何/同格式/同预热，**只**把重复请求换成单张捕获",
                format = android.graphics.ImageFormat.YUV_420_888,
                template = android.hardware.camera2.CameraDevice.TEMPLATE_MANUAL,
                forcedSize = null,                 // 自动选档 = 与 ② 同一档
                suppressFrameDuration = false      // 必须发，否则相对②多动了一个变量
            ),
            StillTrial(
                id = LockedCaptureProbe.PATH_STILL_JPEG_TEMPLATE,
                label = "交付档：JPEG + TEMPLATE_STILL_CAPTURE + 全幅 4096x3072，不发帧时长约束",
                format = android.graphics.ImageFormat.JPEG,
                template = android.hardware.camera2.CameraDevice.TEMPLATE_STILL_CAPTURE,
                forcedSize = android.util.Size(4096, 3072),
                suppressFrameDuration = true
            )
        ))
        stillTrials.clear()
        stillIndex = 0
        setBusy(true)
        log("")
        log("===== ⑥ 拍照路径（单张捕获） =====")
        log("两条路径，各 $stillShots 张（预热 $stillWarmup 张），一张一张串行下发")
        stillTargets.forEach { log("  · ${it.id} —— ${it.label}") }
        log("关注点：单张时间戳是否 = 曝光起点（ZSL 会让它变成缓冲里的旧帧）、")
        log("        是否触发多帧合成（edge/nr 回读被改写即证据）、EXIF 亚秒位数、")
        log("        以及 images_missing/surplus 是否为 0（图与结果是否一一配对）")
        runNextStillTrial()
    }

    private fun runNextStillTrial() {
        if (stillIndex >= stillTargets.size) {
            finishStillShot()
            return
        }
        val tr = stillTargets[stillIndex]
        setStatus("⑥ ${stillIndex + 1}/${stillTargets.size}：${tr.id}")
        val (dist, fps, _) = readParams()
        val p = LockedCaptureProbe(
            context = this,
            cameraId = backCameraId!!,
            focusDistanceM = dist,
            targetFps = fps,
            targetFrames = stillShots,
            warmupFrames = stillWarmup,
            reportKind = "m0_still_shot_trial",
            forcedCaptureSize = tr.forcedSize,
            singleShot = true,
            shotIntervalMs = stillIntervalMs,
            outputFormat = tr.format,
            template = tr.template,
            capturePathId = tr.id,
            capturePathRev = LockedCaptureProbe.STILL_PATH_REV,
            suppressFrameDuration = tr.suppressFrameDuration
        )
        probe = p
        p.run(object : LockedCaptureProbe.Callback {
            override fun onProgress(msg: String) {
                runOnUiThread { setStatus("⑥ ${tr.id} $msg") }
            }

            override fun onDone(report: JSONObject?, error: String?) {
                runOnUiThread {
                    if (error == null && report != null) {
                        report.put("trial_id", tr.id)
                        report.put("label", tr.label)
                        renderStillTrial(report)
                        stillTrials.add(report)
                    } else {
                        val t = JSONObject().apply {
                            put("trial_id", tr.id)
                            put("label", tr.label)
                            put("error", error ?: "unknown")
                        }
                        stillTrials.add(t)
                        log("  ${tr.id} → 失败: $error")
                    }
                    stillIndex++
                    probe = null
                    // 换格式会重建会话，留出关闭摄像头的时间
                    ui.postDelayed({ runNextStillTrial() }, 2000L)
                }
            }
        })
    }

    private fun renderStillTrial(report: JSONObject) {
        val st = report.optJSONObject("stats")
        val cfg = report.optJSONObject("capture_config")
        val sp = st?.optJSONObject("shot_path")
        log("  ${report.optString("trial_id")}  ${report.opt("capture_size")} " +
            "${cfg?.optString("output_format")} ${cfg?.optString("template_name")} " +
            "crop=${cfg?.optJSONObject("crop_region")}")
        log("    下发 ${sp?.optInt("shots_dispatched")}/${sp?.optInt("shots_requested")} 张  " +
            "skew=${fmt3(jsonDouble(st?.optJSONObject("rolling_shutter_skew_ns"), "value")?.div(1e6))} ms  " +
            "指纹=${cfg?.optString("config_fingerprint")}")
        val lat = st?.optJSONObject("exposure_to_callback_ns")
        if (lat != null) {
            log("    曝光→回调 延迟: 均 ${fmt3(lat.optDouble("mean") / 1e6)} ms  " +
                "极差 ${fmt3(lat.optDouble("spread_ms"))} ms  n=${lat.optInt("n")}")
        }
        val adv = st?.optJSONObject("sensor_vs_callback_advance")
        if (adv != null) {
            log("    时间戳推进 vs 回调推进 最大差 ${fmt3(adv.optDouble("max_abs_diff_ms"))} ms" +
                "（大 ⇒ 交出的可能是缓冲里的旧帧）")
        }
        val lag = st?.optJSONObject("sensor_ts_minus_request")
        if (lag != null) {
            log("    时间戳 − 下发时刻: min=${fmt3(lag.optDouble("min")?.div(1e6))} ms " +
                "max=${fmt3(lag.optDouble("max")?.div(1e6))} ms " +
                "早于下发的张数=${lag.optInt("negative_count")}")
        }
        val ex = st?.optJSONObject("exif")
        if (ex != null) {
            log("    EXIF: 有亚秒的张数=${ex.optInt("with_subsec")}/${ex.optInt("n")} " +
                "亚秒位数=${ex.optJSONArray("subsec_digits_values")} " +
                "DateTimeOriginal=${ex.opt("sample_datetime_original")}")
        }
        val lock = st?.optJSONObject("lock_stability")
        for (k in listOf("edge_mode", "noise_reduction_mode", "distortion_correction_mode",
                         "control_enable_zsl")) {
            val e = lock?.optJSONObject(k) ?: continue
            log("    $k: distinct=${e.optInt("distinct_count")} values=${e.optJSONArray("values")}")
        }
    }

    private fun finishStillShot() {
        val trials = JSONArray()
        stillTrials.forEach { trials.put(it) }
        val o = JSONObject().apply {
            put("report_kind", "m0_still_shot")
            put("generated_at_wall_ms", System.currentTimeMillis())
            put("app_version", LockedCaptureProbe.APP_VERSION)
            // 本报告含**两条不同的采集路径**，所以顶层不写单一的 capture_path_id ——
            // 写了就等于撒谎（哪一条？）。路径标识逐 trial 记录。
            put("capture_path_id_per_trial", true)
            put("camera_id", backCameraId ?: JSONObject.NULL)
            // 设备身份：判据要回答"这份产出是哪台机器写的"。缺了它，
            // "两台机器的报告混在一起"这种错误所有判据都看不见
            // （配置指纹与采集路径可以完全一致）。
            put("device", JSONObject().apply {
                put("manufacturer", android.os.Build.MANUFACTURER)
                put("model", android.os.Build.MODEL)
                put("device", android.os.Build.DEVICE)
                put("build_fingerprint", android.os.Build.FINGERPRINT)
                put("android_sdk", android.os.Build.VERSION.SDK_INT)
            })
            put("shots_per_trial", stillShots)
            put("warmup_shots", stillWarmup)
            put("shot_interval_ms", stillIntervalMs)
            put("interleaved_formats_ok", true)
            put("trials", trials)
        }
        try {
            File(externalDir(), stillShotFile).writeText(o.toString(2))
            log("已写出: ${sharePath(stillShotFile)}")
        } catch (e: Exception) {
            log("写文件失败: ${e.message}")
        }
        log("")
        log("判读：① 对照档的 skew/crop 与 ② 相同 ⇒ 单张与重复请求用同一读出模式；")
        log("      ② 时间戳−下发时刻 全部为正 ⇒ 拿到的是「按下那一刻」的帧（未被 ZSL 换旧帧）；")

        log("      ③ edge/nr 回读仍是 OFF ⇒ 未触发多帧合成，时间戳仍是单值；")
        log("      ④ EXIF 亚秒位数 <3 ⇒ EXIF 不可作时间源，必须回传 SENSOR_TIMESTAMP；")
        log("      ⑤ images_missing>0 ⇒ 有结果没配到图（交付少图）；images_surplus>0 ⇒")
        log("         有图没被任何结果认领（说明图被记到了别处，比少图更严重）")
        setBusy(false)
        setStatus("⑥ 完成")
    }

    private fun setBusy(b: Boolean) {
        captureButton.isEnabled = !b
        repeatButton.isEnabled = !b
        sweepButton.isEnabled = !b
        sizeSweepButton.isEnabled = !b
        stillShotButton.isEnabled = !b
    }

    // ---------------- 报告渲染 ----------------

    private fun renderCaptureSummary(report: JSONObject) {
        val applied = report.optJSONObject("applied")
        val requested = report.optJSONObject("requested")
        val stats = report.optJSONObject("stats")
        val cfg = report.optJSONObject("capture_config")
        val rv = stats?.optJSONObject("requested_vs_actual")

        log("输出尺寸: ${report.opt("capture_size")}  帧数: ${report.optInt("frame_count")} " +
            "预热: ${stats?.optInt("warmup_frames")} 有效: ${stats?.optInt("valid_frames")} " +
            "时长: ${fmt2(stats?.optDouble("capture_duration_s"))}s")
        val scaleX = cfg?.optDouble("output_to_active_scale_x")
        if (scaleX != null && !scaleX.isNaN()) {
            log("传感器全幅=${cfg.optJSONObject("active_array")} " +
                "输出/全幅比例 x=${fmt4(scaleX)} y=${fmt4(cfg.optDouble("output_to_active_scale_y"))}")
        }

        log("")
        log("对焦策略: ${requested?.optString("focus_policy")}")
        log("  请求 ${fmt6(requested?.optDouble("focus_diopter"))}diopt " +
            "(${fmt3(requested?.optDouble("focus_requested_distance_m"))}m)  " +
            "→ 回读 ${fmt6(rv?.optJSONObject("focus_diopter")?.optDouble("actual"))}diopt " +
            "偏差 ${fmt3(rv?.optJSONObject("focus_diopter")?.optDouble("deviation_pp_percent"))}%")

        val iv = stats?.optJSONObject("interval_ns")
        val ivc = rv?.optJSONObject("interval_ns")
        log("")
        if (iv != null) {
            log("帧间隔: n=${iv.optInt("n")} mean=${fmt2(iv.optDouble("mean") / 1e6)}ms " +
                "min=${fmt2(iv.optDouble("min") / 1e6)} max=${fmt2(iv.optDouble("max") / 1e6)}ms")
            log("        抖动 std=${fmt3(iv.optDouble("std") / 1e6)}ms " +
                "(${fmt3(iv.optDouble("jitter_pp_percent"))}%)")
            log("        请求帧时长=${fmt3(ivc?.optDouble("requested")?.div(1e6))}ms " +
                "实测=${fmt3(ivc?.optDouble("actual")?.div(1e6))}ms " +
                "偏差=${fmt3(ivc?.optDouble("deviation_pp_percent"))}%")
        } else {
            log("帧间隔: 无数据（可能只收到 1 帧）")
        }

        val fdc = stats?.optJSONObject("frame_duration_control")
        log("帧时长控制: 请求=${fmt3(jsonDouble(fdc, "requested_ns")?.div(1e6))}ms " +
            "回读=${fmt3(jsonDouble(fdc, "actual_ns")?.div(1e6))}ms " +
            "逐字采纳=${fdc?.optBoolean("honored_verbatim")}")
        if (fdc?.optBoolean("honored_verbatim") == false) {
            log("  ⚠ 回读值不等于请求值 => HAL 自行决定了帧时长，" +
                "实测速率才是唯一事实。帧率是否可控由 ④ 判定")
        }

        val lock = stats?.optJSONObject("lock_stability")
        log("")
        log("参数锁定稳定性 (distinct_count 应为 1):")
        if (lock != null) {
            for (k in lock.keys()) {
                val e = lock.optJSONObject(k)
                val ok = if (e?.optBoolean("locked") == true) "OK  " else "漂移"
                log("  $ok $k: distinct=${e?.optInt("distinct_count")} " +
                    "samples=${e?.optInt("samples")} values=${e?.optJSONArray("values")}")
            }
            val pid = lock.optJSONObject("active_physical_id")
            log("  生效物理摄像头: distinct=${pid?.optInt("distinct_count")} " +
                "values=${pid?.optJSONArray("values")}")
            if ((pid?.optInt("distinct_count") ?: 1) > 1) {
                log("  ⚠ 采集中途换了物理镜头 => 焦距/主点/畸变同时跳变，整段数据内参不一致")
            }
        }

        log("")
        log("状态: ${stats?.optJSONObject("states")}")

        val lt = stats?.optJSONObject("lens_state_timeline")
        log("")
        log("镜头状态时间线: 前12帧=${lt?.optJSONArray("sequence_head")}")
        log("  MOVING 帧索引=${lt?.optJSONArray("moving_frame_indices")} " +
            "预热区外=${lt?.optInt("moving_after_warmup")} " +
            "全在预热区内=${lt?.optBoolean("moving_all_within_warmup")}")

        log("")
        log("卷帘读出时间: ${stats?.opt("rolling_shutter_skew_ns")}")
        log("温度(电池代理): ${stats?.opt("temperature_c")}")
        log("实际施加: ${applied}")
    }

    private fun fmt2(v: Double?): String =
        if (v == null || v.isNaN()) "?" else String.format("%.2f", v)

    private fun fmt3(v: Double?): String =
        if (v == null || v.isNaN()) "?" else String.format("%.3f", v)

    /** 无小数（行数这类整数用） */
    private fun fmt0(v: Double?): String =
        if (v == null || v.isNaN()) "?" else String.format("%.0f", v)

    private fun fmt4(v: Double?): String =
        if (v == null || v.isNaN()) "?" else String.format("%.4f", v)

    private fun fmt6(v: Double?): String =
        if (v == null || v.isNaN()) "?" else String.format("%.6f", v)

    /** 读 JSON 数值，缺失/为空/非数一律返回 null（避免 optDouble 的 NaN 语义泄漏到判定里）。 */
    private fun jsonDouble(o: JSONObject?, key: String): Double? {
        if (o == null || !o.has(key) || o.isNull(key)) return null
        val v = o.optDouble(key, Double.NaN)
        return if (v.isNaN()) null else v
    }

    private fun jsonLong(o: JSONObject?, key: String): Long? {
        if (o == null || !o.has(key) || o.isNull(key)) return null
        return try { o.getLong(key) } catch (e: Exception) { null }
    }

    companion object {
        private const val REQ_CAMERA = 1001
        /** 扫码配对（`startActivityForResult`）：framework Activity 没有 AndroidX 的
         *  `registerForActivityResult`（本工程 `android.useAndroidX=false`），
         *  故用老 API —— 它被标记废弃但仍然可用，且不引入任何依赖。 */
        private const val REQ_SCAN = 1002
    }
}

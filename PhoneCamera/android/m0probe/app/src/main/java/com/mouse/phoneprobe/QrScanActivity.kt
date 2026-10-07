package com.mouse.phoneprobe

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Matrix
import android.graphics.RectF
import android.graphics.SurfaceTexture
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.media.Image
import android.media.ImageReader
import android.media.ToneGenerator
import android.media.AudioManager
import android.os.Bundle
import android.os.Handler
import android.os.HandlerThread
import android.util.Size
import android.view.Gravity
import android.view.Surface
import android.view.TextureView
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.common.GlobalHistogramBinarizer
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 扫码配对界面：后摄预览 + 实时解码，扫到即回传原文给 [MainActivity]。
 *
 * ## 为什么不把解码放在主界面里
 * 扫码与"采集"会**抢同一个相机**。做成独立 Activity ⇒ 边界清晰：
 * 密码是"进这个界面就说明现在不采集"（主界面在入口处拦一次），
 * 相机被占用时也能给出明确原因而不是让主界面变成"点了一下没反应"。
 *
 * ## 为什么只做"扫到就返回"，不做"扫到就自动连"
 * 解析与连接是两件事：本界面只负责**把二维码原文拿出来**，
 * 交给唯一的解析器 [PairCode]，连不连、填不填由主界面决定。
 * 这样"扫码"这条路和"手输"那条路走的是**同一段代码**，不会出现
 * "扫到的能连、手输同一串连不上"的分叉。
 *
 * ## 三个容易写错的地方（都留了注释）
 *  1. `YUV_420_888` 的 Y 平面 `rowStride` 可能大于图像宽度 ⇒ 必须逐行搬，
 *     直接 `buffer.get(out)` 会在每行末尾串入 padding，图像斜切 ⇒ 永远解不出；
 *  2. 预览要按 `sensorOrientation` 旋转 + center-crop，否则画面是横躺的
 *     （能解出码，但用户以为坏了 —— 界面可信度也是可用性）；
 *  3. 这里用 `acquireLatestImage()` **故意丢旧帧**（与交付道里的
 *     `acquireNextImage()` 相反，那处丢帧会破坏抽帧栅格）。
 *     扫码只要"最新的一帧"，且必须 close，否则 ImageReader 队列满会彻底停摆。
 */
class QrScanActivity : Activity(), TextureView.SurfaceTextureListener {

    companion object {
        /** 解出的原文（String）。 */
        const val EXTRA_TEXT = "pair_text"

        /** 失败原因（String）：相机打不开、没权限等 —— 主界面要把它说出来。 */
        const val EXTRA_ERROR = "scan_error"

        private const val REQ_CAMERA = 41

        /** 解码限速：~6.7 次/秒。相机给 30 fps，逐帧解码只是白烧电。 */
        private const val DECODE_INTERVAL_MS = 150L

        /** 解码用的 YUV 尺寸档（分辨率越高越慢，1280x720 扫屏幕上的码足够）。 */
        private const val TARGET_W = 1280
        private const val TARGET_H = 720
    }

    private lateinit var textureView: TextureView
    private lateinit var hintView: TextView
    private lateinit var stateView: TextView

    private var cameraManager: CameraManager? = null
    private var cameraId: String? = null
    private var sensorOrientation = 90
    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var reader: ImageReader? = null
    private var bgThread: HandlerThread? = null
    private var bg: Handler? = null
    private var previewSize: Size? = null

    private val decoder = MultiFormatReader()
    private val decodeExec = Executors.newSingleThreadExecutor()
    private val decoding = AtomicBoolean(false)
    private val finished = AtomicBoolean(false)
    private var lastDecodeAt = 0L
    private var lastStatsAt = 0L
    private var framesSeen = 0L
    private var framesDecoded = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        cameraManager = getSystemService(CAMERA_SERVICE) as CameraManager
        buildUi()

        val hints = HashMap<DecodeHintType, Any>()
        hints[DecodeHintType.POSSIBLE_FORMATS] = listOf(BarcodeFormat.QR_CODE)
        hints[DecodeHintType.ALSO_INVERTED] = true      // 深底白码也要能扫（有人用暗色主题）
        hints[DecodeHintType.CHARACTER_SET] = "UTF-8"
        decoder.setHints(hints)

        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.CAMERA), REQ_CAMERA)
        }
    }

    // ------------------------------------------------------------------ 界面

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun buildUi() {
        val root = FrameLayout(this)

        textureView = TextureView(this).apply {
            surfaceTextureListener = this@QrScanActivity
        }
        root.addView(textureView, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        // 顶部提示 + 中间"取景框提示"：把人应该对准的地方说出来，
        // 不然黑屏状态下用户不知道该对着哪（终端里那个字符二维码也算数）。
        hintView = TextView(this).apply {
            text = "把电脑上的二维码放进画面里。\n" +
                "电脑端面板顶部有「扫码配对」二维码；终端里打印的字符二维码也可以扫。"
            textSize = 13f
            setTextColor(0xFFFFFFFF.toInt())
            setPadding(dp(12), dp(12), dp(12), dp(12))
            setBackgroundColor(0xCC000000.toInt())
        }
        root.addView(hintView, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            Gravity.TOP))

        val bottom = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setBackgroundColor(0xCC000000.toInt())
            setPadding(dp(12), dp(10), dp(12), dp(10))
        }
        stateView = TextView(this).apply {
            text = "正在打开相机…"
            textSize = 13f
            setTextColor(0xFFFFFFFF.toInt())
        }
        bottom.addView(stateView)
        val cancel = Button(this).apply {
            text = "取消（回主界面手输）"
            setOnClickListener { finishWithError(null) }
        }
        bottom.addView(cancel, LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
        root.addView(bottom, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            Gravity.BOTTOM))

        setContentView(root)
    }

    private fun setState(s: String) {
        runOnUiThread { stateView.text = s }
    }

    // ------------------------------------------------------------------ 权限

    override fun onRequestPermissionsResult(
        requestCode: Int, permissions: Array<out String>, grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQ_CAMERA) {
            if (grantResults.isNotEmpty() && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                if (textureView.isAvailable) openCamera() else setState("等画面就绪…")
            } else {
                // 没权限不是"什么都做不了"：地址还能手输。把出路写出来。
                finishWithError("没有相机权限 ⇒ 无法扫码。可在主界面手输 ip:port 与 token")
            }
        }
    }

    // ------------------------------------------------------------------ 生命周期

    override fun onResume() {
        super.onResume()
        bgThread = HandlerThread("qr-bg").also { it.start() }
        bg = Handler(bgThread!!.looper)
        textureView.post {
            if (textureView.isAvailable && device == null) {
                openCamera()
            } else if (textureView.isAvailable) {
                configureTransform(textureView.width, textureView.height)
            }
        }
    }

    override fun onPause() {
        closeCamera()
        bgThread?.quitSafely()
        bgThread = null
        bg = null
        super.onPause()
    }

    override fun onDestroy() {
        decodeExec.shutdown()
        super.onDestroy()
    }

    override fun onSurfaceTextureAvailable(st: SurfaceTexture, w: Int, h: Int) {
        openCamera()
    }

    override fun onSurfaceTextureSizeChanged(st: SurfaceTexture, w: Int, h: Int) {
        configureTransform(w, h)
    }

    override fun onSurfaceTextureDestroyed(st: SurfaceTexture): Boolean = true

    override fun onSurfaceTextureUpdated(st: SurfaceTexture) {}

    // ------------------------------------------------------------------ 相机

    /** 选后摄：优先 `LENS_FACING_BACK`；没有任何后摄就退到第一个（总比不给用强）。 */
    private fun pickCamera(): String? {
        val cm = cameraManager ?: return null
        return try {
            val ids = cm.cameraIdList
            var fallback: String? = null
            for (id in ids) {
                val ch = cm.getCameraCharacteristics(id)
                if (fallback == null) fallback = id
                if (ch.get(CameraCharacteristics.LENS_FACING) == CameraCharacteristics.LENS_FACING_BACK) {
                    sensorOrientation = ch.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90
                    return id
                }
            }
            // 退档时也要读它自己的朝向，否则预览会躺倒（旋转是按 id 定的，不是全局常量）
            fallback?.let {
                sensorOrientation = cm.getCameraCharacteristics(it)
                    .get(CameraCharacteristics.SENSOR_ORIENTATION) ?: 90
            }
            fallback
        } catch (e: Exception) {
            null
        }
    }

    private fun openCamera() {
        if (device != null) return
        val cm = cameraManager ?: return
        if (checkSelfPermission(Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            setState("等待相机权限…")
            return
        }
        val id = pickCamera()
        if (id == null) {
            finishWithError("这台设备没有可用相机 ⇒ 无法扫码。可在主界面手输 ip:port 与 token")
            return
        }
        cameraId = id
        try {
            val ch = cm.getCameraCharacteristics(id)
            val map = ch.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            // 预览：尽量贴近控件尺寸（RATIO 保持）但不超过 1920 宽
            val viewW = maxOf(textureView.width, 1)
            val viewH = maxOf(textureView.height, 1)
            val previewCandidates = map?.getOutputSizes(SurfaceTexture::class.java) ?: emptyArray()
            previewSize = previewCandidates
                .filter { it.width <= 1920 }
                .minByOrNull { kotlin.math.abs(it.height - viewH) + kotlin.math.abs(it.width - viewW) }
                ?: previewCandidates.firstOrNull()
                ?: Size(TARGET_H, TARGET_W)

            // 解码：最接近 1280x720 的 YUV 档
            val yuv = map?.getOutputSizes(ImageReader::class.java)
                ?.filter { it.width * it.height <= TARGET_W * TARGET_H * 2 }
                ?.minByOrNull {
                    kotlin.math.abs(it.width - TARGET_W) + kotlin.math.abs(it.height - TARGET_H)
                }
                ?: Size(TARGET_W, TARGET_H)
            reader?.close()
            reader = ImageReader.newInstance(yuv.width, yuv.height, android.graphics.ImageFormat.YUV_420_888, 2)
            reader!!.setOnImageAvailableListener({ r -> onFrame(r) }, bg)

            cm.openCamera(id, object : CameraDevice.StateCallback() {
                override fun onOpened(d: CameraDevice) {
                    device = d
                    createSession()
                }

                override fun onDisconnected(d: CameraDevice) {
                    d.close(); device = null
                    setState("相机被其他应用接管了")
                }

                override fun onError(d: CameraDevice, error: Int) {
                    d.close(); device = null
                    // ⚠ 采集会话正在跑时最容易撞上这一条（后摄被占）。
                    // 原因要说清楚，否则用户只会看到"扫码没反应"。
                    finishWithError("相机打不开（error=$error）：可能正在采集、或被其他 App 占用 ⇒ 先停流再扫码")
                }
            }, bg)
        } catch (e: Exception) {
            finishWithError("打开相机失败：${e.javaClass.simpleName} ${e.message ?: ""}")
        }
    }

    private fun createSession() {
        val d = device ?: return
        val st = textureView.surfaceTexture ?: return
        val ps = previewSize ?: return
        try {
            st.setDefaultBufferSize(ps.width, ps.height)
            val previewSurface = Surface(st)
            val target = listOf(previewSurface, reader!!.surface)
            @Suppress("DEPRECATION")
            d.createCaptureSession(target, object : CameraCaptureSession.StateCallback() {
                override fun onConfigured(s: CameraCaptureSession) {
                    session = s
                    try {
                        val req = d.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW)
                        req.addTarget(previewSurface)
                        req.addTarget(reader!!.surface)
                        req.set(CaptureRequest.CONTROL_AF_MODE,
                            CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE)
                        req.set(CaptureRequest.CONTROL_AE_MODE, CaptureRequest.CONTROL_AE_MODE_ON)
                        s.setRepeatingRequest(req.build(), null, bg)
                        runOnUiThread {
                            configureTransform(textureView.width, textureView.height)
                            setState("对准二维码…（会自动对焦）")
                        }
                    } catch (e: Exception) {
                        finishWithError("开始预览失败：${e.message ?: e.javaClass.simpleName}")
                    }
                }

                override fun onConfigureFailed(s: CameraCaptureSession) {
                    finishWithError("相机配置失败（这台设备不支持 预览+YUV 组合）")
                }
            }, bg)
        } catch (e: Exception) {
            finishWithError("创建会话失败：${e.message ?: e.javaClass.simpleName}")
        }
    }

    private fun closeCamera() {
        try { session?.close() } catch (_: Exception) {}
        session = null
        try { device?.close() } catch (_: Exception) {}
        device = null
        try { reader?.close() } catch (_: Exception) {}
        reader = null
    }

    /**
     * 预览变换：按 `sensorOrientation` 旋转 + center-crop 填满控件。
     * 不做这一步的画面是横躺的 —— 码照样能解，但"看起来坏了"本身就是要修的问题。
     */
    private fun configureTransform(viewW: Int, viewH: Int) {
        val ps = previewSize ?: return
        if (viewW <= 0 || viewH <= 0) return
        val m = Matrix()
        val viewRect = RectF(0f, 0f, viewW.toFloat(), viewH.toFloat())
        val cx = viewRect.centerX()
        val cy = viewRect.centerY()
        val rotate = sensorOrientation == 90 || sensorOrientation == 270
        val bw = if (rotate) ps.height.toFloat() else ps.width.toFloat()
        val bh = if (rotate) ps.width.toFloat() else ps.height.toFloat()
        val bufferRect = RectF(0f, 0f, bw, bh)
        bufferRect.offset(cx - bufferRect.centerX(), cy - bufferRect.centerY())
        m.setRectToRect(viewRect, bufferRect, Matrix.ScaleToFit.FILL)
        val scale = maxOf(viewH / bh, viewW / bw)
        m.postScale(scale, scale, cx, cy)
        if (rotate) m.postRotate(sensorOrientation.toFloat(), cx, cy)
        textureView.setTransform(m)
    }

    // ------------------------------------------------------------------ 解码

    private fun onFrame(r: ImageReader) {
        if (finished.get()) return
        // ⚠ 这里**故意**用 acquireLatestImage：扫码只要最新一帧，
        // 丢旧帧是正确行为（交付道那边相反，必须 acquireNextImage 保住抽帧栅格）。
        val img = try { r.acquireLatestImage() } catch (e: Exception) { null } ?: return
        framesSeen++
        try {
            val now = System.currentTimeMillis()
            if (now - lastDecodeAt < DECODE_INTERVAL_MS) return          // 限速
            if (!decoding.compareAndSet(false, true)) return              // 上一帧还在解 ⇒ 丢这帧
            lastDecodeAt = now
            val w = img.width
            val h = img.height
            val data = yPlane(img, w, h)
            decodeExec.execute {
                try {
                    framesDecoded++
                    val text = decodeYuv(data, w, h)
                    if (text != null) onHit(text)
                } catch (e: Exception) {
                    // 解不出是常态（没对准、反光、还没进画面），不打扰用户
                } finally {
                    decoding.set(false)
                }
            }
        } finally {
            // 必须 close：ImageReader 上限 2 张，漏掉一次就永远不再回调（"扫着扫着不动了"）
            img.close()
        }
        // 周期性自述进度。**别小看这两个数**：扫不出来时它们能立刻把原因分成两种 ——
        // "已看 0 帧"（相机/预览没通）vs "已看 300 帧 / 解码 0 次"（限速或解码线程卡住）
        // vs "解码 60 次仍为 0"（确实解不出：没对准/反光/码太小）。没有它们只能瞎猜。
        val nowMs = System.currentTimeMillis()
        if (nowMs - lastStatsAt > 2500) {
            lastStatsAt = nowMs
            val f = framesSeen
            val d = framesDecoded
            runOnUiThread { if (!finished.get()) stateView.text = "对准二维码… 已看 $f 帧 / 解码 $d 次" }
        }
    }

    /**
     * 取 Y 平面灰度。
     *
     * ⚠ `rowStride` 可能大于宽度（硬件对齐 padding）。直接整体读入会让每一行末尾
     * 混进 padding 像素 ⇒ 图像斜切 ⇒ **永远解不出**。所以按行搬。
     * `pixelStride` 对 Y 平面按规范恒为 1，这里仍做一步防御（不满足则按步进采样）。
     */
    private fun yPlane(img: Image, w: Int, h: Int): ByteArray {
        val p = img.planes[0]
        val buf = p.buffer
        val out = ByteArray(w * h)
        buf.rewind()
        val rowStride = p.rowStride
        val pixelStride = p.pixelStride
        if (rowStride == w && pixelStride == 1) {
            buf.get(out, 0, minOf(w * h, buf.remaining()))
            return out
        }
        val row = ByteArray(rowStride)
        for (y in 0 until h) {
            val base = y * rowStride
            if (base >= buf.limit()) break
            buf.position(base)
            val n = minOf(rowStride, buf.remaining())
            buf.get(row, 0, n)
            if (pixelStride == 1) {
                System.arraycopy(row, 0, out, y * w, minOf(w, n))
            } else {
                var x = 0
                var s = 0
                while (x < w && s < n) {
                    out[y * w + x] = row[s]
                    x++
                    s += pixelStride
                }
            }
        }
        return out
    }

    /**
     * 两个二值化器**都试**：`HybridBinarizer` 适合有光照不均的场景（拍屏幕常见反光），
     * `GlobalHistogramBinarizer` 在整体均匀时更稳。多花一点点 CPU 换"能扫出来"。
     */
    private fun decodeYuv(data: ByteArray, w: Int, h: Int): String? {
        val src = PlanarYUVLuminanceSource(data, w, h, 0, 0, w, h, false)
        for (bin in listOf(HybridBinarizer(src), GlobalHistogramBinarizer(src))) {
            try {
                val r = decoder.decode(BinaryBitmap(bin))
                val t = r.text
                if (!t.isNullOrBlank()) return t
            } catch (e: Exception) {
                // NotFoundException 是常态，继续试下一个
            } finally {
                // 不 reset 的话下次 decode 会带着上一次的"未找到"状态
                try { decoder.reset() } catch (_: Exception) {}
            }
        }
        return null
    }

    private fun onHit(text: String) {
        if (!finished.compareAndSet(false, true)) return
        runOnUiThread { setState("已扫到：$text") }
        beep()
        closeCamera()
        runOnUiThread {
            setResult(RESULT_OK, Intent().putExtra(EXTRA_TEXT, text))
            finish()
        }
    }

    /** 成功提示音：扫码必须"看得见/听得见地成功"，否则用户会继续举着手机。 */
    private fun beep() {
        try {
            val t = ToneGenerator(AudioManager.STREAM_MUSIC, 80)
            t.startTone(ToneGenerator.TONE_PROP_BEEP, 120)
            Handler().postDelayed({ try { t.release() } catch (_: Exception) {} }, 400)
        } catch (_: Exception) {
        }
    }

    private fun finishWithError(why: String?) {
        if (!finished.compareAndSet(false, true)) return
        closeCamera()
        val i = Intent()
        if (why != null) i.putExtra(EXTRA_ERROR, why)
        setResult(RESULT_CANCELED, i)
        finish()
    }
}

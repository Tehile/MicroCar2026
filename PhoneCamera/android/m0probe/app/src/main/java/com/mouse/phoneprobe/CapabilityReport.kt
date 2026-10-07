package com.mouse.phoneprobe

import android.content.Context
import android.graphics.ImageFormat
import android.graphics.Rect
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CameraMetadata
import android.hardware.camera2.params.StreamConfigurationMap
import android.os.Build
import android.util.Range
import android.util.Size
import android.util.SizeF
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.abs
import kotlin.math.sqrt

/**
 * 静态能力探测：把 CameraCharacteristics 里与本项目相关的字段导出为 JSON。
 *
 * 注意：能力位（REQUEST_AVAILABLE_CAPABILITIES）与各种模式枚举（CONTROL_AF_MODE_* 等）
 * 都是小整数，必须用各自的映射表，不可混用。
 */
object CapabilityReport {

    private const val MAX_SIZES_PER_FORMAT = 12

    /** 全画幅 36x24mm 的对角线长度，用于计算裁切系数与等效焦距。 */
    private const val FULL_FRAME_DIAGONAL_MM = 43.2666

    fun collect(context: Context): JSONObject {
        val root = JSONObject()
        root.put("report_kind", "m0_capability")
        root.put("generated_at_wall_ms", System.currentTimeMillis())
        root.put("device", JSONObject().apply {
            put("manufacturer", Build.MANUFACTURER)
            put("model", Build.MODEL)
            put("device", Build.DEVICE)
            put("android_release", Build.VERSION.RELEASE)
            put("sdk_int", Build.VERSION.SDK_INT)
            put("abis", JSONArray(Build.SUPPORTED_ABIS.toList()))
            put("build_fingerprint", Build.FINGERPRINT)
        })

        val manager = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val cameras = JSONArray()
        for (id in manager.cameraIdList) {
            cameras.put(describeCamera(id, manager.getCameraCharacteristics(id), manager))
        }
        root.put("cameras", cameras)
        return root
    }

    private fun describeCamera(id: String, c: CameraCharacteristics, manager: CameraManager): JSONObject {
        val o = JSONObject()
        o.put("id", id)
        o.put("lens_facing", lensFacing(c.get(CameraCharacteristics.LENS_FACING)))
        o.put("supported_hardware_level", hardwareLevel(c.get(CameraCharacteristics.INFO_SUPPORTED_HARDWARE_LEVEL)))
        o.put("sensor_orientation_deg", c.get(CameraCharacteristics.SENSOR_ORIENTATION) ?: JSONObject.NULL)

        // ---- 手动对焦能力（R3：决定方案能否成立）----
        val minFocus = c.get(CameraCharacteristics.LENS_INFO_MINIMUM_FOCUS_DISTANCE) ?: 0f
        val hyperFocus = c.get(CameraCharacteristics.LENS_INFO_HYPERFOCAL_DISTANCE) ?: 0f
        val afModes = c.get(CameraCharacteristics.CONTROL_AF_AVAILABLE_MODES)
        val manualFocusSupported =
            (minFocus > 0f) && (afModes?.contains(CameraMetadata.CONTROL_AF_MODE_OFF) == true)

        o.put("focus", JSONObject().apply {
            put("min_focus_distance_diopter", minFocus.toDouble())
            put("hyperfocal_distance_diopter", hyperFocus.toDouble())
            put("min_focus_distance_meters", if (minFocus > 0f) 1.0 / minFocus else JSONObject.NULL)
            put("focus_distance_calibration",
                focusDistanceCalibration(c.get(CameraCharacteristics.LENS_INFO_FOCUS_DISTANCE_CALIBRATION)))
            put("af_available_modes", afModesToNames(afModes))
            put("manual_focus_supported", manualFocusSupported)
        })

        o.put("lens", JSONObject().apply {
            put("focal_lengths_mm", floatArr(c.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)))
            put("apertures", floatArr(c.get(CameraCharacteristics.LENS_INFO_AVAILABLE_APERTURES)))
            put("optical_stabilization_available_modes",
                oisModesToNames(c.get(CameraCharacteristics.LENS_INFO_AVAILABLE_OPTICAL_STABILIZATION)))
        })

        // ---- 逻辑多摄（I8）----
        // zoom 区间下界 < 1.0 只说明"这颗逻辑摄像头含更广的镜头"，
        // 但**没有说明 1.0 档位落在哪颗物理镜头上**，也没说明会不会中途换镜。
        // 换镜 = 焦距/主点/畸变同时跳变，是内参可复现性的直接威胁。
        // 必须把物理摄像头的焦距全部列出来，才能判定 1.0 档位对应哪一颗。
        o.put("multi_camera", describeMultiCamera(c, manager))

        // ---- 光学常数：用于标定时交叉校验（fx 是否落在光学预测值附近）----
        // 若实测 fx 偏离预测值一个焦距比（例如 40%），说明采集时生效的不是这颗镜头。
        o.put("optics", JSONObject().apply {
            val focals = c.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)
            val active = c.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
            val phys: SizeF? = c.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE)
            put("focal_length_mm", if (focals != null && focals.isNotEmpty()) focals[0].toDouble() else JSONObject.NULL)
            if (active != null && active.width() > 0 && phys != null && phys.width > 0f) {
                val pitchUm = phys.width / active.width() * 1000.0
                put("pixel_pitch_um", pitchUm)
                val f = if (focals != null && focals.isNotEmpty()) focals[0].toDouble() else null
                if (f != null) {
                    put("fx_px_full_array", f * 1000.0 / pitchUm)
                }
                val diag = sqrt(phys.width.toDouble() * phys.width + phys.height.toDouble() * phys.height)
                put("sensor_diagonal_mm", diag)
                put("crop_factor", FULL_FRAME_DIAGONAL_MM / diag)
                if (f != null) put("equivalent_focal_length_mm", f * FULL_FRAME_DIAGONAL_MM / diag)
            }
        })

        o.put("zoom", JSONObject().apply {
            put("max_digital_zoom",
                (c.get(CameraCharacteristics.SCALER_AVAILABLE_MAX_DIGITAL_ZOOM) ?: 1f).toDouble())
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                val r: Range<Float>? = c.get(CameraCharacteristics.CONTROL_ZOOM_RATIO_RANGE)
                put("zoom_ratio_range", if (r == null) JSONObject.NULL
                else rangeArr(r.lower.toDouble(), r.upper.toDouble()))
            } else {
                put("zoom_ratio_range", JSONObject.NULL)
            }
        })

        o.put("stabilization", JSONObject().apply {
            put("video_stabilization_available_modes",
                videoStabModesToNames(c.get(CameraCharacteristics.CONTROL_AVAILABLE_VIDEO_STABILIZATION_MODES)))
        })

        // ---- 时间戳（时间轴根基）----
        // 注意：SENSOR_ROLLING_SHUTTER_SKEW 在 android-34 里**不是** CameraCharacteristics 的特征，
        // 而是 CaptureResult 的逐帧 Key，因此只能在 LockedCaptureProbe 里读。
        o.put("timing", JSONObject().apply {
            put("timestamp_source", timestampSource(c.get(CameraCharacteristics.SENSOR_INFO_TIMESTAMP_SOURCE)))
            put("max_frame_duration_ns",
                c.get(CameraCharacteristics.SENSOR_INFO_MAX_FRAME_DURATION) ?: JSONObject.NULL)
        })

        // ---- 畸变矫正（R2：决定标定模型是否必须含畸变）----
        o.put("distortion", JSONObject().apply {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                put("distortion_correction_available_modes",
                    distortionModesToNames(c.get(CameraCharacteristics.DISTORTION_CORRECTION_AVAILABLE_MODES)))
            } else {
                put("distortion_correction_available_modes", JSONArray())
            }
        })

        o.put("sensor", JSONObject().apply {
            val active = c.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
            put("active_array_size",
                if (active == null) JSONObject.NULL else sizeObj(active.width(), active.height()))
            val px = c.get(CameraCharacteristics.SENSOR_INFO_PIXEL_ARRAY_SIZE)
            put("pixel_array_size",
                if (px == null) JSONObject.NULL else sizeObj(px.width, px.height))
            val phys: SizeF? = c.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE)
            put("physical_size_mm",
                if (phys == null) JSONObject.NULL else sizeFObj(phys.width.toDouble(), phys.height.toDouble()))
            val exp: Range<Long>? = c.get(CameraCharacteristics.SENSOR_INFO_EXPOSURE_TIME_RANGE)
            put("exposure_time_range_ns",
                if (exp == null) JSONObject.NULL else rangeArr(exp.lower.toDouble(), exp.upper.toDouble()))
            val iso: Range<Int>? = c.get(CameraCharacteristics.SENSOR_INFO_SENSITIVITY_RANGE)
            put("sensitivity_range_iso",
                if (iso == null) JSONObject.NULL else rangeArr(iso.lower.toDouble(), iso.upper.toDouble()))
            put("color_filter_arrangement",
                c.get(CameraCharacteristics.SENSOR_INFO_COLOR_FILTER_ARRANGEMENT) ?: JSONObject.NULL)
            put("white_level", c.get(CameraCharacteristics.SENSOR_INFO_WHITE_LEVEL) ?: JSONObject.NULL)
        })

        o.put("exposure_control", JSONObject().apply {
            put("ae_available_modes", aeModesToNames(c.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_MODES)))
            put("awb_available_modes", awbModesToNames(c.get(CameraCharacteristics.CONTROL_AWB_AVAILABLE_MODES)))
            val fps: Array<Range<Int>>? = c.get(CameraCharacteristics.CONTROL_AE_AVAILABLE_TARGET_FPS_RANGES)
            val fpsArr = JSONArray()
            fps?.forEach { fpsArr.put(rangeArr(it.lower.toDouble(), it.upper.toDouble())) }
            put("ae_target_fps_ranges", fpsArr)
            put("edge_available_modes", edgeModesToNames(c.get(CameraCharacteristics.EDGE_AVAILABLE_EDGE_MODES)))
            put("noise_reduction_available_modes",
                nrModesToNames(c.get(CameraCharacteristics.NOISE_REDUCTION_AVAILABLE_NOISE_REDUCTION_MODES)))
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                put("aberration_correction_available_modes",
                    aberrationModesToNames(c.get(CameraCharacteristics.COLOR_CORRECTION_AVAILABLE_ABERRATION_MODES)))
            }
        })

        val caps = c.get(CameraCharacteristics.REQUEST_AVAILABLE_CAPABILITIES)
        o.put("capabilities", capsToNames(caps))
        o.put("manual_sensor_supported",
            caps?.contains(CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_MANUAL_SENSOR) == true)
        o.put("burst_capture_supported",
            caps?.contains(CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_BURST_CAPTURE) == true)
        o.put("raw_supported",
            caps?.contains(CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_RAW) == true)
        o.put("private_reprocessing_supported",
            caps?.contains(CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_PRIVATE_REPROCESSING) == true)

        o.put("output_sizes", outputSizes(
            c.get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP),
            c.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)))
        return o
    }

    /**
     * 逻辑多摄探测：列出组成该逻辑摄像头的物理摄像头及其光学参数。
     *
     * 目的只有一个——判定 zoom=1.0 档位落在哪颗镜头、以及是否存在换镜风险。
     * 物理摄像头的焦距是关键证据：主摄约 23mm 等效，超广角约 14mm 等效，
     * 两者相差约 1.67 倍，在像素坐标里体现为 fx 相差同样倍数，绝不可能被误认为同一颗镜头。
     */
    private fun describeMultiCamera(c: CameraCharacteristics, manager: CameraManager): JSONObject {
        val m = JSONObject()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            val ids: Set<String> = try { c.physicalCameraIds } catch (e: Exception) { emptySet() }
            m.put("is_logical", ids.size > 1)
            m.put("physical_camera_count", ids.size)
            m.put("physical_camera_ids", JSONArray(ids.toList()))
            val arr = JSONArray()
            for (pid in ids) {
                arr.put(describePhysicalCamera(manager, pid))
            }
            m.put("physical_cameras", arr)
        } else {
            m.put("is_logical", false)
            m.put("physical_camera_count", 0)
            m.put("physical_camera_ids", JSONArray())
            m.put("physical_cameras", JSONArray())
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            m.put("sensor_sync_type",
                syncType(c.get(CameraCharacteristics.LOGICAL_MULTI_CAMERA_SENSOR_SYNC_TYPE)))
        }
        return m
    }

    private fun describePhysicalCamera(manager: CameraManager, pid: String): JSONObject {
        val o = JSONObject()
        o.put("id", pid)
        val p: CameraCharacteristics = try {
            manager.getCameraCharacteristics(pid)
        } catch (e: Exception) {
            o.put("error", "${e.javaClass.simpleName}: ${e.message}")
            return o
        }
        val focals = p.get(CameraCharacteristics.LENS_INFO_AVAILABLE_FOCAL_LENGTHS)
        val phys: SizeF? = p.get(CameraCharacteristics.SENSOR_INFO_PHYSICAL_SIZE)
        val active = p.get(CameraCharacteristics.SENSOR_INFO_ACTIVE_ARRAY_SIZE)
        o.put("focal_lengths_mm", floatArr(focals))
        o.put("apertures", floatArr(p.get(CameraCharacteristics.LENS_INFO_AVAILABLE_APERTURES)))
        o.put("active_array_size",
            if (active == null) JSONObject.NULL else sizeObj(active.width(), active.height()))
        o.put("physical_size_mm",
            if (phys == null) JSONObject.NULL else sizeFObj(phys.width.toDouble(), phys.height.toDouble()))
        if (phys != null && phys.width > 0f && phys.height > 0f) {
            val diag = sqrt(phys.width.toDouble() * phys.width + phys.height.toDouble() * phys.height)
            o.put("crop_factor", FULL_FRAME_DIAGONAL_MM / diag)
            if (focals != null && focals.isNotEmpty()) {
                o.put("equivalent_focal_length_mm", focals[0] * FULL_FRAME_DIAGONAL_MM / diag)
            }
        }
        return o
    }

    private fun syncType(v: Int?): String = when (v) {
        CameraMetadata.LOGICAL_MULTI_CAMERA_SENSOR_SYNC_TYPE_APPROXIMATE -> "APPROXIMATE"
        CameraMetadata.LOGICAL_MULTI_CAMERA_SENSOR_SYNC_TYPE_CALIBRATED -> "CALIBRATED"
        else -> "UNKNOWN"
    }

    private fun outputSizes(map: StreamConfigurationMap?, active: Rect?): JSONObject {
        val o = JSONObject()
        if (map == null) return o
        val formats = linkedMapOf(
            "JPEG" to ImageFormat.JPEG,
            "YUV_420_888" to ImageFormat.YUV_420_888,
            "PRIVATE" to ImageFormat.PRIVATE,
            "RAW_SENSOR" to ImageFormat.RAW_SENSOR,
            "RAW10" to ImageFormat.RAW10
        )
        val activeAspect = if (active != null && active.height() > 0)
            active.width().toDouble() / active.height() else null
        for ((name, fmt) in formats) {
            val sizes: Array<Size>? = try { map.getOutputSizes(fmt) } catch (e: Exception) { null }
            val entry = JSONObject()
            if (sizes == null || sizes.isEmpty()) {
                entry.put("count", 0)
                entry.put("sizes", JSONArray())
                entry.put("sizes_same_aspect", JSONArray())
                entry.put("largest", JSONObject.NULL)
            } else {
                val sorted = sizes.sortedByDescending { it.width.toLong() * it.height.toLong() }
                val arr = JSONArray()
                sorted.take(MAX_SIZES_PER_FORMAT).forEach { arr.put(sizeObj(it.width, it.height)) }
                entry.put("count", sizes.size)
                entry.put("sizes", arr)
                entry.put("largest", sizeObj(sorted[0].width, sorted[0].height))
                entry.put("sizes_same_aspect", JSONArray().apply {
                    // 与活动阵列**同纵横比**的尺寸单独列一份（不截断）。
                    // 原因：SCALER_CROP_REGION 的纵横比必须与输出一致，否则映射是各向异性的
                    // （4:3 的活动阵列映射到 16:9 输出 = 纵向压扁 25%，fy = 0.75 fx）。
                    // 只有同纵横比尺寸才能在"整个活动阵列无裁切"的前提下保证 fx == fy。
                    if (activeAspect != null) {
                        sorted.filter {
                            it.height > 0 &&
                                abs(it.width.toDouble() / it.height - activeAspect) / activeAspect < 0.01
                        }.forEach { put(sizeObj(it.width, it.height)) }
                    }
                })
            }
            o.put(name, entry)
        }
        return o
    }

    // ---------------- 名称映射（每个枚举族各用各的表）----------------

    private fun lensFacing(v: Int?): String = when (v) {
        CameraMetadata.LENS_FACING_FRONT -> "FRONT"
        CameraMetadata.LENS_FACING_BACK -> "BACK"
        CameraMetadata.LENS_FACING_EXTERNAL -> "EXTERNAL"
        else -> "UNKNOWN"
    }

    private fun hardwareLevel(v: Int?): String = when (v) {
        CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED -> "LIMITED"
        CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_FULL -> "FULL"
        CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_LEGACY -> "LEGACY"
        CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_3 -> "LEVEL_3"
        CameraMetadata.INFO_SUPPORTED_HARDWARE_LEVEL_EXTERNAL -> "EXTERNAL"
        else -> "UNKNOWN"
    }

    private fun timestampSource(v: Int?): String = when (v) {
        CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME -> "REALTIME"
        CameraMetadata.SENSOR_INFO_TIMESTAMP_SOURCE_UNKNOWN -> "UNKNOWN"
        else -> "UNKNOWN"
    }

    private fun focusDistanceCalibration(v: Int?): String = when (v) {
        CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_UNCALIBRATED -> "UNCALIBRATED"
        CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_APPROXIMATE -> "APPROXIMATE"
        CameraMetadata.LENS_INFO_FOCUS_DISTANCE_CALIBRATION_CALIBRATED -> "CALIBRATED"
        else -> "UNKNOWN"
    }

    fun afMode(v: Int?): String = when (v) {
        CameraMetadata.CONTROL_AF_MODE_OFF -> "OFF"
        CameraMetadata.CONTROL_AF_MODE_AUTO -> "AUTO"
        CameraMetadata.CONTROL_AF_MODE_MACRO -> "MACRO"
        CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_VIDEO -> "CONTINUOUS_VIDEO"
        CameraMetadata.CONTROL_AF_MODE_CONTINUOUS_PICTURE -> "CONTINUOUS_PICTURE"
        CameraMetadata.CONTROL_AF_MODE_EDOF -> "EDOF"
        else -> "UNKNOWN"
    }

    fun afState(v: Int?): String = when (v) {
        CameraMetadata.CONTROL_AF_STATE_INACTIVE -> "INACTIVE"
        CameraMetadata.CONTROL_AF_STATE_PASSIVE_SCAN -> "PASSIVE_SCAN"
        CameraMetadata.CONTROL_AF_STATE_PASSIVE_FOCUSED -> "PASSIVE_FOCUSED"
        CameraMetadata.CONTROL_AF_STATE_ACTIVE_SCAN -> "ACTIVE_SCAN"
        CameraMetadata.CONTROL_AF_STATE_FOCUSED_LOCKED -> "FOCUSED_LOCKED"
        CameraMetadata.CONTROL_AF_STATE_NOT_FOCUSED_LOCKED -> "NOT_FOCUSED_LOCKED"
        CameraMetadata.CONTROL_AF_STATE_PASSIVE_UNFOCUSED -> "PASSIVE_UNFOCUSED"
        else -> "UNKNOWN"
    }

    fun aeState(v: Int?): String = when (v) {
        CameraMetadata.CONTROL_AE_STATE_INACTIVE -> "INACTIVE"
        CameraMetadata.CONTROL_AE_STATE_SEARCHING -> "SEARCHING"
        CameraMetadata.CONTROL_AE_STATE_CONVERGED -> "CONVERGED"
        CameraMetadata.CONTROL_AE_STATE_LOCKED -> "LOCKED"
        CameraMetadata.CONTROL_AE_STATE_FLASH_REQUIRED -> "FLASH_REQUIRED"
        CameraMetadata.CONTROL_AE_STATE_PRECAPTURE -> "PRECAPTURE"
        else -> "UNKNOWN"
    }

    fun lensState(v: Int?): String = when (v) {
        CameraMetadata.LENS_STATE_STATIONARY -> "STATIONARY"
        CameraMetadata.LENS_STATE_MOVING -> "MOVING"
        else -> "UNKNOWN"
    }

    fun oisMode(v: Int?): String = when (v) {
        CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_OFF -> "OFF"
        CameraMetadata.LENS_OPTICAL_STABILIZATION_MODE_ON -> "ON"
        else -> "UNKNOWN"
    }

    fun videoStabMode(v: Int?): String = when (v) {
        CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_OFF -> "OFF"
        CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_ON -> "ON"
        CameraMetadata.CONTROL_VIDEO_STABILIZATION_MODE_PREVIEW_STABILIZATION -> "PREVIEW_STABILIZATION"
        else -> "UNKNOWN"
    }

    fun controlMode(v: Int?): String = when (v) {
        CameraMetadata.CONTROL_MODE_OFF -> "OFF"
        CameraMetadata.CONTROL_MODE_AUTO -> "AUTO"
        CameraMetadata.CONTROL_MODE_USE_SCENE_MODE -> "USE_SCENE_MODE"
        CameraMetadata.CONTROL_MODE_OFF_KEEP_STATE -> "OFF_KEEP_STATE"
        else -> "UNKNOWN"
    }

    fun aeMode(v: Int?): String = when (v) {
        CameraMetadata.CONTROL_AE_MODE_OFF -> "OFF"
        CameraMetadata.CONTROL_AE_MODE_ON -> "ON"
        CameraMetadata.CONTROL_AE_MODE_ON_AUTO_FLASH -> "ON_AUTO_FLASH"
        CameraMetadata.CONTROL_AE_MODE_ON_ALWAYS_FLASH -> "ON_ALWAYS_FLASH"
        CameraMetadata.CONTROL_AE_MODE_ON_AUTO_FLASH_REDEYE -> "ON_AUTO_FLASH_REDEYE"
        CameraMetadata.CONTROL_AE_MODE_ON_EXTERNAL_FLASH -> "ON_EXTERNAL_FLASH"
        else -> "UNKNOWN"
    }

    fun awbMode(v: Int?): String = when (v) {
        CameraMetadata.CONTROL_AWB_MODE_OFF -> "OFF"
        CameraMetadata.CONTROL_AWB_MODE_AUTO -> "AUTO"
        CameraMetadata.CONTROL_AWB_MODE_INCANDESCENT -> "INCANDESCENT"
        CameraMetadata.CONTROL_AWB_MODE_FLUORESCENT -> "FLUORESCENT"
        CameraMetadata.CONTROL_AWB_MODE_WARM_FLUORESCENT -> "WARM_FLUORESCENT"
        CameraMetadata.CONTROL_AWB_MODE_DAYLIGHT -> "DAYLIGHT"
        CameraMetadata.CONTROL_AWB_MODE_CLOUDY_DAYLIGHT -> "CLOUDY_DAYLIGHT"
        CameraMetadata.CONTROL_AWB_MODE_TWILIGHT -> "TWILIGHT"
        CameraMetadata.CONTROL_AWB_MODE_SHADE -> "SHADE"
        else -> "UNKNOWN"
    }

    fun distortionMode(v: Int?): String = when (v) {
        CameraMetadata.DISTORTION_CORRECTION_MODE_OFF -> "OFF"
        CameraMetadata.DISTORTION_CORRECTION_MODE_FAST -> "FAST"
        CameraMetadata.DISTORTION_CORRECTION_MODE_HIGH_QUALITY -> "HIGH_QUALITY"
        else -> "UNKNOWN"
    }

    fun edgeMode(v: Int?): String = when (v) {
        CameraMetadata.EDGE_MODE_OFF -> "OFF"
        CameraMetadata.EDGE_MODE_FAST -> "FAST"
        CameraMetadata.EDGE_MODE_HIGH_QUALITY -> "HIGH_QUALITY"
        CameraMetadata.EDGE_MODE_ZERO_SHUTTER_LAG -> "ZERO_SHUTTER_LAG"
        else -> "UNKNOWN"
    }

    fun noiseReductionMode(v: Int?): String = when (v) {
        CameraMetadata.NOISE_REDUCTION_MODE_OFF -> "OFF"
        CameraMetadata.NOISE_REDUCTION_MODE_FAST -> "FAST"
        CameraMetadata.NOISE_REDUCTION_MODE_HIGH_QUALITY -> "HIGH_QUALITY"
        CameraMetadata.NOISE_REDUCTION_MODE_MINIMAL -> "MINIMAL"
        CameraMetadata.NOISE_REDUCTION_MODE_ZERO_SHUTTER_LAG -> "ZERO_SHUTTER_LAG"
        else -> "UNKNOWN"
    }

    fun aberrationMode(v: Int?): String = when (v) {
        CameraMetadata.COLOR_CORRECTION_ABERRATION_MODE_OFF -> "OFF"
        CameraMetadata.COLOR_CORRECTION_ABERRATION_MODE_FAST -> "FAST"
        CameraMetadata.COLOR_CORRECTION_ABERRATION_MODE_HIGH_QUALITY -> "HIGH_QUALITY"
        else -> "UNKNOWN"
    }

    private fun capability(v: Int): String = when (v) {
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_BACKWARD_COMPATIBLE -> "BACKWARD_COMPATIBLE"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_MANUAL_SENSOR -> "MANUAL_SENSOR"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_MANUAL_POST_PROCESSING -> "MANUAL_POST_PROCESSING"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_RAW -> "RAW"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_PRIVATE_REPROCESSING -> "PRIVATE_REPROCESSING"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_READ_SENSOR_SETTINGS -> "READ_SENSOR_SETTINGS"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_BURST_CAPTURE -> "BURST_CAPTURE"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_YUV_REPROCESSING -> "YUV_REPROCESSING"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_DEPTH_OUTPUT -> "DEPTH_OUTPUT"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_CONSTRAINED_HIGH_SPEED_VIDEO -> "CONSTRAINED_HIGH_SPEED_VIDEO"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_MOTION_TRACKING -> "MOTION_TRACKING"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_LOGICAL_MULTI_CAMERA -> "LOGICAL_MULTI_CAMERA"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_MONOCHROME -> "MONOCHROME"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_SECURE_IMAGE_DATA -> "SECURE_IMAGE_DATA"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_SYSTEM_CAMERA -> "SYSTEM_CAMERA"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_OFFLINE_PROCESSING -> "OFFLINE_PROCESSING"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_ULTRA_HIGH_RESOLUTION_SENSOR -> "ULTRA_HIGH_RESOLUTION_SENSOR"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_REMOSAIC_REPROCESSING -> "REMOSAIC_REPROCESSING"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_DYNAMIC_RANGE_TEN_BIT -> "DYNAMIC_RANGE_TEN_BIT"
        CameraMetadata.REQUEST_AVAILABLE_CAPABILITIES_STREAM_USE_CASE -> "STREAM_USE_CASE"
        else -> "UNKNOWN_$v"
    }

    // ---------------- 数组映射 ----------------

    private inline fun mapInts(a: IntArray?, f: (Int) -> String): JSONArray {
        val arr = JSONArray()
        if (a == null) return arr
        for (v in a) arr.put(f(v))
        return arr
    }

    private fun capsToNames(a: IntArray?) = mapInts(a) { capability(it) }
    private fun afModesToNames(a: IntArray?) = mapInts(a) { afMode(it) }
    private fun oisModesToNames(a: IntArray?) = mapInts(a) { oisMode(it) }
    private fun videoStabModesToNames(a: IntArray?) = mapInts(a) { videoStabMode(it) }
    private fun distortionModesToNames(a: IntArray?) = mapInts(a) { distortionMode(it) }
    private fun edgeModesToNames(a: IntArray?) = mapInts(a) { edgeMode(it) }
    private fun nrModesToNames(a: IntArray?) = mapInts(a) { noiseReductionMode(it) }
    private fun aberrationModesToNames(a: IntArray?) = mapInts(a) { aberrationMode(it) }
    private fun aeModesToNames(a: IntArray?) = mapInts(a) { aeMode(it) }
    private fun awbModesToNames(a: IntArray?) = mapInts(a) { awbMode(it) }

    private fun floatArr(a: FloatArray?): JSONArray {
        val arr = JSONArray()
        if (a == null) return arr
        for (v in a) arr.put(v.toDouble())
        return arr
    }

    private fun sizeObj(w: Int, h: Int) = JSONObject().apply { put("w", w); put("h", h) }
    private fun sizeFObj(w: Double, h: Double) = JSONObject().apply { put("w", w); put("h", h) }
    private fun rangeArr(lo: Double, hi: Double) = JSONArray().apply { put(lo); put(hi) }
}

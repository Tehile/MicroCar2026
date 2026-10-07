"""波形指标: 阶跃响应(超调/上升时间/整定时间/稳态误差)与跟踪误差。

用于 1kHz dump 的自动标注, 目标是回答"这次转弯/这段直线的控制好不好"。
所有函数对 numpy 数组操作, 不依赖 GUI。
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np


def _first_cross(t: np.ndarray, y: np.ndarray, level: float, rising: bool = True) -> Optional[float]:
    if len(t) == 0:
        return None
    sign = (y >= level) if rising else (y <= level)
    idx = np.argmax(sign)
    if not sign[idx]:
        return None
    return float(t[idx])


def step_metrics(t: np.ndarray, y: np.ndarray, target: float, band: float = 0.02) -> Dict[str, float]:
    """阶跃响应指标。

    target  : 期望终值(如目标转速)
    band    : 整定带 (±band * |target|)
    返回 rise_s / overshoot_pct / settle_s / sse / peak / final
    """
    out: Dict[str, float] = {}
    if len(t) < 3 or target == 0:
        return out
    t = np.asarray(t, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    y0 = float(y[0])
    amp = target - y0
    if abs(amp) < 1e-9:
        return out
    rising = amp > 0

    lv10 = y0 + 0.10 * amp
    lv90 = y0 + 0.90 * amp
    t10 = _first_cross(t, y, lv10, rising)
    t90 = _first_cross(t, y, lv90, rising)
    if t10 is not None and t90 is not None:
        out["rise_s"] = max(0.0, t90 - t10)

    peak = float(np.max(y)) if rising else float(np.min(y))
    out["peak"] = peak
    out["final"] = float(y[-1])
    denom = abs(amp)
    out["overshoot_pct"] = float((peak - target) / denom * 100.0) if rising else float((target - peak) / denom * 100.0)
    if out["overshoot_pct"] < 0:
        out["overshoot_pct"] = 0.0

    tol = band * abs(target)
    inside = np.abs(y - target) <= tol
    settle_s = None
    for i in range(len(inside) - 1, -1, -1):
        if not inside[i]:
            if i + 1 < len(t):
                settle_s = float(t[i + 1] - t[0])
            break
    else:
        settle_s = 0.0
    if settle_s is not None:
        out["settle_s"] = settle_s

    tail = max(1, len(y) // 10)
    out["sse"] = float(np.mean(y[-tail:]) - target)
    return out


def tracking_error(t: np.ndarray, y: np.ndarray, ref: np.ndarray, tail_frac: float = 0.2) -> Dict[str, float]:
    """跟踪误差: 全程 RMS, 以及末段(近似稳态)的均值与 RMS。"""
    out: Dict[str, float] = {}
    if len(t) == 0:
        return out
    e = np.asarray(y, dtype=np.float64) - np.asarray(ref, dtype=np.float64)
    out["rms"] = float(np.sqrt(np.mean(e * e)))
    out["maxabs"] = float(np.max(np.abs(e)))
    n = max(1, int(len(e) * tail_frac))
    out["tail_mean"] = float(np.mean(e[-n:]))
    out["tail_rms"] = float(np.sqrt(np.mean(e[-n:] ** 2)))
    return out


def segment_by_task(t_ms: np.ndarray, task: np.ndarray) -> List[Dict[str, object]]:
    """把 dump 序列按 motionTask 变化切成若干段。

    这是分析单次转弯/单次直线最实用的入口: 一次动作就是一段。
    """
    segs: List[Dict[str, object]] = []
    if len(t_ms) == 0:
        return segs
    start = 0
    for i in range(1, len(task)):
        if task[i] != task[start]:
            segs.append(
                {
                    "task": int(task[start]),
                    "i0": start,
                    "i1": i - 1,
                    "t0_ms": float(t_ms[start]),
                    "t1_ms": float(t_ms[i - 1]),
                    "duration_ms": float(t_ms[i - 1] - t_ms[start]),
                }
            )
            start = i
    segs.append(
        {
            "task": int(task[start]),
            "i0": start,
            "i1": len(task) - 1,
            "t0_ms": float(t_ms[start]),
            "t1_ms": float(t_ms[-1]),
            "duration_ms": float(t_ms[-1] - t_ms[start]),
        }
    )
    # 过滤掉过短的段(抖动噪声)
    return [s for s in segs if s["duration_ms"] >= 5]


def format_metrics(m: Dict[str, float], unit_s: str = "s") -> str:
    if not m:
        return "—"
    parts = []
    if "rise_s" in m:
        parts.append(f"上升 {m['rise_s']*1000:.0f}ms")
    if "overshoot_pct" in m:
        parts.append(f"超调 {m['overshoot_pct']:.1f}%")
    if "settle_s" in m:
        parts.append(f"整定 {m['settle_s']*1000:.0f}ms")
    if "sse" in m:
        parts.append(f"稳态误差 {m['sse']:+.1f}")
    return "  ".join(parts) if parts else "—"

"""M2: ideal closed-loop dynamics, 1 ms discrete, integer truncation included.

Everything except the motor time constant is taken from the firmware:

    gdvl  = (long)(setvl * SpeedRatioL)          timer.c:1165  truncation
    err   = (int)gdvl - speed_pulse              timer.c:1172
    errs += err, clamp +-1200                    timer.c:1173-1175
    pwm   = kpl*err + kil*errs + kdl*(err-errh)  timer.c:1225
    pwm   = clamp(pwm, +-350)                    timer.c:1231

The plant is deliberately naive (no slip, no noise, no battery sag):

    v_pulse += (K*pwm - v_pulse) * dt / tau_m

`tau_m` and `K` are the only assumed constants; they are tagged as such and
must be calibrated on the real vehicle before any absolute claim.
"""

from ..ir import params as P

SCHEMA_VERSION = "1.0"
ASSUMED = {"tau_m_ms": 10.0, "headroom": 1.2}


def _speed_ratio(ir):
    v = P.value(ir, "SpeedRatioL")
    return v if v else 0.56169


def _pid(ir):
    out = {}
    for k, default in (("kpl", 3.0), ("kil", 2.0), ("kdl", 0.1)):
        v = P.value(ir, k)
        out[k] = v if v is not None else default
    return out


def constants(ir):
    """Everything the JS port needs, in one flat dict (single source of truth)."""
    return {
        "speed_ratio": _speed_ratio(ir),
        "kpl": _pid(ir)["kpl"],
        "kil": _pid(ir)["kil"],
        "kdl": _pid(ir)["kdl"],
        "tau_ms": ASSUMED["tau_m_ms"],
        "headroom": ASSUMED["headroom"],
        "pwm_limit": 350.0,
        "t_max": 3000,
    }


def run(ir, profile_mod, dis=180, p=None, tau_ms=None, dt=1, t_max=3000,
        headroom=None, grade=None):
    tau = tau_ms or ASSUMED["tau_m_ms"]
    head = headroom or ASSUMED["headroom"]
    sr = _speed_ratio(ir)
    pid = _pid(ir)
    p = p or profile_mod.grade_params(ir, grade or "LOW")

    k_gain = head * p["top"] * sr / 350.0

    track = 0.0
    v_pulse = 0.0
    errs = 0.0
    err_h = 0.0
    t = 0
    ch = {"t_ms": [], "s_mm": [], "v_ref": [], "v_act": [], "pwm": [], "err": []}
    pwm_sat = 0
    v_peak = 0.0

    while track < dis and t < t_max:
        v_ref = profile_mod.v_ref(min(track, dis), dis, p)
        gdvl = int(v_ref * sr)
        err = gdvl - v_pulse
        errs += err
        errs = max(-1200.0, min(1200.0, errs))
        pwm = pid["kpl"] * err + pid["kil"] * errs + pid["kdl"] * (err - err_h)
        err_h = err
        if pwm > 350.0:
            pwm = 350.0
            pwm_sat += 1
        elif pwm < -350.0:
            pwm = -350.0
            pwm_sat += 1

        v_pulse += (k_gain * pwm - v_pulse) * dt / tau
        v_cm = v_pulse / sr
        track += v_cm * 0.01 * dt
        v_peak = max(v_peak, v_cm)

        ch["t_ms"].append(t)
        ch["s_mm"].append(round(track, 3))
        ch["v_ref"].append(round(v_ref, 2))
        ch["v_act"].append(round(v_cm, 2))
        ch["pwm"].append(round(pwm, 1))
        ch["err"].append(round(err, 2))
        t += dt

    ref_target = profile_mod.v_ref(dis * 0.5, dis, p)
    overshoot = (v_peak - ref_target) / ref_target * 100.0 if ref_target else 0.0
    return {
        "dis_mm": dis,
        "grade": p.get("grade"),
        "tau_m_ms": tau,
        "assumed": dict(ASSUMED, tau_m_ms=tau, headroom=head),
        "channels": ch,
        "scalars": {
            "T_total_ms": t,
            "v_peak": round(v_peak, 2),
            "overshoot_pct": round(overshoot, 2),
            "pwm_sat_pct": round(100.0 * pwm_sat / max(t, 1), 1),
            "v_avg": round(dis / max(t, 1) * 100.0, 2),
            "speed_ratio": sr,
        },
    }


def step_response(ir, tau_ms=10.0, pwm_step=100.0, n=200):
    """Open-loop step for tau_m calibration on the bench."""
    sr = _speed_ratio(ir)
    k_gain = ASSUMED["headroom"] * 240 * sr / 350.0
    v, out = 0.0, []
    for t in range(n):
        v += (k_gain * pwm_step - v) * 1.0 / tau_ms
        out.append(round(v / sr, 2))
    return {"t_ms": list(range(n)), "v_cm_s": out}

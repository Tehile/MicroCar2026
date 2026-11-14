"""Unit conventions of the firmware.

The firmware mixes two unit systems on purpose and without conversion helpers:

    geometry   : mm          (dis, track, beforeturntrack)
    velocity   : cm/s        (setvl, setvr, speed_ss, LTopSpeed, BottomSpeed)
    encoder    : pulse/ms    (gdvl = setvl * SpeedRatio)
    angle      : degree      (gangle, setgangle, gyro tables)

Because 1 cm/s == 0.01 mm/ms, time integration is:

    dt[ms] = ds[mm] / (v[cm/s] * 0.01)

and the effective wheel track follows from

    b[cm] = integral(|v_out - v_in| dt[ms]) / theta[rad] / 10

Everything derived from these constants is tagged `exact` in the feature
output; anything that needs a physical wheel radius is tagged `derived`.
"""

CM_S_TO_MM_MS = 0.01
MM_MS_TO_CM_S = 100.0

PWM_MAX = 350          # SPEED_HAHA, timer.h:13
PWM_ARR = 1500         # MAXPWM, timer.h:5 -> duty ceiling 23.3 %
ERRS_LIMIT = 1200      # integral clamp, timer.c:1174
PID_DEFAULT = {"kpl": 3.0, "kil": 2.0, "kdl": 0.1,
               "kpr": 3.0, "kir": 2.0, "kdr": 0.0}   # timer.c:75


def mm_per_ms(v_cm_s):
    """cm/s -> mm/ms"""
    return v_cm_s * CM_S_TO_MM_MS


def cm_s(v_mm_ms):
    """mm/ms -> cm/s"""
    return v_mm_ms * MM_MS_TO_CM_S


def integrate_time(ds_mm, v_cm_s):
    """Time [ms] needed to travel ds [mm] at constant v [cm/s]."""
    if v_cm_s <= 0:
        return float("inf")
    return ds_mm / mm_per_ms(v_cm_s)


def duty_ratio(pwm=PWM_MAX, arr=PWM_ARR):
    return pwm / arr

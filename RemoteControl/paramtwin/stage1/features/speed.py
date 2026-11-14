"""F-A speed capability, and F-B cell timing.

`ratio` and `dec` are NOT accelerations: the firmware plans velocity against
*distance*, so v(s) is linear and v(t) is exponential. The physical quantity is
a rate constant k [1/s]; the acceleration at any moment is a = k * v.
"""

from .base import family, feat, r2
from ..model import profile

SRC = "UTILITY/utility.c:1383-1388"


def _p(ir, grade):
    return profile.grade_params(ir, grade)


def speed(ir, grade="LOW", dis=180):
    p = _p(ir, grade)
    a = profile.analyse(dis, p)
    k_acc = 1.5 * p["ratio"]
    k_dec = 5.0 * p["dec"]
    items = [
        feat("F-A.v_set", p["top"], "cm/s", "exact",
             "params:top (%s)" % grade, "TopSpeed, the clamp ceiling"),
        feat("F-A.v_floor", p["bottom"], "cm/s", "exact",
             "params:bottom (%s)" % grade,
             "BottomSpeed; a hard floor, both branches end on +Bottom"),
        feat("F-A.k_acc", r2(k_acc, 3), "1/s", "exact", SRC,
             "rate constant: v=1.5*ratio*s+Bottom"),
        feat("F-A.k_dec", r2(k_dec, 3), "1/s", "exact", SRC,
             "rate constant: v=5*dec*(dis-s)+Bottom"),
        feat("F-A.a_at_floor", r2(k_acc * p["bottom"] / 100.0, 2), "m/s^2",
             "derived", SRC, "instantaneous acceleration when v==v_floor"),
        feat("F-A.s_switch", a["s_switch_mm"], "mm", "exact", SRC,
             "hard switch point 0.5/1.1*dis"),
        feat("F-A.v_acc_end", a["v_acc_end"], "cm/s", "exact", SRC,
             "reference speed at the switch, end of accel branch"),
        feat("F-A.v_dec_start", a["v_dec_start"], "cm/s", "exact", SRC,
             "reference speed at the switch, start of decel branch (clamped)"),
        feat("F-A.v_jump", a["v_jump"], "cm/s", "exact", SRC,
             "discontinuity of the reference at s_switch"),
        feat("F-A.clamp_ratio", a["clamp_ratio"], "1", "exact", SRC,
             "fraction of travel spent at the clamp ceiling"),
        feat("F-A.pwm_headroom", r2(350 / 1500, 4), "1", "exact",
             "HARDWARE/TIMER/timer.h:5,13", "duty ceiling 350/1500"),
    ]
    return family("F-A", "speed capability",
                  "how fast does the table want to run, and how fast can it?",
                  items)


def timing(ir, grade="LOW", dis_list=(180, 254, 360)):
    items = []
    table = []
    for dis in dis_list:
        p = _p(ir, grade)
        a = profile.analyse(dis, p)
        table.append({k: v for k, v in a.items() if k != "points"})
        items.append(feat("F-B.T_cell_%d" % dis, a["T_total_ms"], "ms", "exact",
                          SRC, "dis=%d mm" % dis))
        items.append(feat("F-B.v_avg_%d" % dis, a["v_avg"], "cm/s", "exact",
                          SRC, "dis=%d mm" % dis))
        items.append(feat("F-B.v_eff_%d" % dis,
                          r2(a["v_avg"] / p["top"], 3) if p["top"] else None,
                          "1", "derived", SRC, "v_avg / v_set, ceiling realisation"))
    return family("F-B", "cell timing",
                  "how long does one cell take at this table?", items), table

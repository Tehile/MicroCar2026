"""F-D loop strength and quantisation limits."""

from .base import family, feat, r2
from ..ir import params as P


def ctrl(ir):
    sr = P.value(ir, "SpeedRatioL", 0.56169)
    items = []

    kp = P.by_name(ir).get("turn_kp")
    kd = P.by_name(ir).get("turn_kd")
    if kp:
        for phase, val in kp["scopes"].items():
            items.append(feat("F-D.turn_kp@%s" % phase, val, "1", "exact",
                              "params:turn_kp", "phase %s" % phase))
    if kd:
        items.append(feat("F-D.turn_kd_default", kd["active"]["value"], "1",
                          "exact", "params:turn_kd",
                          "inverse-time damping on the angle error"))

    kp_default = kp["active"]["value"] if kp else None
    items.append(feat("F-D.K_deg2pulse",
                      r2(kp_default * sr, 3) if kp_default else None,
                      "pulse/ms per deg", "derived", "turn_kp * SpeedRatioL",
                      "how hard one degree of error pushes the wheel"))
    items.append(feat("F-D.v_deadband", r2(1.0 / sr, 3), "cm/s", "derived",
                      "1 SpeedRatio", "velocity change per 1 pulse/ms quantum"))
    items.append(feat("F-D.pwm_per_pulse", r2(P.value(ir, "kpl", 3.0), 3), "1",
                      "exact", "HARDWARE/TIMER/timer.c:75", "kpl"))

    dp = P.by_name(ir).get("turn_delay")
    if dp:
        items.append(feat("F-D.turn_delay", dp["active"]["value"], "mm", "exact",
                          "params:turn_delay",
                          "distance, not cells; compared against track"))

    return family("F-D", "control strength",
                  "can a small parameter change even be seen by the loop?", items)

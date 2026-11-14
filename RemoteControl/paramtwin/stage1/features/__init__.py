"""Feature engine: F-A .. F-F, plus the consistency checks used by the report."""

from . import base, ctrl, health, speed, struct, turn

SCHEMA_VERSION = "1.0"

B_EST_VALID = (7.2, 8.0)


def extract(ir, tir):
    fam_a = speed.speed(ir)
    fam_b, timing = speed.timing(ir)
    fam_c, turn_rows = turn.turn(tir)
    fam_d = ctrl.ctrl(ir)
    fam_e, health_rows, duplicates = health.health(tir)
    fam_f, struct_extra = struct.struct(ir, tir)

    warnings = list(tir["warnings"])

    b_vals = [a["b_est_cm"] for a in tir["actions"]
              if a["b_est_cm"] and a["nominal_deg"] >= 90 and a["kind"] == "switch"]
    b_mean = round(sum(b_vals) / len(b_vals), 3) if b_vals else None
    b_spread = round(max(b_vals) - min(b_vals), 3) if b_vals else None
    if b_mean is not None and not (B_EST_VALID[0] <= b_mean <= B_EST_VALID[1]):
        warnings.append({
            "code": "B_EST_OUT_OF_RANGE",
            "detail": "mean b_est %.3f cm outside %s" % (b_mean, (B_EST_VALID,)),
        })

    asym = next((i["value"] for f in (fam_c,) for i in f["items"]
                 if i["key"] == "F-C.asym_90"), None)
    if asym is not None and abs(asym) > 1.0:
        warnings.append({
            "code": "TURN_ASYMMETRY",
            "detail": "left/right 90 deg differ by %.1f deg" % abs(asym),
        })

    return {
        "schema_version": SCHEMA_VERSION,
        "families": [fam_a, fam_b, fam_c, fam_d, fam_e, fam_f],
        "tables": {
            "timing": timing,
            "turn": turn_rows,
            "health": health_rows,
            "duplicates": duplicates,
            "residual": turn.residual(tir),
            "struct": struct_extra,
        },
        "checks": {
            "b_est_mean": b_mean,
            "b_est_spread": b_spread,
            "b_est_valid": (B_EST_VALID[0] <= b_mean <= B_EST_VALID[1])
            if b_mean is not None else None,
        },
        "warnings": warnings,
    }


def flat(feat_doc):
    """key -> value, for CLI printing and golden tests."""
    return {i["key"]: i["value"]
            for f in feat_doc["families"] for i in f["items"]}

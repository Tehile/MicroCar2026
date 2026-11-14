"""Stage1 acceptance tests (V1-V5 + convergence).

Run:  cd paramtwin && python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from stage1 import features                                  # noqa: E402
from stage1.ir import params as P                            # noqa: E402
from stage1.ir import tables as T                            # noqa: E402
from stage1.model import dynamics, playback, profile         # noqa: E402
from stage1.report import build as report_build              # noqa: E402

PARAMS = os.path.join(ROOT, "..", "tools", "params",
                      "params.competition.v3.json")
SRC = os.path.join(ROOT, "..", "code_competition")


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ir = P.load(PARAMS)
        cls.tir = T.load(SRC)
        cls.feat = features.extract(cls.ir, cls.tir)
        cls.flat = features.flat(cls.feat)
        cls.acts = {a["id"]: a for a in cls.tir["actions"]}


class TestV1TableIntegral(Base):
    """V1: the three numbers that define the tables must never drift."""

    def test_lt90(self):
        self.assertEqual(self.acts["LT90SEARCH"]["tick_max"], 189)
        self.assertAlmostEqual(self.acts["LT90SEARCH"]["theta_tab"], 90.0, places=2)

    def test_rt90_over_rotates(self):
        self.assertEqual(self.acts["RT90SEARCH"]["tick_max"], 193)
        self.assertAlmostEqual(self.acts["RT90SEARCH"]["theta_tab"], 92.0, places=2)
        self.assertAlmostEqual(self.acts["RT90SEARCH"]["delta_theta"], 2.0, places=2)

    def test_180_over_rotates(self):
        self.assertAlmostEqual(self.acts["L180L"]["theta_tab"], 186.0, places=2)

    def test_comment_versions_are_ignored(self):
        # user.c keeps 170/201/210/222-element copies commented out and
        # utility.c keeps a 341-element copy inside /* */; the live one is 195.
        self.assertEqual(self.acts["LT90SEARCH"]["n"], 195)
        self.assertEqual(self.acts["LT90L"]["n"], 195)


class TestV2WheelTrack(Base):
    """V2: differential-over-angle must land on a physical wheel track."""

    def test_b_est_in_range(self):
        b = self.feat["checks"]["b_est_mean"]
        self.assertIsNotNone(b)
        self.assertTrue(7.2 <= b <= 8.0, "b_est=%.3f outside [7.2, 8.0]" % b)

    def test_b_est_consistent(self):
        lt = self.acts["LT90SEARCH"]["b_est_cm"]
        h = self.acts["L180L"]["b_est_cm"]
        self.assertLess(abs(lt - h) / h, 0.05)


class TestV3SpeedProfile(Base):
    """V3: the reference profile is a two-slope polyline with a jump."""

    def setUp(self):
        self.p = profile.grade_params(self.ir, "LOW")

    def test_parameters(self):
        self.assertEqual(self.p["top"], 240)
        self.assertEqual(self.p["bottom"], 95)
        self.assertAlmostEqual(self.p["ratio"], 0.7, places=3)

    def test_jump_exists(self):
        a = profile.analyse(180, self.p)
        self.assertGreater(a["v_jump"], 50.0)
        self.assertAlmostEqual(a["s_switch_mm"], 81.82, places=2)

    def test_cell_time(self):
        a = profile.analyse(180, self.p)
        self.assertAlmostEqual(a["T_total_ms"], 111.5, delta=0.6)

    def test_monotonic_in_v_set(self):
        ts = []
        for v in (100, 140, 180, 240, 320, 400):
            p = profile.patch(self.ir, "LOW", top=v)
            ts.append(profile.analyse(180, p)["T_total_ms"])
        self.assertEqual(ts, sorted(ts, reverse=True), ts)

    def test_slow_target_is_clamped_by_top(self):
        p = profile.patch(self.ir, "LOW", top=60)
        a = profile.analyse(180, p)
        self.assertAlmostEqual(a["v_avg"], 60.0, delta=0.5)

    def test_floor_is_not_a_target(self):
        # v_ref is clamped by Top, so a target below Bottom still runs at Top;
        # the floor only binds when Top is left alone and the profile is scaled.
        p = profile.patch(self.ir, "LOW", top=240)
        self.assertGreaterEqual(min(q[1] for q in profile.sample(180, p)), 95.0)


class TestV4Convergence(Base):
    """Grid refinement must not move the answer."""

    def test_step_convergence(self):
        p = profile.grade_params(self.ir, "LOW")
        coarse = profile.analyse(180, p, ds=0.25)["T_total_ms"]
        fine = profile.analyse(180, p, ds=0.05)["T_total_ms"]
        self.assertLess(abs(coarse - fine), 0.3)


class TestV5Overrun(Base):
    """V5: exactly one action reads past its array."""

    def test_only_rv135_overruns(self):
        bad = [a["id"] for a in self.tir["actions"] if a["overrun"]]
        self.assertEqual(bad, ["RV135L"])
        self.assertEqual(self.acts["RV135L"]["overrun"], 3)

    def test_no_duplicate_table_definitions(self):
        codes = [w["code"] for w in self.tir["warnings"]]
        self.assertNotIn("DUPLICATE_TABLE", codes)


class TestModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ir = P.load(PARAMS)
        cls.tir = T.load(SRC)

    def test_playback_theta_matches_table(self):
        r = playback.run(self.tir, "LT90SEARCH")
        self.assertAlmostEqual(r["scalars"]["theta_deg"], 90.0, places=1)
        self.assertEqual(r["scalars"]["t_ms"], 189)

    def test_playback_right_turns_further(self):
        lt = abs(playback.run(self.tir, "LT90SEARCH")["scalars"]["theta_deg"])
        rt = abs(playback.run(self.tir, "RT90SEARCH")["scalars"]["theta_deg"])
        self.assertGreater(rt, lt)

    def test_m2_is_slower_than_m1(self):
        p = profile.grade_params(self.ir, "LOW")
        m1 = profile.analyse(180, p)["T_total_ms"]
        m2 = dynamics.run(self.ir, profile, dis=180)["scalars"]["T_total_ms"]
        self.assertGreater(m2, m1 * 0.98)
        self.assertLess(m2, m1 * 1.15)

    def test_m2_respects_pwm_limit(self):
        r = dynamics.run(self.ir, profile, dis=180)
        self.assertTrue(all(abs(v) <= 350.0 for v in r["channels"]["pwm"]))


class TestFeatures(Base):
    def test_six_families(self):
        self.assertEqual([f["id"] for f in self.feat["families"]],
                         ["F-A", "F-B", "F-C", "F-D", "F-E", "F-F"])

    def test_every_item_has_provenance(self):
        for f in self.feat["families"]:
            for i in f["items"]:
                self.assertTrue(i["source"], "%s has no source" % i["key"])
                self.assertIn(i["method"], ("exact", "derived", "assumed"))

    def test_json_serialisable(self):
        json.dumps(self.feat)


class TestV6JsParity(unittest.TestCase):
    """V6: the slider recalculation in the browser must equal Python exactly."""

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_js_matches_python(self):
        ir = P.load(PARAMS)
        cases, expect = [], []
        for grade in ("LOW", "LOW_90", "BACK"):
            g = profile.grade_params(ir, grade)
            for dis in (90, 180, 254, 360):
                for top in (60, 140, 240, 400):
                    p = {"top": top, "bottom": g["bottom"],
                         "ratio": g["ratio"], "dec": g["dec"]}
                    cases.append({"dis": dis, "p": p})
                    expect.append(profile.analyse(dis, dict(p, grade=grade)))

        js = subprocess.run(
            ["node", os.path.join(ROOT, "tests", "parity.cjs"),
             json.dumps({"cases": cases})],
            capture_output=True, text=True)
        self.assertEqual(js.returncode, 0, js.stderr)
        out = json.loads(js.stdout)["m1"]

        for py, j in zip(expect, out):
            # Python rounds to 2 decimals for display; compare at that scale.
            self.assertAlmostEqual(py["T_total_ms"], j["T"], places=2)
            self.assertAlmostEqual(py["v_avg"], j["vAvg"], places=2)
            self.assertAlmostEqual(py["v_jump"], j["vJump"], places=2)
            self.assertAlmostEqual(py["clamp_ratio"], j["clampRatio"], places=2)
            self.assertAlmostEqual(py["s_switch_mm"], j["sw"], places=2)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_m2_js_matches_python(self):
        """M2 depends on truncation and clamp order; both must be identical."""
        ir = P.load(PARAMS)
        c = dynamics.constants(ir)
        cases, expect = [], []
        for grade in ("LOW", "BACK"):
            g = profile.grade_params(ir, grade)
            for dis in (180, 360):
                for top in (140, 240):
                    p = {"top": top, "bottom": g["bottom"],
                         "ratio": g["ratio"], "dec": g["dec"]}
                    cases.append({"dis": dis, "p": p})
                    expect.append(dynamics.run(ir, profile, dis=dis, p=p))
        js = subprocess.run(
            ["node", os.path.join(ROOT, "tests", "parity.cjs"),
             json.dumps({"cases": cases, "m2": c})],
            capture_output=True, text=True)
        self.assertEqual(js.returncode, 0, js.stderr)
        out = json.loads(js.stdout)["m2"]
        for py, j in zip(expect, out):
            self.assertEqual(py["scalars"]["T_total_ms"], j["T"], py["dis_mm"])
            self.assertAlmostEqual(py["scalars"]["v_peak"], j["vPeak"], places=2)
            self.assertAlmostEqual(py["scalars"]["v_avg"], j["vAvg"], places=2)
            self.assertAlmostEqual(py["scalars"]["pwm_sat_pct"], j["satRatio"] * 100,
                                   places=1)



class TestV7Golden(unittest.TestCase):
    """V7: nothing may drift silently. Regenerate with `python tests/gen_golden.py`."""

    def test_snapshot_matches(self):
        sys.path.insert(0, os.path.join(ROOT, "tests"))
        import gen_golden
        with open(gen_golden.GOLDEN, encoding="utf-8") as fh:
            golden = json.load(fh)
        d = gen_golden.diff(golden, gen_golden.snapshot())
        self.assertEqual(d, [], "\n".join(d))


class TestReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ir = P.load(PARAMS)
        tir = T.load(SRC)
        feat = features.extract(ir, tir)
        cls.data = report_build.collect(ir, tir, feat)
        cls.html = report_build.render(cls.data, os.path.join(ROOT, "out",
                                                              "_test_report.html"))[0]

    def test_offline(self):
        with open(self.html, encoding="utf-8") as fh:
            body = fh.read()
        for bad in ("http://", "https://", "cdn."):
            self.assertNotIn(bad, body)
        self.assertIn("application/json", body)

    def test_payload_keys(self):
        for k in ("meta", "families", "grades", "playback", "tables", "params"):
            self.assertIn(k, self.data)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_dom_smoke(self):
        r = subprocess.run(
            ["node", os.path.join(ROOT, "tests", "dom_smoke.cjs"), self.html],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["kpis"], 8)
        # every non-empty action must be selectable, and every chart must draw
        self.assertEqual(out["act_options"], 21)
        for cid, ops in out["canvas_ops"].items():
            self.assertGreater(ops, 20, "%s drew nothing" % cid)

    @classmethod
    def tearDownClass(cls):
        for p in ("_test_report.html",):
            f = os.path.join(ROOT, "out", p)
            if os.path.exists(f):
                os.remove(f)


if __name__ == "__main__":
    unittest.main(verbosity=2)

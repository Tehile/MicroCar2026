/* Parity harness.
   node tests/parity.cjs '{"cases":[{"dis":180,"p":{...}}],"m2":{...}}'
   Every field it prints is compared against Python by tests/test_stage1.py. */
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(
  path.join(__dirname, "..", "stage1", "report", "assets", "model.js"), "utf8");
(0, eval)(src);

const req = JSON.parse(process.argv[2] || "{}");
const cases = req.cases || [];

const m1 = cases.map(function (c) {
  const a = globalThis.PTM.analyse(c.dis, c.p);
  return {
    T: a.T, vAvg: a.vAvg, vJump: a.vJump,
    Tacc: a.Tacc, Tcru: a.Tcru, Tdec: a.Tdec,
    clampRatio: a.clampRatio, sw: a.sw
  };
});

let m2 = [];
if (req.m2) {
  m2 = cases.map(function (c) {
    const r = globalThis.PTM.m2(c.dis, c.p, req.m2);
    return { T: r.T, vPeak: r.vPeak, satRatio: r.satRatio, vAvg: r.vAvg };
  });
}

process.stdout.write(JSON.stringify({ m1: m1, m2: m2 }));

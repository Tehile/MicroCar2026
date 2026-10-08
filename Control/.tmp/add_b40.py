# -*- coding: utf-8 -*-
"""一次性：把 B40 判据插进 tools/browser_check.py（B37 之后、B38 之前）。
用脚本写文件而不是逐条 Edit —— 上一轮 Edit 回执说成功、事后 grep 却没有，
必须"写完立刻核验"（铁律 44）。跑完即可删。"""
import io
import sys

TARGET = 'tools/browser_check.py'
ANCHOR = '            # ── B38：改版十四的五件小事'

BLOCK = r'''            # ── B40：迷宫绘图落位 —— 前墙两档**分边** + 最底行不缺竖线（改版十八） ──
            # 守的是现场报的两条（都是"看图就能发现"的错，而 B37 只读左栏文字与画布
            # 尺寸，**看不见画在哪** ⇒ 必须像素级判据才钉得住）：
            #   ① 近端墙（`frontNear`，< 80 mm 贴脸）落在**本格**前沿 `PY(y)`；
            #      远端墙（`frontFar`，≥ 80 mm 提前量）落在**外一格**前沿 `PY(y)-cs`
            #      —— 两档**必须分边**。早先两档都落在 `PY(y+1)`，于是"贴脸"看着像隔了一格，
            #      照图判断"还有一格能走"就撞了。
            #   ② 每一行的竖线都在本格**整高** `PY(y)`→`PY(y)+cs` 内。格 (x,y) 的屏幕
            #      纵向范围就是 `[PY(y), PY(y)+cs]`；早先竖线跨 `PY(y)→PY(y+1)`
            #      （整体上移一格）⇒ **最底行一条竖线都没有**。
            # 手法（**不复制绘图端的几何公式**，否则改实现它跟着变、永远绿）：
            #   · 先渲染一张"每格四面全有墙"的图 ⇒ 白格线自己把网格位置暴露出来，
            #     `PY(0)` 就是第 `n-1` 条横线（底格上沿），外一格就是第 `n-2` 条；
            #   · 前墙那条线用**逐像素相减**摘出来：与"没有前墙"那一张比，差异像素
            #     **就是**那条线本身（不按颜色认，免得和琥珀色小车/起点圈混淆）。
            # 负向（两条都要"造得出来"）：
            #   ① 拿**只有远端墙**那张去按"近端墙应落在本格前沿"验 ⇒ 必须判红；
            #   ② 自绘一张**故意不画最底行竖线**的图，用同一个探测器验 ⇒ 必须报缺。
            head("2i-2. 迷宫绘图：前墙两档分边 + 每行不缺竖线（像素级，含负向）")
            b40_bad, b40_got = [], []
            b40 = {}
            try:
                b40 = await cdp.eval(r"""(()=>{
                  const N = 7, D = window.devicePixelRatio || 1, R = {};
                  const ALL = []; for (let i=0;i<N;i++) ALL.push('f'.repeat(N));
                  const mk = function(){ const c = document.createElement('canvas');
                    c.width = Math.round(400*D); c.height = Math.round(320*D);
                    c.style.cssText = 'position:absolute;left:-99999px;top:0;'
                      + 'width:400px;height:320px;display:block;';
                    document.body.appendChild(c); return c; };
                  const cv = mk(), cv2 = mk();
                  try {
                    /* 画一张"四面全有墙"的迷宫：白线即格线，位置由画布自己给出 */
                    const draw = function(c, front){ C.draw.maze(c, {
                        height:320, n:N, known:ALL, wall:ALL, visited:null,
                        pos:[0,0], dir:0, front:front, label:''});
                      return c.getContext('2d').getImageData(0,0,c.width,c.height); };
                    /* 网格线白 = #e8e8ea（近白、低饱和）；小车/起点圈是琥珀、地面是深蓝
                       ⇒ 都不算（不靠"是不是白色"认墙，避免把图例文字算进来） */
                    const isW = function(d,i){ const r=d[i],g=d[i+1],b=d[i+2];
                      return r>195 && g>195 && b>195
                             && (Math.max(r,g,b)-Math.min(r,g,b)) < 24; };
                    /* 把"每列/每行有多少白像素"聚类成一条线。
                       ⚠️ 不能"阈值以上就算一条"：线宽 2.2 px 在整数像素上跨 3~4 列，
                       而**抗锯齿**让边缘列的混合色偏暗 ⇒ 时过时不通过（第一版就这样
                       数出 14 条竖线/12 条横行）。按间距聚类对半深边缘不敏感。 */
                    const cluster = function(a, gap){
                      const out = []; let s = -1, p = -1;
                      for (let i=0;i<a.length;i++){
                        if (s < 0) { s = i; p = i; continue; }
                        if (a[i] - p <= gap) { p = i; continue; }
                        out.push(Math.round((a[s] + a[p]) / 2));
                        s = i; p = i;
                      }
                      if (s >= 0) out.push(Math.round((a[s] + a[p]) / 2));
                      return out; };
                    const scan = function(im){ const w=im.width,h=im.height,d=im.data;
                      const cols=new Int32Array(w), rows=new Int32Array(h);
                      for (let y=0;y<h;y++) for (let x=0;x<w;x++){
                        if (isW(d,(y*w+x)*4)) { cols[x]++; rows[y]++; } }
                      /* 间隙 >4 px 才算两条不同的线（格距 ≈40 px，远大于 4
                         ⇒ 绝不会把相邻格线并成一条） */
                      const pick = function(a){ let m=0;
                        for (let i=0;i<a.length;i++) if (a[i]>m) m=a[i];
                        const idx=[]; for (let i=0;i<a.length;i++)
                          if (a[i] >= m*0.6) idx.push(i);
                        return cluster(idx, 4); };
                      return { xs:pick(cols), ys:pick(rows) }; };
                    const dbox = function(a,b){ const w=a.width;
                      let mnx=1e9,mxx=-1,mny=1e9,mxy=-1,c=0,sy=0;
                      for (let i=0;i<a.data.length;i+=4){
                        if (a.data[i]!==b.data[i] || a.data[i+1]!==b.data[i+1]
                            || a.data[i+2]!==b.data[i+2]){
                          const p=(i>>2), x=p%w, y=(p/w)|0; c++; sy+=y;
                          if (x<mnx) mnx=x; if (x>mxx) mxx=x;
                          if (y<mny) mny=y; if (y>mxy) mxy=y; } }
                      return { n:c, my: c ? sy/c : -1, x0:mnx, x1:mxx, y0:mny, y1:mxy }; };
                    /* 这一行里数"竖线有几条"（按白像素段聚类，一条线可能占好几列） */
                    const vlinesInRow = function(im,y){ const w=im.width,d=im.data;
                      const idx=[]; for (let x=0;x<w;x++)
                        if (isW(d,(y*w+x)*4)) idx.push(x);
                      return cluster(idx, 4).length; };
                    /* 归一化到 CSS px（自绘图要按 dpr 画） */
                    const S = function(v){ return v / D; };

                    const base = draw(cv, null);
                    const L = scan(base), xs = L.xs, ys = L.ys;
                    R.w = base.width; R.h = base.height;
                    R.nx = xs.length; R.ny = ys.length;
                    if (xs.length !== N+1 || ys.length !== N+1) {
                      R.fatal = '白格线聚类得到竖线 ' + xs.length + ' 条、横线 ' + ys.length
                              + ' 条（应各 ' + (N+1) + ' 条）';
                      return R;
                    }
                    R.csx = Math.round((xs[xs.length-1]-xs[0]) / N);
                    R.csy = Math.round((ys[ys.length-1]-ys[0]) / N);
                    R.expNearY = ys[N-1];      // = PY(0)      本格（底格）上沿
                    R.expFarY  = ys[N-2];      // = PY(0)-cs   外一格上沿

                    const nD = dbox(draw(cv, {near:true, far:false}), base);
                    const fD = dbox(draw(cv, {near:false, far:true }), base);
                    const r1 = function(v){ return Math.round(v*10)/10; };
                    R.near = { n:nD.n, my:r1(nD.my), y0:nD.y0, y1:nD.y1 };
                    R.far  = { n:fD.n, my:r1(fD.my), y0:fD.y0, y1:fD.y1 };

                    /* 每一行都得有 n+1 条竖线（缺格线就是这里少） */
                    R.rowLines = [];
                    for (let y=0; y<N; y++) {
                      const mid = Math.round((ys[y] + ys[y+1]) / 2);
                      R.rowLines.push(vlinesInRow(base, mid));
                    }
                    R.minRowLines = Math.min.apply(null, R.rowLines);

                    /* 负向①：把"只有远端墙"那张按"近端墙该落在本格前沿"验 ⇒ 应当判红 */
                    R.negFrontGap = Math.round(Math.abs(fD.my - ys[N-1]));

                    /* 负向②：自绘一张故意不画最底行竖线的图，用同一探测器 ⇒ 应当报缺 */
                    const g2 = cv2.getContext('2d');
                    g2.setTransform(D,0,0,D,0,0);
                    g2.fillStyle = '#101419'; g2.fillRect(0,0,400,320);
                    g2.strokeStyle = '#e8e8ea'; g2.lineWidth = 2;
                    const csS = S(ys[N] - ys[0]) / N;
                    for (let k=0;k<=N;k++){ const x = S(xs[0]) + k*csS;
                      g2.beginPath(); g2.moveTo(x, S(ys[1])); g2.lineTo(x, S(ys[N-1]));
                      g2.stroke(); }
                    const im2 = g2.getImageData(0,0,cv2.width,cv2.height);
                    R.negBotLines = vlinesInRow(im2, Math.round((ys[N-1]+ys[N])/2));
                    R.negTopLines = vlinesInRow(im2, Math.round((ys[0]+ys[1])/2));
                    return R;
                  } finally { cv.remove(); cv2.remove(); }
                })()""")
                b40 = b40 if isinstance(b40, dict) else {}
                if b40.get("fatal"):
                    b40_bad.append("白格线探测失败：" + str(b40.get("fatal")))
                else:
                    near, far = b40.get("near") or {}, b40.get("far") or {}
                    expN, expF = b40.get("expNearY"), b40.get("expFarY")
                    cs = b40.get("csy") or 1
                    rows_l = b40.get("rowLines") or []
                    b40_got.append(
                        f"格距≈{cs}px，近端墙 y≈{near.get('my')}（应 {expN}）、"
                        f"远端墙 y≈{far.get('my')}（应 {expF}）；"
                        f"逐行竖线数={rows_l}（最少 {b40.get('minRowLines')}）")
                    if near.get("n", 0) < 5:
                        b40_bad.append("近端墙几乎没画出来（与'无前墙'那一张的差异像素只有 "
                                       f"{near.get('n')} 个）")
                    elif abs(float(near.get("my", -999)) - expN) > 3:
                        b40_bad.append(
                            f"**近端墙落错边**：画在 y≈{near.get('my')}，本格前沿在 y={expN}"
                            f"（差 {round(abs(float(near.get('my')) - expN))} px ≈ "
                            f"{round(abs(float(near.get('my')) - expN) / float(cs) * 10) / 10} 格）"
                            " —— 贴脸的墙必须就在车头这一格边上")
                    if far.get("n", 0) < 5:
                        b40_bad.append("远端墙几乎没画出来（与'无前墙'那一张的差异像素只有 "
                                       f"{far.get('n')} 个）")
                    elif abs(float(far.get("my", -999)) - expF) > 3:
                        b40_bad.append(
                            f"**远端墙落错边**：画在 y≈{far.get('my')}，外一格前沿在 y={expF}"
                            f"（差 {round(abs(float(far.get('my')) - expF))} px）")
                    if (near.get("n", 0) >= 5 and far.get("n", 0) >= 5
                            and abs(float(near.get("my")) - float(far.get("my"))) < 3):
                        b40_bad.append("**前墙两档落在同一条边上**（y 坐标相同）"
                                       " —— 贴脸与提前量必须分边，否则看着像隔了一格")
                    if (b40.get("minRowLines") or 0) < 8:
                        bad_rows = [i for i, v in enumerate(rows_l) if v < 8]
                        b40_bad.append(
                            f"**有 {len(bad_rows)} 行竖线不足 8 条**（自下往上第 "
                            f"{[r + 1 for r in bad_rows][:4]} 行，最少 "
                            f"{b40.get('minRowLines')} 条）—— 格 (x,y) 的纵向范围是 "
                            "[PY(y), PY(y)+cs]，竖线跨错一格 ⇒ 那一行看起来没有格线")
                    if not b40_bad:
                        b40_got.append(f"负向①「拿远端墙当近端墙验」如期判红"
                                       f"（差 {b40.get('negFrontGap')} px）")
                        if int(b40.get("negFrontGap") or 0) <= 3:
                            b40_bad.append("负向①失效：远端墙恰好落在本格前沿上，"
                                           "这条判据区分不出两档（等于没钉住）")
                        else:
                            b40_got.append("负向②自绘图缺最底行竖线，探测器报 "
                                           f"{b40.get('negBotLines')} 条"
                                           f"（顶行 {b40.get('negTopLines')} 条）")
                            if int(b40.get("negBotLines") or 99) >= 8:
                                b40_bad.append("负向②失效：那张故意缺最底行竖线的图，"
                                               "探测器却数出了 ≥8 条 ⇒ 探测器认不出缺线")
            except Exception as e:                            # noqa: BLE001
                b40_bad.append(f"做像素级前墙/格线断言时抛异常：{type(e).__name__}: {e}")
            b40_ok = not b40_bad
            record("B40", "PASS" if b40_ok else "FAIL",
                   f"迷宫图上前墙两档**分边**（近端墙落在本格前沿、远端墙落在外一格前沿）"
                   f"且逐行不缺竖线（{'；'.join(b40_got)}）"
                   if b40_ok else
                   f"**迷宫绘图的落位/格线不对**：{b40_bad[:4]}")

'''

src = io.open(TARGET, encoding='utf-8').read()
if 'record("B40"' in src:
    print('ALREADY_PRESENT')
    sys.exit(0)
if ANCHOR not in src:
    print('ANCHOR_NOT_FOUND')
    sys.exit(2)
if src.count(ANCHOR) != 1:
    print('ANCHOR_AMBIGUOUS x%d' % src.count(ANCHOR))
    sys.exit(3)
src = src.replace(ANCHOR, BLOCK + ANCHOR)
io.open(TARGET, 'w', encoding='utf-8', newline='').write(src)

# 写完立刻核验（不核验就等于没写 —— 铁律 44）
chk = io.open(TARGET, encoding='utf-8').read()
n = chk.count('record("B40"')
print('WROTE_OK  record(B40) x%d  size=%d' % (n, len(chk)))
sys.exit(0 if n == 1 else 4)
/* 二进制 WS 帧编解码 + 全局命名空间。
   格式（设计方案 §6.2）：
     [0:4]  uint32 header_len (小端)
     [4:4+h] JSON header (UTF-8)
     [..]   payload: Float32Array(小端, 行主序)
   前端零解析：直接 new Float32Array(payload)，不做 JSON.parse 每个数。 */

/* `views` = 原有九页（属于两栏/底栏）；`mods` = 子系统模块（属于某个子系统的子页面）。
   两者分开注册：模块不参与两栏的页签计数（EXPECTED_VIEWS 只数 views）。

   ⚠️ **逐项建，不要整体覆盖**。写成 `window.C = window.C || {views, mods, ...}`
   时，只要 `C` 已经存在（旧缓存里的同名文件、或别的脚本先建过），这一行就
   **整句不生效**，新增的命名空间被静默丢掉 —— 然后在别处炸成一句看不出原因的
   TypeError（实测：`reading 'mod_params'`，整页空壳）。 */
window.C = window.C || {};
C.views = C.views || {};
C.mods = C.mods || {};
C.util = C.util || {};
C.state = C.state || {};
C.onBin = C.onBin || {};

window.WSProto = {
  HEADER_LEN: 4,

  decode(buf) {
    const dv = new DataView(buf);
    const hlen = dv.getUint32(0, true);
    const hbytes = new Uint8Array(buf, 4, hlen);
    let meta = {};
    try {
      meta = JSON.parse(new TextDecoder('utf-8').decode(hbytes));
    } catch (e) {
      meta = { type: 'decode.error', message: String(e) };
    }
    const payload = buf.slice(4 + hlen);
    // Float32Array 的 byteOffset 必须是 4 的倍数；slice 出来的是新 buffer，偏移为 0
    const floats = payload.byteLength % 4 === 0 ? new Float32Array(payload) : new Float32Array(0);
    return { meta, payload, floats };
  },

  /* 把 shape=[N,C] 的浮点数组取第 c 列整列（避免每帧重新切片的开销） */
  col(floats, n, c) {
    const out = new Float32Array(n);
    for (let i = 0; i < n; i++) out[i] = floats[i * c];
    return out;
  },

  /* 行主序 → 列视图，不复制 */
  isRowMajor: true
};

C.util.fmt = function (v, d) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(d === undefined ? 3 : d);
  return String(v);
};

C.util.pad = function (s, n) { s = String(s); return s.length >= n ? s : ' '.repeat(n - s.length) + s; };

C.util.esc = function (s) {
  return String(s === null || s === undefined ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
};

/* 统一的"这个值代表什么"三态渲染 —— 判据 I11：
   complete=null 绝不渲染成"通过"，必须显示"判不了"。 */
C.util.tri = function (v, trueText, falseText, nullText) {
  if (v === true) return '<span class="tag ok">' + (trueText || '通过') + '</span>';
  if (v === false) return '<span class="tag bad">' + (falseText || '不通过') + '</span>';
  return '<span class="tag warn">' + (nullText || '判不了') + '</span>';
};

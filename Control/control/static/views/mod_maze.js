/* 定位 · 迷宫结构解析 —— **尚未接通，先空着**。

   上游（Auto Locate）已经把算法做出来了：独立脚本 `maze_extract.py` 的
   `extract_maze(frame_bgr, H_img2field, cfg)` —— 从红色墙线解出 8×8 迷宫结构并出图。
   它不进 `aruco_locator` 包（v1.x 对外接口保持冻结），要求 **BGR 三通道**。

   卡在哪一步：**Control 手里没有彩色帧。**
   PhoneCamera 的交付道落盘的是**原始 Y 平面**（灰度），Control 的 `decode_y()` 也只是把它
   铺成三通道；喂给 `extract_maze` 等于一张没有红色的图，解不出墙。
   彩色只在**实时道**（`capture_mode=live`）里存在，而实时道**不落盘**。

   所以这一步需要一个"彩色帧的落盘来源"才能做，属于上游能力，不在本层伪造。
   详见 `docs/未实现功能.md`。 */
C.mods.mod_maze = (function () {
  function mount(host) {
    host.innerHTML = `
      <div class="pane">
        <h4>迷宫结构解析</h4>
        <div class="hint">未接通</div>
        <div class="kv" style="margin-top:6px">
          <b>上游接口</b><span><code>maze_extract.extract_maze(frame_bgr, H_img2field, cfg)</code></span>
          <b>输入要求</b><span>BGR 三通道（从红色墙线解墙）</span>
          <b>现状</b><span>交付帧只有 Y 平面（灰度），彩色只在实时道且不落盘</span>
          <b>缺什么</b><span>彩色帧的落盘来源（属上游能力）</span>
        </div>
      </div>`;
  }
  return { mount: mount, activate: function () { }, onHealth: function () { } };
})();

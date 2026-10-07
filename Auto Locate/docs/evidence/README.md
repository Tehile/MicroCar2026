# 验证证据留档

这里是 2026-10-03 新样本验证过程中的**原始输出留档**（未修改一字）：

| 文件 | 内容 |
|---|---|
| `validate_before.txt` | v1.0.0 默认（`auto` + `vertex_margin_weight=0.02`）的完整分析输出 |
| `validate_fixed.txt` | v1.0.1 默认（`auto` + `w=0.1`）的完整分析输出 |
| `validate_20261003_analysis.json` | 36 帧逐帧明细（位置/角度/flags/内顶点诊断） |

⚠ 这三个文件是**捕获的原样输出**，其中的路径（如 `out/...`）指向当时的临时输出目录，
该目录已在 2026-10-04 整理项目时清空 —— 保留原文是为了留下不可篡改的验证痕迹，
路径不影响结论。

重新生成方式见 [验证报告 §7](../验证报告-2026-10-03新样本.md)：

```bash
python tools/validate_new_samples2.py "samples/IMG_20261003*.jpg"          # 当前默认
python tools/validate_new_samples2.py "samples/IMG_20261003*.jpg" nearest  # 只走最近角判据
```

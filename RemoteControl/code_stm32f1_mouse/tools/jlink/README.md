# J-Link 在线诊断脚本

真机排查用。**没有示波器时，靠读内存变量、外设寄存器与引脚电平即可完成大部分判断。**

用法（在 `code_stm32f1_mouse/` 下执行）：

```bash
JL="C:/Program Files/SEGGER/JLink_V976a/JLink.exe"
"$JL" -CommanderScript tools/jlink/jl_state.jlink -ExitOnError 1
```

注意：**Keil 正在调试时会独占 J-Link**，先停止调试再跑脚本。

## 脚本清单

| 脚本 | 作用 | 关键看点 |
|---|---|---|
| `jl_probe.jlink` | 连接并 halt，打印寄存器现场 | `IPSR` 非正常异常号 = 卡在异常里；`PC` 落在哪个函数 |
| `jl_state.jlink` | 读链路变量 + USART/DMA/RCC 寄存器，`go` 5 秒后再读一次 | tick/帧计数**增量**；`DMA->CNDTR` 是否递减；`BRR` 反算波特率 |
| `jl_pins.jlink` | 读重映射与 GPIO 配置 | `AFIO->MAPR` bit2 = USART1 是否重映射；`GPIOA->CRH` 中 PA9 是否 `0xB`(复用推挽) |
| `jl_scope.jlink` | **连续采样 `GPIOA->IDR` 与 `DMA->CNDTR`** | IDR 的 bit9 在 0/1 间跳变 = **PA9 上真有数据波形** |
| `jl_rate.jlink` | 隔 5 秒读两次帧计数与分频 | 帧率 = 增量 / 5；对照 快+中+慢 三路速率之和 |
| `jl_slowdown.jlink` | **运行时**把快帧分频改成 40（25Hz） | 零重烧验证"数据量是否是链路不稳的诱因" |
| `jl_verify.jlink` | 烧录后验证运行状态 | 分频是否为预期值、帧率、有无 HardFault |
| `jl_flash.jlink` | 备份全片 128KB → 烧写 hex → 复位运行 | 备份落在 `%TEMP%\mouse_fw_backup.bin`，可回滚 |

## 符号地址会变

脚本里的变量地址（如 `linkFastDiv = 0x20000836`）**每次重新编译后都要重新取**：

```bash
"C:/Keil_v5/ARM/ARMCC/Bin/fromelf.exe" --text -s USER/Object/Mouse.axf | grep -i linkFastDiv
```

## 两个决定性实验（见 host/ 下的 Python 封装）

1. **绕过固件写 `USART1->DR`** → `host/jl_tx_loopback_test.py`
   收到 = 物理层好，问题在固件；收不到 = 物理层不通，与固件无关
2. **从 PC 发数据，读设备接收缓冲** → `host/jl_wire_test.py`
   配合上一项，可判定是"双向通"还是"单向接线问题"

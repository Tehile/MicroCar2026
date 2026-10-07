#!/bin/bash
# ============================================================================
#  trylink.sh —— 不开 Keil, 直接在命令行"试链接", 测量 ROM/RAM 占用
#
#  用途: 改完代码后快速确认有没有超出存储范围, 以及各优化级别的余量。
#        比"开 Keil → 编译 → 看报错 → 改 → 再编译"快得多。
#
#  用法:
#      cd code_stm32f1_mouse
#      bash trylink.sh
#
#  原理: 用 armcc/armasm/armlink 以与 Keil 工程等价的参数编译全部源文件,
#        并用自定义 scatter 复刻工程的存储布局(IROM 0x08000000 限 88KB /
#        IRAM 0x20000000 限 20KB), 然后分别试链接三种配置。
#        已验证: "无 split" 配置能逐字节复现 Keil 的 L6220E 报错(91556B),
#        说明本脚本的配置与 Keil 一致。
#
#  依赖: Keil MDK 的 ARMCC V5 (C:\Keil_v5\ARM\ARMCC\Bin)
# ============================================================================
set -u
K="C:/Keil_v5/ARM/ARMCC/Bin"
AC="$K/armcc.exe"; AS="$K/armasm.exe"; LN="$K/armlink.exe"
DV="C:/Keil_v5/ARM/Pack/Keil/STM32F1xx_DFP/2.4.1/Device/Include"
CM="C:/Keil_v5/ARM/Pack/ARM/CMSIS/4.5.0/CMSIS/Include"
INC="-IAPP -IHARDWARE -IUSER -ISTRATEGY -IMOTION -I$DV -I$CM"
DEF="-D__MICROLIB -DSTM32F10X_MD -DUSE_STDPERIPH_DRIVE"

# 与 uvprojx 的 OnChipMemories 一致: IROM1 0x16000 (88KB, pages 0-87), IRAM1 0x5000 (20KB)
IROM_LIMIT=90112
IRAM_LIMIT=20480

for t in "$AC" "$AS" "$LN"; do
  [ -f "$t" ] || { echo "找不到 $t"; exit 1; }
done

cat > /tmp/scatter.sct <<'EOF'
LR_IROM1 0x08000000 0x00016000 {
  ER_IROM1 0x08000000 0x00016000 {
    *.o (RESET, +First)
    *(InRoot$$Sections)
    .ANY (+RO)
  }
  RW_IRAM1 0x20000000 0x00005000 {
    .ANY (+RW +ZI)
  }
}
EOF

# $1=额外编译选项 $2=标签 $3=优化级别
build () {
  local SPLIT="$1" TAG="$2" OPT="${3:--O0}"
  local OD="/tmp/objs_$TAG"
  local FAIL=0
  rm -rf "$OD"; mkdir -p "$OD"
  for f in APP/*.c HARDWARE/*.c MOTION/*.c STRATEGY/*.c USER/main.c; do
    local O="$OD/$(basename "$f" .c).o"
    "$AC" --cpu Cortex-M3 --thumb -c $OPT $SPLIT $DEF $INC "$f" -o "$O" > "$OD/log_$(basename "$f" .c).txt" 2>&1
    [ -f "$O" ] || { FAIL=1; echo "  [$TAG] 编译失败 $f:"; head -3 "$OD/log_$(basename "$f" .c).txt" | sed 's/^/    /'; }
  done
  # 注意: 必须给汇编器显式定义 __MICROLIB, 否则启动文件会走非 MicroLIB 分支,
  #       链接时缺 __initial_sp / 多要 __use_two_region_memory
  "$AS" --cpu Cortex-M3 --thumb --pd "__MICROLIB SETA 1" USER/STM32F10x.s -o "$OD/STM32F10x.o" > "$OD/log_asm.txt" 2>&1
  [ -f "$OD/STM32F10x.o" ] || { FAIL=1; echo "  [$TAG] 汇编失败:"; head -3 "$OD/log_asm.txt" | sed 's/^/    /'; }
  [ $FAIL -eq 1 ] && return

  "$LN" --cpu Cortex-M3 --library_type=microlib --scatter /tmp/scatter.sct \
        --entry Reset_Handler --info sizes --info totals \
        $(ls "$OD"/*.o) -o "$OD/out.axf" > "$OD/link.txt" 2>&1

  # 注意: armlink 输出的是 "<字节数> (  <kB>kB)", 没有 "bytes" 字样
  local ROM RW MROM MRAM
  ROM=$(grep -E "Total ROM Size" "$OD/link.txt" | grep -oE "[0-9]+ \(" | grep -oE "[0-9]+" | head -1)
  RW=$(grep -E "Total RW  Size" "$OD/link.txt" | grep -oE "[0-9]+ \(" | grep -oE "[0-9]+" | head -1)

  if [ -z "${ROM:-}" ]; then
    # 链接失败: 从 L6220E 报文里取"实际需要多少"; 此时取不到 RAM 数据
    ROM=$(grep -oE "Load region LR_IROM1 size \( *[0-9]+" "$OD/link.txt" | grep -oE "[0-9]+$" | head -1)
    [ -z "${ROM:-}" ] && { echo "  [$TAG] 链接失败(无法取到体积):"; grep -E "Error" "$OD/link.txt" | head -3 | sed 's/^/    /'; return; }
    MROM=$((IROM_LIMIT - ROM))
    printf "  [%-8s] ROM %6d / %d  %-10s  RAM      — / %d  (链接失败, 无 RAM 数据)\n" \
        "$TAG" "$ROM" "$IROM_LIMIT" "$([ $MROM -ge 0 ] && echo "余 $MROM" || echo "★超 $((0-MROM))★")" "$IRAM_LIMIT"
    return
  fi

  MROM=$((IROM_LIMIT - ROM))
  MRAM=$((IRAM_LIMIT - RW))
  printf "  [%-8s] ROM %6d / %d  %-10s  RAM %6d / %d  %s\n" \
      "$TAG" "$ROM" "$IROM_LIMIT" "$([ $MROM -ge 0 ] && echo "余 $MROM" || echo "★超 $((0-MROM))★")" \
      "$RW" "$IRAM_LIMIT" "$([ $MRAM -ge 0 ] && echo "余 $MRAM" || echo "★超 $((0-MRAM))★")"
}

echo "=============== 电脑鼠固件 试链接 ==============="
echo "A) 等价于当前 uvprojx 原配置 (Optim=Level0, OneElfS=0)"
build "" "nosplit"
echo "B) Optim=Level0 + OneElfS=1  <-- 现行配置(uvprojx 已设 OneElfS=1)"
build "--split_sections" "split"
echo "C) Optim=Level1 + OneElfS=1  (仅作余量参考; 会改变已整定代码的生成结果)"
build "--split_sections" "o1split" "-O1"
echo "================================================"
echo "说明: OneElfS = Keil 的 \"One ELF Section per Function\", 只做死代码消除,"
echo "      不改动任何被保留函数的代码, 是零语义风险的省空间手段。"

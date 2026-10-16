#ifndef _MAIN_H_
#define _MAIN_H_

/*
*********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*       ------------------------------------------------------------------------------------------------
*               Function: main.h —— 远程档平台垫片(比赛固件远程化移植 P1, 新增文件)
*       ------------------------------------------------------------------------------------------------
*               存在原因: link.c/link.h 是**从旧工程原样移植**的协议层, 其开头固定
*               `#include "main.h"`(旧工程的总头)。新比赛固件没有 main.h, 也不想为了
*               迁就协议层去改 link.c 的 include 行 —— 于是在 REMOTE/ 下放这个垫片,
*               依靠 Target_Remote 的 IncludePath(..\REMOTE)让 link.c 的 include 落到这里。
*
*               内容 = 新固件各模块头文件 + link_port.h(镜像/钩子) + 少量旧 main.h 才有的声明。
*               ※ 本文件只被 Target_Remote 编译(IncludePath 是 Target_Remote 独有的),
*                  比赛档 Target 1 看不到它, 故此处任何声明都不会影响比赛档产物。
*               ※ 编码: GBK + CRLF(与 REMOTE/ 其余文件及被适配的新固件一致)。
*********************************************************************************************************
*/

#include <stm32f10x.h>
/* 旧工程 main.h 原有(link.c 的 strcmp/printf/math 需要) */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "sys.h"
#include "timer.h"
#include "stratagy.h"
#include "utility.h"
#include "user.h"
#include "adc.h"
#include "stmflash.h"
#include "led.h"
#include "usart.h"
#include "link_port.h"

/* Sys_Soft_Reset 等旧 main.h 才有的声明见 link_port.h(经 main.h -> link_port.h 传递) */

#endif /* _MAIN_H_ */

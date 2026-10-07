/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: lesson_lab.c —— 课程调参实验服务(实现)
*       ------------------------------------------------------------------------------------------------
*               上游: 上位机 host/host/api/lesson.py (门面 mc.lesson)
*               下游: speedSt(速度目标) / lessonStr(课程参数) / odomStr / gyroStr / irDist
*
*               设计要点:
*                 - 本文件整体位于 #ifdef MOUSE_LESSON_LAB 之内, 正式固件不编译;
*                 - 电机六增益的**默认值直接取自 motor.h 的红线常量**, 只是变得可改 —— 数值未被改动;
*                 - 所有实验都带**时间护栏**: 不管剖面写错还是上位机失联, 到时必停;
*                 - 结束时统一走 ll_Finish(): 目标速度归零 + freemode=1 + 回一行 LESSON DONE。
*********************************************************************************************************
*/

#include "lesson_lab.h"

#ifdef MOUSE_LESSON_LAB

/*---------------------------------------  V A R I A B L E S  ---------------------------------------*/
LessonGain lessonGain;      /* 课程可调增益(初值见 LESSONLAB_Init) */
LessonLabState lessonLab;   /* 实验引擎状态 */

/** @brief 传感器方案原始值(0..3)。lessonStr 里只有两个开关位, 这里保留原始"方案号" */
static u8 llSensorMode = LL_SENSOR_NONE;

/*---------------------------------------  小工具  ---------------------------------------*/

/** @brief float -> Q16.16 */
static s32 ll_F2Q(float f)
{
    return (s32)(f * 65536.0);
}

/** @brief Q16.16 -> float */
static float ll_Q2F(s32 q)
{
    return (float)((double)q / 65536.0);
}

/** @brief 左轮累计脉冲(含回绕补偿) */
static s32 ll_DistL(void)
{
    return (s32)(odomStr.distL + odomStr.distLCycle * ENCODER_ARR_VALUE);
}

/** @brief 右轮累计脉冲(含回绕补偿) */
static s32 ll_DistR(void)
{
    return (s32)(odomStr.distR + odomStr.distRCycle * ENCODER_ARR_VALUE);
}

/** @brief 取绝对值(wheel 目标速度需要, 避免引入 math.h 的 fabs 到 ISR 路径) */
static s32 ll_Abs(s32 v)
{
    return v < 0 ? -v : v;
}

/** @brief 追加字符串, 返回新的写指针(C90 下不用 snprintf, 也不引入 %f 软浮点格式化) */
static char *ll_PutStr(char *p, const char *s)
{
    while (*s)
        *p++ = *s++;
    return p;
}

/** @brief 追加十进制整数 */
static char *ll_PutInt(char *p, s32 v)
{
    char tmp[12];
    u8 n = 0;

    if (v < 0)
    {
        *p++ = '-';
        v = -v;
    }
    if (v == 0)
        tmp[n++] = '0';
    while (v > 0)
    {
        tmp[n++] = (char)('0' + (v % 10));
        v /= 10;
    }
    while (n > 0)
        *p++ = tmp[--n];
    return p;
}

/** @brief 空格 + 整数 */
static char *ll_PutSP(char *p, s32 v)
{
    *p++ = ' ';
    return ll_PutInt(p, v);
}

/*---------------------------------------  初始化  ---------------------------------------*/

/**
 * @brief 初始化课程调参服务
 * @note  增益初值 = motor.h 的红线常量。**这不是复制一份数值, 而是引用同一个宏**:
 *        若将来赛场整定值改了, 这里自动跟着变, 不会出现"两处数值不一致"。
 */
void LESSONLAB_Init(void)
{
    lessonGain.kpL = MOTOR_KP_L;
    lessonGain.kiL = MOTOR_KI_L;
    lessonGain.kdL = MOTOR_KD_L;
    lessonGain.kpR = MOTOR_KP_R;
    lessonGain.kiR = MOTOR_KI_R;
    lessonGain.kdR = MOTOR_KD_R;

    lessonLab.exp = LL_EXP_NONE;
    lessonLab.phase = LL_PH_IDLE;
    lessonLab.elapsed = 0;
    lessonLab.limit = 0;
    lessonLab.running = 0;
    lessonLab.doneFlag = 0;
    lessonLab.reason = LL_REASON_DONE;

    /* 课程参数默认值: 与 APP/lesson.c 里各实验写死的那组数保持一致,
     * 便于"用上位机复现课件结果"。 */
    lessonStr.irLeftP = 0.1f;
    lessonStr.irRightP = 0.1f;
    lessonStr.gyroP = 2.5f;
    lessonStr.gyroD = 1.6f;
    lessonStr.speedNeed = 50;
    lessonStr.allDis = 1000;
    lessonStr.irSwitch = 0;
    lessonStr.gyroSwitch = 0;
    lessonStr.wallMode = 1;
    lessonStr.isTest = 0;
    llSensorMode = LL_SENSOR_NONE;
}

/*---------------------------------------  参数读写  ---------------------------------------*/

u8 LESSONLAB_ParamIdIs(u16 id)
{
    return (id >= LL_P_KPL && id <= LL_P_WALLMODE) ? 1 : 0;
}

u8 LESSONLAB_ParamGet(u16 id, s32 *q)
{
    switch (id)
    {
    case LL_P_KPL: *q = ll_F2Q(lessonGain.kpL); return 1;
    case LL_P_KIL: *q = ll_F2Q(lessonGain.kiL); return 1;
    case LL_P_KDL: *q = ll_F2Q(lessonGain.kdL); return 1;
    case LL_P_KPR: *q = ll_F2Q(lessonGain.kpR); return 1;
    case LL_P_KIR: *q = ll_F2Q(lessonGain.kiR); return 1;
    case LL_P_KDR: *q = ll_F2Q(lessonGain.kdR); return 1;
    case LL_P_IRPL: *q = ll_F2Q(lessonStr.irLeftP); return 1;
    case LL_P_IRPR: *q = ll_F2Q(lessonStr.irRightP); return 1;
    case LL_P_GYROP: *q = ll_F2Q(lessonStr.gyroP); return 1;
    case LL_P_GYROD: *q = ll_F2Q(lessonStr.gyroD); return 1;
    case LL_P_SPEEDNEED: *q = (s32)lessonStr.speedNeed << 16; return 1;
    case LL_P_ALLDIS: *q = (s32)lessonStr.allDis << 16; return 1;
    case LL_P_SENSORMODE: *q = (s32)llSensorMode << 16; return 1;
    case LL_P_WALLMODE: *q = (s32)lessonStr.wallMode << 16; return 1;
    default: return 0;
    }
}

/**
 * @brief 写一个课程调参量
 * @note  传感器方案(id 20)是**派生值**: 写它同时改 irSwitch / gyroSwitch 两个开关位,
 *        这样固件内部的控制律仍然只看开关位, 不必到处判断"方案号"。
 */
u8 LESSONLAB_ParamSet(u16 id, s32 q)
{
    float f = ll_Q2F(q);
    s32 i = q >> 16; /* 整数型参数用高 16 位取整, 与链路层一致的约定 */

    switch (id)
    {
    case LL_P_KPL: lessonGain.kpL = f; return 1;
    case LL_P_KIL: lessonGain.kiL = f; return 1;
    case LL_P_KDL: lessonGain.kdL = f; return 1;
    case LL_P_KPR: lessonGain.kpR = f; return 1;
    case LL_P_KIR: lessonGain.kiR = f; return 1;
    case LL_P_KDR: lessonGain.kdR = f; return 1;
    case LL_P_IRPL: lessonStr.irLeftP = f; return 1;
    case LL_P_IRPR: lessonStr.irRightP = f; return 1;
    case LL_P_GYROP: lessonStr.gyroP = f; return 1;
    case LL_P_GYROD: lessonStr.gyroD = f; return 1;
    case LL_P_SPEEDNEED:
        if (i < 0 || i > LINK_RUN_SPEED_MAX) return 0;
        lessonStr.speedNeed = (int)i;
        return 1;
    case LL_P_ALLDIS:
        if (i < 0 || i > 30000) return 0;
        lessonStr.allDis = (int)i;
        return 1;
    case LL_P_SENSORMODE:
        if (i < LL_SENSOR_NONE || i > LL_SENSOR_BOTH) return 0;
        llSensorMode = (u8)i;
        lessonStr.irSwitch = (i == LL_SENSOR_IR || i == LL_SENSOR_BOTH) ? 1 : 0;
        lessonStr.gyroSwitch = (i == LL_SENSOR_GYRO || i == LL_SENSOR_BOTH) ? 1 : 0;
        return 1;
    case LL_P_WALLMODE:
        if (i != 1 && i != 2) return 0;
        lessonStr.wallMode = (int)i;
        return 1;
    default:
        return 0;
    }
}

/*---------------------------------------  实验引擎  ---------------------------------------*/

u8 LESSONLAB_Running(void)
{
    return lessonLab.running;
}

u16 LESSONLAB_Exp(void)
{
    return lessonLab.exp;
}

/**
 * @brief 实验收尾: 停车 + 记录结果 + 置 DONE 待报标志
 * @param reason LL_REASON_*
 */
static void ll_Finish(const char *reason)
{
    lessonLab.dL = ll_DistL() - lessonLab.vStartL;
    lessonLab.dR = ll_DistR() - lessonLab.vStartR;
    lessonLab.angFinal = gyroStr.angle;

    speedSt.speedTargetL = 0;
    speedSt.speedTargetR = 0;
    speedSt.freemode = 1; /* 自由模式: 电机输出强制为 0 */
    lessonStr.isTest = 0;

    lessonLab.running = 0;
    lessonLab.phase = LL_PH_IDLE;
    lessonLab.reason = reason;
    lessonLab.doneFlag = 1;
}

/**
 * @brief 启动一个实验
 * @param exp  实验号
 * @param a,b,c,d 参数(随实验而定):
 *        实验三 ENCODER : a=速度cm/s  b=目标距离mm  c=时间上限ms
 *        实验四 PID     : a=巡航速度   b=加速段ms    c=匀速段ms   d=减速段ms
 *        实验六 GYRO    : a=目标角度度 b=旋转速度    c=时间上限ms
 *        实验七 MULTI   : a=速度cm/s  b=目标距离mm  c=时间上限ms
 * @return LL_OK / LL_ERR_*
 */
u8 LESSONLAB_Start(u16 exp, s32 a, s32 b, s32 c, s32 d)
{
    if (lessonLab.running)
        return LL_ERR_BUSY;

    /* 起点快照: 位移以"启动那一刻"为基准, 与 MOVE RUN 的做法一致 */
    lessonLab.vStartL = ll_DistL();
    lessonLab.vStartR = ll_DistR();
    lessonLab.elapsed = 0;
    lessonLab.phase = LL_PH_IDLE;
    lessonLab.dL = 0;
    lessonLab.dR = 0;
    lessonLab.vPeakL = 0;
    lessonLab.vPeakR = 0;
    lessonLab.doneFlag = 0;
    lessonLab.reason = LL_REASON_DONE;
    lessonLab.distTarget = b;
    lessonLab.angleTarget = a;

    switch (exp)
    {
    case LL_EXP_ENCODER: /* 实验三: 直行到"自认为"走完 b 毫米 */
        if (a <= 0 || a > LINK_RUN_SPEED_MAX || b <= 0 || c <= 0)
            return LL_ERR_VALUE;
        lessonLab.vCruise = a;
        lessonLab.limit = (u16)(c > LINK_RUN_MS_MAX ? LINK_RUN_MS_MAX : c);
        break;

    case LL_EXP_PID: /* 实验四: 加速-匀速-减速-停车 */
        if (a <= 0 || a > LINK_RUN_SPEED_MAX || b < 0 || c < 0 || d < 0)
            return LL_ERR_VALUE;
        if (b == 0 || d == 0) /* 加速/减速段必须非零, 否则不是阶跃实验 */
            return LL_ERR_VALUE;
        lessonLab.vCruise = a;
        lessonLab.tAcc = (u16)b;
        lessonLab.tCruise = (u16)c;
        lessonLab.tDec = (u16)d;
        lessonLab.limit = (u16)(b + c + d + 500); /* 剖面 + 0.5s 余量 */
        break;

    case LL_EXP_GYRO: /* 实验六: 原地转到目标角度 */
        if (a == 0 || a > 3600 || a < -3600 || b <= 0 || b > LINK_RUN_SPEED_MAX || c <= 0)
            return LL_ERR_VALUE;
        lessonLab.vCruise = b;
        lessonLab.limit = (u16)(c > LINK_RUN_MS_MAX ? LINK_RUN_MS_MAX : c);
        MOTION_SetAngle(0); /* 姿态清零: 角度以启动时刻为基准 */
        break;

    case LL_EXP_MULTI: /* 实验七: 多传感器直行到目标距离 */
        if (a <= 0 || a > LINK_RUN_SPEED_MAX || b <= 0 || c <= 0)
            return LL_ERR_VALUE;
        lessonLab.vCruise = a;
        lessonLab.limit = (u16)(c > LINK_RUN_MS_MAX ? LINK_RUN_MS_MAX : c);
        lessonStr.blockCount = 0;
        lessonStr.irErrPrev = 0;
        lessonStr.gyroErrPrev = 0;
        MOTION_SetAngle(0);
        break;

    case LL_EXP_IR:  /* 实验五: 用既有 CAL IRDATA 四步标定, 不走本引擎 */
    case LL_EXP_TURN: /* 实验八: 转向搜索数组, 预留 */
        return LL_ERR_EXP;

    default:
        return LL_ERR_EXP;
    }

    lessonLab.exp = exp;
    lessonLab.running = 1;
    lessonStr.isTest = 1;
    speedSt.freemode = 0; /* 解除自由模式, 电机可控 */
    speedSt.speedTargetL = 0;
    speedSt.speedTargetR = 0;
    return LL_OK;
}

/**
 * @brief 外部(链路层: STOP/ESTOP)要求中止实验
 * @param reason LL_REASON_STOP / LL_REASON_ESTOP
 */
void LESSONLAB_Abort(const char *reason)
{
    if (lessonLab.running)
        ll_Finish(reason);
}

u8 LESSONLAB_Stop(void)
{
    LESSONLAB_Abort(LL_REASON_STOP);
    return LL_OK;
}

/**
 * @brief 实验四/七 的直行控制律(复刻 APP/lesson.c 的 LESSON_ISR 写法)
 * @note  与 lesson.c 共用同一组 lessonStr 参数, 因此"上位机调出来的参数"
 *        可以直接对照课件里那个版本的效果。
 */
static void ll_ApplyCorrection(s32 *spdL, s32 *spdR)
{
    if (lessonStr.irSwitch)
    {
        if (lessonStr.wallMode == 1)
        {
            lessonStr.irErrPrev = lessonStr.irErr;
            lessonStr.irErr = irDist.l - sysCfg.distToWall;
        }
        else if (lessonStr.wallMode == 2)
        {
            lessonStr.irErrPrev = lessonStr.irErr;
            lessonStr.irErr = sysCfg.distToWall - irDist.r;
        }
        *spdL -= (s32)(lessonStr.irErr * lessonStr.irLeftP);
        *spdR += (s32)(lessonStr.irErr * lessonStr.irRightP);
    }

    if (lessonStr.gyroSwitch)
    {
        s32 d;
        lessonStr.gyroErrPrev = lessonStr.gyroErr;
        lessonStr.gyroErr = gyroStr.angle;
        d = (s32)(lessonStr.gyroErr * lessonStr.gyroP +
                  (lessonStr.gyroErr - lessonStr.gyroErrPrev) * lessonStr.gyroD);
        *spdL -= d;
        *spdR += d;
    }
}

/**
 * @brief 1ms 引擎(由 LINK_Tick 调用)
 */
void LESSONLAB_Tick(void)
{
    s32 spdL, spdR;

    if (!lessonLab.running)
        return;

    if (speedSt.sleepmode)
    {
        ll_Finish(LL_REASON_STOP);
        return;
    }

    lessonLab.elapsed++;

    /* 速度峰值统计(所有实验通用): "电机真的转了"的证据 */
    if (ll_Abs(speedSt.speedL) > ll_Abs(lessonLab.vPeakL))
        lessonLab.vPeakL = speedSt.speedL;
    if (ll_Abs(speedSt.speedR) > ll_Abs(lessonLab.vPeakR))
        lessonLab.vPeakR = speedSt.speedR;

    switch (lessonLab.exp)
    {
    /* ---------------- 实验四: 匀变速直线(加速-匀速-减速-停车) ---------------- */
    case LL_EXP_PID:
    {
        u16 e = lessonLab.elapsed;
        u16 t1 = lessonLab.tAcc;
        u16 t2 = (u16)(lessonLab.tAcc + lessonLab.tCruise);

        if (e <= t1)
        {
            lessonLab.phase = LL_PH_ACC;
            spdL = spdR = (s32)((s32)lessonLab.vCruise * e / t1);
        }
        else if (e <= t2)
        {
            lessonLab.phase = LL_PH_CRUISE;
            spdL = spdR = lessonLab.vCruise;
        }
        else if (e <= (u16)(t2 + lessonLab.tDec))
        {
            u16 te = (u16)(e - t2);
            lessonLab.phase = LL_PH_DEC;
            spdL = spdR = (s32)((s32)lessonLab.vCruise * (s32)(lessonLab.tDec - te) / (s32)lessonLab.tDec);
        }
        else
        {
            ll_Finish(LL_REASON_DONE);
            return;
        }
        speedSt.speedTargetL = spdL;
        speedSt.speedTargetR = spdR;
        break;
    }

    /* ---------------- 实验三: 编码器测距(直行到自认为的 b 毫米) ---------------- */
    case LL_EXP_ENCODER:
    {
        float dL = odomStr.trackAbsL - (float)lessonLab.vStartL / sysCfg.trackRatioL;
        float dR = odomStr.trackAbsR - (float)lessonLab.vStartR / sysCfg.trackRatioR;

        /* 用"固件自己算出来的里程"当判据 —— 这样测出的正是**当前系数下的测距误差** */
        if (dL < 0) dL = -dL;
        if (dR < 0) dR = -dR;
        if (dL >= (float)lessonLab.distTarget || dR >= (float)lessonLab.distTarget)
        {
            ll_Finish(LL_REASON_DIST);
            return;
        }
        speedSt.speedTargetL = lessonLab.vCruise;
        speedSt.speedTargetR = lessonLab.vCruise;
        break;
    }

    /* ---------------- 实验六: 陀螺标定(原地转到目标角度) ---------------- */
    case LL_EXP_GYRO:
    {
        s32 tgt = lessonLab.angleTarget;
        s32 cur = gyroStr.angle;

        lessonLab.phase = LL_PH_CRUISE;
        if (ll_Abs(cur) >= ll_Abs(tgt))
        {
            ll_Finish(LL_REASON_ANGLE);
            return;
        }
        /* 左转为负、右转为正: 目标为正(右转)时左轮前进、右轮后退 */
        if (tgt > 0)
        {
            speedSt.speedTargetL = lessonLab.vCruise;
            speedSt.speedTargetR = -lessonLab.vCruise;
        }
        else
        {
            speedSt.speedTargetL = -lessonLab.vCruise;
            speedSt.speedTargetR = lessonLab.vCruise;
        }
        break;
    }

    /* ---------------- 实验七: 多传感器校准直行 ---------------- */
    case LL_EXP_MULTI:
    {
        float dL, dR;

        if (irDist.f1 < 30 || irDist.f2 < 30)
        {
            lessonStr.blockCount++;
            if (lessonStr.blockCount >= 3)
            {
                ll_Finish(LL_REASON_IR);
                return;
            }
        }
        else
            lessonStr.blockCount = 0;

        dL = odomStr.trackAbsL - (float)lessonLab.vStartL / sysCfg.trackRatioL;
        dR = odomStr.trackAbsR - (float)lessonLab.vStartR / sysCfg.trackRatioR;
        if (dL < 0) dL = -dL;
        if (dR < 0) dR = -dR;
        if (dL >= (float)lessonLab.distTarget || dR >= (float)lessonLab.distTarget)
        {
            ll_Finish(LL_REASON_DIST);
            return;
        }

        spdL = spdR = lessonLab.vCruise;
        ll_ApplyCorrection(&spdL, &spdR);
        speedSt.speedTargetL = spdL;
        speedSt.speedTargetR = spdR;
        lessonLab.phase = LL_PH_CRUISE;
        break;
    }

    default:
        ll_Finish(LL_REASON_DONE);
        return;
    }

    /* 时间护栏: 与 MOVE RUN 一样, 放在最后统一判定 —— 任何实验都跑不出这个上限 */
    if (lessonLab.elapsed >= lessonLab.limit)
        ll_Finish(LL_REASON_TIME);
}

/**
 * @brief 取走一条待上报的 DONE 行
 * @note  格式: LESSON DONE <exp> <REASON> <ms> <dL> <dR> <vpkL> <vpkR> <angle>
 *        全部为整数, 不含浮点格式化 —— 既避开 C90 的 snprintf, 也避开 ISR 里的软浮点。
 */
u8 LESSONLAB_TakeDone(char *buf, u16 cap)
{
    char *p = buf;
    char *end = buf + cap - 2;

    if (!lessonLab.doneFlag)
        return 0;

    p = ll_PutStr(p, "LESSON DONE");
    p = ll_PutSP(p, (s32)lessonLab.exp);
    p = ll_PutStr(p, " ");
    p = ll_PutStr(p, lessonLab.reason);
    p = ll_PutSP(p, (s32)lessonLab.elapsed);
    p = ll_PutSP(p, lessonLab.dL);
    p = ll_PutSP(p, lessonLab.dR);
    p = ll_PutSP(p, lessonLab.vPeakL);
    p = ll_PutSP(p, lessonLab.vPeakR);
    p = ll_PutSP(p, (s32)lessonLab.angFinal);

    if (p > end)
        p = end; /* 兜底: 缓冲不够时只截断, 不越界 */
    *p = '\0';

    lessonLab.doneFlag = 0;
    return 1;
}

#endif /* MOUSE_LESSON_LAB */

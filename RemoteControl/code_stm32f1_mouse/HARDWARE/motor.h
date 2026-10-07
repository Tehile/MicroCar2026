/**
 * @file    motor.h
 * @brief   电机模块: PWM/编码器初始化 + 速度PID + 闭环控制
 * @author  赛曙开发组
 * @date    2026-08-01
 * @note    对标标杆 HARDWARE/motor.h, 集中管理电机全部功能:
 *            - 硬件初始化: PWM(TIM4) / 左右轮编码器(TIM2/TIM3)
 *            - 速度PID: PidStruct + MOTOR_Pid* 系列(原 pid.c/h)
 *            - 闭环控制: 编码器采样 -> PID -> PWM输出(原 SysTick_Handler 内联)
 *          红线#6: PID增益数值不可改; 红线#13: 编码器溢出补偿逻辑不可改。
 */
#ifndef _MOTOR_H_
#define _MOTOR_H_
#include "main.h"
/*-----------------------------------------  I N C L U D E S  -----------------------------------------*/
#include "sys.h"

/*---------------------------------------  D E F I N I T I O N  ---------------------------------------*/
/* ---- PWM 通道(TIM4, PB6-9) ---- */
#define MOTOR_LPWMH TIM4->CCR1 /* 左轮前进 PB6 */
#define MOTOR_LPWML TIM4->CCR2 /* 左轮后退 PB7 */
#define MOTOR_RPWML TIM4->CCR3 /* 右轮后退 PB8 */
#define MOTOR_RPWMH TIM4->CCR4 /* 右轮前进 PB9 */

/* ---- 编码器计数值(TIM2左轮/TIM3右轮) ---- */
#define MOTOR_ENCL_VALUE TIM2->CNT
#define MOTOR_ENCR_VALUE TIM3->CNT

/* ---- PWM 限幅(红线: 数值不可改) ---- */
#define MOTOR_PWM_MAX 1500  /* TIM4 ARR 周期值 */
#define MOTOR_PWM_LIMIT 350 /* 安全限幅, 禁止超过 */

/* ---- 速度环PID增益(红线#6: 赛场整定值, freemode=0 时每周期设置) ----
 * v2.5: 远程档改读链路参数表宿主变量(初值==原常量, LINK_Init 按参数表覆写,
 *       SETQ 可在线调); 比赛档保持编译期常量(产物逐字节不变)。 */
#ifdef MOUSE_REMOTE
extern double linkMtrKpL, linkMtrKiL, linkMtrKdL; /* 定义于 link.c, 见 link.h id 50-52 */
extern double linkMtrKpR, linkMtrKiR, linkMtrKdR; /* id 53-55 */
#define MOTOR_KP_L (linkMtrKpL)
#define MOTOR_KI_L (linkMtrKiL)
#define MOTOR_KD_L (linkMtrKdL)
#define MOTOR_KP_R (linkMtrKpR)
#define MOTOR_KI_R (linkMtrKiR)
#define MOTOR_KD_R (linkMtrKdR)
#else
#define MOTOR_KP_L 10.75       /* 左轮比例(太低对应60+搜索速度会抽风) */
#define MOTOR_KI_L 0.55        /* 左轮积分 */
#define MOTOR_KD_L 0.55        /* 左轮微分 */
#define MOTOR_KP_R 10.20       /* 右轮比例 */
#define MOTOR_KI_R 0.5         /* 右轮积分 */
#define MOTOR_KD_R 0.5         /* 右轮微分 */
#endif
#define MOTOR_INTEG_LIMIT 1200 /* 积分限幅(anti-windup) */

/* ---- PWM 输出死区(红线: 左右不对称为赛场现状, 不可擅自统一) ---- */
#define MOTOR_DEADZONE_L 1  /* 左轮死区 */
#define MOTOR_DEADZONE_R 35 /* 右轮死区 */

/**
 * @brief 通用PID控制器(成员 lowerCamelCase, 符合 coding-standard)
 */
typedef struct
{
    int ref;        /* 设定值(目标速度, pulse/ms) */
    int feedBack;   /* 反馈值(编码器测速) */
    int err;        /* 当前误差 = ref - feedBack */
    int errPrev;    /* 上一次误差(微分用) */
    int errInteg;   /* 误差积分累加(带 anti-windup) */
    double kp;      /* 比例增益(double 保证红线#6 数值精度) */
    double ki;      /* 积分增益 */
    double kd;      /* 微分增益 */
    int integLimit; /* 积分限幅 */
    double out;     /* PID输出(PWM) */
} PidStruct;

/**
 * @brief 电机测速状态(编码器差分测速)
 */
typedef struct
{
    int speedL;
    int speedR; /* 测速值(pulse/ms) */
    int wheelAngleL;
    int wheelAngleR; /* 当周期编码器计数 */
    int whAngPrevL;
    int whAngPrevR; /* 上周期编码器计数 */
    double targetL;
    double targetR; /* 目标速度(pulse/ms) */
} MotorVars;

extern MotorVars motor;     /* 电机测速状态 */
extern PidStruct motorPidL; /* 左轮速度PID */
extern PidStruct motorPidR; /* 右轮速度PID */

/*-------------------------------------  F U N C T I O N S  -------------------------------------*/
/* ---- 硬件初始化 ---- */
void MOTOR_PwmInit(void);      /* 电机PWM初始化(TIM4, PB6-9) */
void MOTOR_EncLeftInit(void);  /* 左轮编码器初始化(TIM2, PA15/PB3) */
void MOTOR_EncRightInit(void); /* 右轮编码器初始化(TIM3, PB4/PB5) */

/* ---- 速度PID ---- */
void MOTOR_PidUpdateErr(PidStruct *pp); /* 误差+积分更新(不算输出) */
void MOTOR_PidCalcOut(PidStruct *pp);   /* 用已更新误差算输出 */
void MOTOR_PidControl(PidStruct *pp);   /* 误差更新+输出一次算完 */

/* ---- 闭环控制(SysTick 每1ms调用) ---- */
void MOTOR_EncSample(void);      /* 编码器采样+测速+PID误差更新 */
void MOTOR_LoopControl(void);    /* PID输出+PWM限幅+方向输出 */
void MOTOR_LeftPwm(double pwm);  /* 左轮PWM方向输出(含死区) */
void MOTOR_RightPwm(double pwm); /* 右轮PWM方向输出(含死区) */

#endif

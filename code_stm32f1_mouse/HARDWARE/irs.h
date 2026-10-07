#ifndef _IRS_H_
#define _IRS_H_
#include "main.h"
/*-----------------------------------------  I N C L U D E S  -----------------------------------------*/
#include "sys.h"
#include "adc.h"

/*---------------------------------------  D E F I N I T I O N  ---------------------------------------*/
/* 传感器编号(IR_CheckValue/IR_InfCorrect 的 Name 参数) */
#define F2 1
#define R 2
#define L 3
#define F1 4
#define NONE 5

/* 红外发射管控制(推挽输出) */
#define INFR_RF PCout(6)
#define INFR_R PBout(15)
#define INFR_L PBout(13)
#define INFR_LF PBout(14)
#define INFR_F2 INFR_RF
#define INFR_F1 INFR_LF

/* ---- 卡尔曼滤波器(28成员4组, 一维滤波: 值/协方差/临时/协方差临时/增益/Q/P) ---- */
typedef struct
{
    double f1;       /* 前左滤波值 */
    double covf1;    /* 前左协方差 */
    double f1tmp;    /* 前左预测值 */
    double covf1tmp; /* 前左预测协方差 */
    double kf1;      /* 前左卡尔曼增益 */
    double Qf1;      /* 前左过程噪声 */
    double Pf1;      /* 前左测量噪声 */
    double f2;       /* 前右滤波值 */
    double covf2;    /* 前右协方差 */
    double f2tmp;    /* 前右预测值 */
    double covf2tmp; /* 前右预测协方差 */
    double kf2;      /* 前右卡尔曼增益 */
    double Qf2;      /* 前右过程噪声 */
    double Pf2;      /* 前右测量噪声 */
    double l;        /* 左滤波值 */
    double covl;     /* 左协方差 */
    double ltmp;     /* 左预测值 */
    double covltmp;  /* 左预测协方差 */
    double kl;       /* 左卡尔曼增益 */
    double Ql;       /* 左过程噪声 */
    double Pl;       /* 左测量噪声 */
    double r;        /* 右滤波值 */
    double covr;     /* 右协方差 */
    double rtmp;     /* 右预测值 */
    double covrtmp;  /* 右预测协方差 */
    double kr;       /* 右卡尔曼增益 */
    double Qr;       /* 右过程噪声 */
    double Pr;       /* 右测量噪声 */
} KalmanState;
/* 卡尔曼变量由 IR_GetDistance(ISR调用)与主循环共享, 实例需 volatile */
extern volatile KalmanState kalmanSt;

/* ---- 红外标定状态(IR_GetDistance ISR共享 + 主循环读写) ---- */
typedef struct
{
    int rawf1, rawf2, rawl, rawr;              /* 当前ADC原始值(IR_GetDistance写, 主循环读) */
    int rawf1Prev, rawf2Prev, rawlPrev, rawrPrev; /* 历史ADC原始值(标定模式滑动平均) */
    int rawf1avrgnear, rawf2avrgnear, rawlavrgnear, rawravrgnear; /* 标定平均值-近(calibrate.c使用) */
    int rawf1avrgfar, rawf2avrgfar, rawlavrgfar, rawravrgfar;     /* 标定平均值-远(calibrate.c使用) */
    u8 kalmanswitch;  /* 卡尔曼滤波开关 */
    u8 calibMode;     /* 标定模式标志(1=滑动平均而非查表) */
} IrCalib;
/* IrCalib 含 ISR共享 + 主循环共享, 需 volatile */
extern volatile IrCalib irCalib;

/* 红外测距标定表(前左/前右/左/右, 各250点; 由 IR_InfCorrect 按标定值重算) */
extern u16 dataf1[250], dataf2[250], datal[250], datar[250];

/*-------------------------------------  F U N C T I O N S  -------------------------------------*/
void IR_Init(void);                             /* 卡尔曼初值 + 标定状态初始化 */
void IR_GetDistance(void);                      /* 采集4方向距离(ISR每周期调用) */
u16 IR_FindDis(u16 ADC, u16 *data, u16 Length); /* 二分查表: ADC值 -> mm距离 */

/* ---- 标定工具链(仅标定菜单流程使用) ---- */
void IR_IfRight(double logV1, double logV2, double logD1, double logD2);
void IR_CurveFit(double Y1, double X1, double Y2, double X2);
void IR_CheckValue(u16 num, u16 infr[], u8 Name);
/* vol1/dist1: 近点ADC与实距; vol2/dist2: 远点; num: 表长; infr: 目标表; Name: 传感器编号 */
void IR_InfCorrect(double vol1, double dist1, double vol2, double dist2, u16 num, u16 infr[], u8 Name);

#endif

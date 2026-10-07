/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: irs.c —— 红外测距(卡尔曼滤波 + 标定表查表 + 标定工具链)
*********************************************************************************************************
*/
/* 本文件在算法中位置: 感知层--红外ADC测距+卡尔曼滤波, 为墙判定与姿态矫正提供距离输入。 */

#include "irs.h"

/* ---- 红外测距标定表(4个方向 × 250点, 由 IR_InfCorrect 按标定值重算) ---- */
u16 dataf1[250] = {2990, 2770, 2576, 2404, 2250, 2112, 1987, 1874, 1772, 1679, 1593, 1515, 1443, 1376, 1315, 1257, 1204, 1155, 1109, 1065, 1025, 987, 952, 918, 886, 857, 828, 802, 776, 752, 730, 708, 688, 668, 649, 631, 614, 598, 583, 568, 553, 540, 527, 514, 502, 490, 479, 468, 458, 448, 438, 429, 420, 411, 403, 395, 387, 380, 372, 365, 358, 352, 345, 339, 333, 327, 321, 316, 310, 305, 300, 295, 290, 285, 281, 276, 272, 268, 264, 260, 256, 252, 248, 245, 241, 238, 234, 231, 228, 225, 222, 219, 216, 213, 210, 207, 205, 202, 199, 197, 195, 192, 190, 187, 185, 183, 181, 179, 177, 174, 172, 170, 169, 167, 165, 163, 161, 159, 158, 156, 154, 153, 151, 149, 148, 146, 145, 143, 142, 140, 139, 138, 136, 135, 134, 132, 131, 130, 129, 127, 126, 125, 124, 123, 122, 120, 119, 118, 117, 116, 115, 114, 113, 112, 111, 110, 109, 108, 108, 107, 106, 105, 104, 103, 102, 101, 101, 100, 99, 98, 97, 97, 96, 95, 94, 94, 93, 92, 92, 91, 90, 90, 89, 88, 88, 87, 86, 86, 85, 84, 84, 83, 83, 82, 81, 81, 80, 80, 79, 79, 78, 78, 77, 77, 76, 76, 75, 75, 74, 74, 73, 73, 72, 72, 71, 71, 70, 70, 69, 69, 69, 68, 68, 67, 67, 66, 66, 66, 65, 65, 64, 64, 64, 63, 63, 63, 62, 62, 62, 61, 61, 60, 60, 60, 59, 59, 59, 58, 58, 0};
u16 dataf2[250] = {4333, 3982, 3675, 3404, 3163, 2949, 2757, 2585, 2429, 2288, 2159, 2042, 1935, 1836, 1745, 1662, 1584, 1512, 1446, 1384, 1326, 1271, 1221, 1173, 1129, 1087, 1047, 1010, 975, 942, 910, 880, 852, 825, 800, 775, 752, 730, 709, 689, 670, 652, 634, 618, 602, 586, 571, 557, 544, 530, 518, 506, 494, 483, 472, 461, 451, 442, 432, 423, 414, 406, 398, 390, 382, 374, 367, 360, 353, 347, 340, 334, 328, 322, 317, 311, 306, 300, 295, 290, 286, 281, 276, 272, 267, 263, 259, 255, 251, 247, 244, 240, 236, 233, 230, 226, 223, 220, 217, 214, 211, 208, 205, 202, 200, 197, 194, 192, 189, 187, 184, 182, 180, 178, 175, 173, 171, 169, 167, 165, 163, 161, 159, 157, 156, 154, 152, 150, 149, 147, 145, 144, 142, 141, 139, 138, 136, 135, 133, 132, 131, 129, 128, 127, 125, 124, 123, 122, 120, 119, 118, 117, 116, 115, 114, 113, 111, 110, 109, 108, 107, 106, 105, 104, 104, 103, 102, 101, 100, 99, 98, 97, 96, 96, 95, 94, 93, 92, 92, 91, 90, 89, 89, 88, 87, 86, 86, 85, 84, 84, 83, 82, 82, 81, 81, 80, 79, 79, 78, 77, 77, 76, 76, 75, 75, 74, 74, 73, 72, 72, 71, 71, 70, 70, 69, 69, 68, 68, 67, 67, 67, 66, 66, 65, 65, 64, 64, 63, 63, 63, 62, 62, 61, 61, 61, 60, 60, 59, 59, 59, 58, 58, 57, 57, 57, 56, 56, 56, 55, 0};
u16 datal[250] = {2827, 2635, 2464, 2313, 2177, 2054, 1943, 1843, 1751, 1667, 1590, 1518, 1453, 1392, 1336, 1283, 1234, 1188, 1146, 1105, 1068, 1032, 999, 967, 937, 909, 882, 856, 832, 809, 787, 766, 746, 727, 709, 692, 675, 659, 644, 629, 615, 602, 589, 576, 564, 552, 541, 530, 520, 510, 500, 490, 481, 473, 464, 456, 448, 440, 432, 425, 418, 411, 404, 398, 392, 385, 379, 374, 368, 362, 357, 352, 347, 342, 337, 332, 328, 323, 319, 315, 310, 306, 302, 298, 295, 291, 287, 284, 280, 277, 273, 270, 267, 264, 261, 258, 255, 252, 249, 246, 244, 241, 238, 236, 233, 231, 228, 226, 224, 221, 219, 217, 215, 212, 210, 208, 206, 204, 202, 200, 198, 197, 195, 193, 191, 189, 188, 186, 184, 183, 181, 180, 178, 176, 175, 173, 172, 171, 169, 168, 166, 165, 164, 162, 161, 160, 158, 157, 156, 155, 153, 152, 151, 150, 149, 148, 147, 146, 144, 143, 142, 141, 140, 139, 138, 137, 136, 135, 134, 133, 133, 132, 131, 130, 129, 128, 127, 126, 125, 125, 124, 123, 122, 121, 121, 120, 119, 118, 118, 117, 116, 115, 115, 114, 113, 113, 112, 111, 110, 110, 109, 109, 108, 107, 107, 106, 105, 105, 104, 104, 103, 102, 102, 101, 101, 100, 100, 99, 98, 98, 97, 97, 96, 96, 95, 95, 94, 94, 93, 93, 92, 92, 91, 91, 90, 90, 89, 89, 88, 88, 88, 87, 87, 86, 86, 85, 85, 85, 84, 0};
u16 datar[250] = {2241, 2106, 1986, 1877, 1779, 1690, 1609, 1535, 1467, 1404, 1345, 1291, 1241, 1195, 1151, 1111, 1072, 1037, 1003, 971, 941, 913, 886, 861, 837, 814, 792, 771, 751, 733, 715, 697, 681, 665, 650, 635, 622, 608, 595, 583, 571, 560, 549, 538, 528, 518, 508, 499, 490, 481, 473, 465, 457, 449, 442, 434, 427, 421, 414, 408, 401, 395, 390, 384, 378, 373, 368, 362, 357, 352, 348, 343, 338, 334, 330, 325, 321, 317, 313, 309, 306, 302, 298, 295, 291, 288, 285, 281, 278, 275, 272, 269, 266, 263, 261, 258, 255, 252, 250, 247, 245, 242, 240, 238, 235, 233, 231, 228, 226, 224, 222, 220, 218, 216, 214, 212, 210, 208, 206, 205, 203, 201, 199, 198, 196, 194, 193, 191, 190, 188, 187, 185, 184, 182, 181, 179, 178, 176, 175, 174, 172, 171, 170, 169, 167, 166, 165, 164, 163, 161, 160, 159, 158, 157, 156, 155, 154, 153, 151, 150, 149, 148, 147, 146, 145, 145, 144, 143, 142, 141, 140, 139, 138, 137, 136, 136, 135, 134, 133, 132, 131, 131, 130, 129, 128, 128, 127, 126, 125, 125, 124, 123, 122, 122, 121, 120, 120, 119, 118, 118, 117, 116, 116, 115, 115, 114, 113, 113, 112, 111, 111, 110, 110, 109, 109, 108, 107, 107, 106, 106, 105, 105, 104, 104, 103, 103, 102, 102, 101, 101, 100, 100, 99, 99, 98, 98, 97, 97, 96, 96, 96, 95, 95, 94, 94, 93, 93, 92, 92, 0};

/**
 * @brief 红外查表: ADC 值 -> 距离(mm)
 * @param ADC ADC 原始值
 * @param data 标定表首地址
 * @param Length 标定表长度
 * @return 距离索引(0 ~ Length-1), 超出表范围时返回端点值
 * @note 表末元素须为 0(查表哨兵)
 */
u16 IR_FindDis(u16 ADC, u16 *data, u16 Length)
{
	int tmp = 0;
	if (ADC > data[0])
		return 0;
	if (ADC < data[Length - 2])
		return Length - 1;
	tmp = (Length - 1) / 2;
	Length /= 2;
	while (1)
	{
		if (data[tmp] > ADC)
		{
			tmp += Length / 2;
			if (Length % 2 == 1)
				Length += 1;
			Length /= 2;
		}
		else if (data[tmp] < ADC)
		{
			tmp -= Length / 2;
			if (Length % 2 == 1)
				Length += 1;
			Length /= 2;
		}
		else
			break;

		if (Length == 1)
			break;
	}
	return tmp;
}

/**
 * @brief 负值截零(红外差分采样可能为负)
 * @param t 待处理数值
 * @return 非负原值, 负数返回 0
 */
static long adcAboveZero(long t)
{
	if (t >= 0)
		return t;
	else
		return 0;
}

/* Q=系统噪声cov, P=测量噪声cov, Q不变P越大输出越稳定反应越迟钝 */
volatile KalmanState kalmanSt; /* 初值由 IR_Init() 设置(volatile 不宜静态初始化) */
/* 18个红外标定变量(10当前/历史ADC + 8标定平均); IR_GetDistance(ISR)写, 主循环读 */
volatile IrCalib irCalib; /* 初值由 IR_Init() 设置 */

/**
 * @brief 红外模块初始化: 卡尔曼滤波器初值 + 标定状态
 * @param none
 * @note 须在 ADC_Init 之后、首次调用 IR_GetDistance 之前调用;
 *       4组初值均为 值2000.0 协方差1.0 Q=0.01 P=0.2
 */
void IR_Init(void)
{
	/* 卡尔曼滤波器4组初值 */
	kalmanSt.f1 = 2000.0;
	kalmanSt.covf1 = 1.0;
	kalmanSt.Qf1 = 0.01;
	kalmanSt.Pf1 = 0.2;
	kalmanSt.f2 = 2000.0;
	kalmanSt.covf2 = 1.0;
	kalmanSt.Qf2 = 0.01;
	kalmanSt.Pf2 = 0.2;
	kalmanSt.l = 2000.0;
	kalmanSt.covl = 1.0;
	kalmanSt.Ql = 0.01;
	kalmanSt.Pl = 0.2;
	kalmanSt.r = 2000.0;
	kalmanSt.covr = 1.0;
	kalmanSt.Qr = 0.01;
	kalmanSt.Pr = 0.2;

	/* 标定状态: 卡尔曼默认启用, 非标定模式 */
	irCalib.kalmanswitch = 1;
	irCalib.calibMode = 0;
}
/**
 * @brief 采集4方向红外距离(前左/前右/左/右, 卡尔曼滤波 + 标定表查表)
 * @param none
 * @note ISR每周期调用; ADC采样时序(DELAY_Us 5/60/55 + INFR管脚开关)为硬件时序依赖不可调整;
 *       标定模式(calibMode=1)做滑动平均而非查表; 查表测距约7us/张, 6张表共约65us
 */
void IR_GetDistance(void)
{
	u16 adcL, adcR; /* 本函数局部临时量 */

	DELAY_Us(5);
	adcL = ADC_Get(ADC_LF);
	adcR = ADC_Get(ADC_RF);
	INFR_LF = 1;
	INFR_RF = 1;

	DELAY_Us(60);
	adcL = adcAboveZero(ADC_Get(ADC_LF) - adcL);
	adcR = adcAboveZero(ADC_Get(ADC_RF) - adcR);
	INFR_LF = 0;
	INFR_RF = 0;

	/* 卡尔曼滤波(一维): 预测 covXtmp=covX+Q; 增益 kX=covXtmp/(covXtmp+P);
	   更新 fX=fXtmp+kX*(测量-fXtmp); covX=(1-kX)*covXtmp。
	   Q=过程噪声协方差(越小越信任预测), P=测量噪声协方差(越大输出越平滑但迟钝)。
	   参数为调参结果。kalmanswitch=1 启用。 */
	if (irCalib.kalmanswitch == 1)
	{
		kalmanSt.f1tmp = kalmanSt.f1;
		kalmanSt.covf1tmp = kalmanSt.covf1 + kalmanSt.Qf1;
		kalmanSt.kf1 = kalmanSt.covf1tmp / (kalmanSt.covf1tmp + kalmanSt.Pf1);
		kalmanSt.f1 = kalmanSt.f1tmp + kalmanSt.kf1 * ((double)adcL - kalmanSt.f1tmp);
		kalmanSt.covf1 = (1 - kalmanSt.kf1) * kalmanSt.covf1tmp;

		kalmanSt.f2tmp = kalmanSt.f2;
		kalmanSt.covf2tmp = kalmanSt.covf2 + kalmanSt.Qf2;
		kalmanSt.kf2 = kalmanSt.covf2tmp / (kalmanSt.covf2tmp + kalmanSt.Pf2);
		kalmanSt.f2 = kalmanSt.f2tmp + kalmanSt.kf2 * ((double)adcR - kalmanSt.f2tmp);
		kalmanSt.covf2 = (1 - kalmanSt.kf2) * kalmanSt.covf2tmp;
	}
	else
	{
		kalmanSt.f1 = adcL;
		kalmanSt.f2 = adcR;
	}
	adcL = ADC_Get(ADC_L);
	adcR = ADC_Get(ADC_R);
	INFR_L = 1;
	INFR_R = 1;
	if (irCalib.calibMode == 0)
	{
		irCalib.rawf1 = kalmanSt.f1;
		irDist.f1 = IR_FindDis(adcAboveZero((u16)kalmanSt.f1), dataf1, 250);

		irCalib.rawf2 = kalmanSt.f2;
		irDist.f2 = IR_FindDis(adcAboveZero((u16)kalmanSt.f2), dataf2, 250);
	}
	else
	{
		irCalib.rawf1Prev = irCalib.rawf1;
		irCalib.rawf1 = kalmanSt.f1;
		irCalib.rawf1 = (irCalib.rawf1 * 3 + irCalib.rawf1Prev * 7) / 10;

		irCalib.rawf2Prev = irCalib.rawf2;
		irCalib.rawf2 = kalmanSt.f2;
		irCalib.rawf2 = (irCalib.rawf2 * 3 + irCalib.rawf2Prev * 7) / 10;
	}
	DELAY_Us(55);
	adcL = adcAboveZero(ADC_Get(ADC_L) - adcL);
	adcR = adcAboveZero(ADC_Get(ADC_R) - adcR);
	INFR_L = 0;
	INFR_R = 0;

	if (irCalib.kalmanswitch == 1)
	{
		kalmanSt.ltmp = kalmanSt.l;
		kalmanSt.covltmp = kalmanSt.covl + kalmanSt.Ql;
		kalmanSt.kl = kalmanSt.covltmp / (kalmanSt.covltmp + kalmanSt.Pl);
		kalmanSt.l = kalmanSt.ltmp + kalmanSt.kl * ((double)adcL - kalmanSt.ltmp);
		kalmanSt.covl = (1 - kalmanSt.kl) * kalmanSt.covltmp;

		kalmanSt.rtmp = kalmanSt.r;
		kalmanSt.covrtmp = kalmanSt.covr + kalmanSt.Qr;
		kalmanSt.kr = kalmanSt.covrtmp / (kalmanSt.covrtmp + kalmanSt.Pr);
		kalmanSt.r = kalmanSt.rtmp + kalmanSt.kr * ((double)adcR - kalmanSt.rtmp);
		kalmanSt.covr = (1 - kalmanSt.kr) * kalmanSt.covrtmp;
	}
	else
	{
		kalmanSt.l = adcL;
		kalmanSt.r = adcR;
	}

	if (irCalib.calibMode == 0)
	{
		irCalib.rawl = kalmanSt.l;
		irDist.l = IR_FindDis(adcAboveZero((u16)kalmanSt.l), datal, 250);

		irCalib.rawr = kalmanSt.r;
		irDist.r = IR_FindDis(adcAboveZero((u16)kalmanSt.r), datar, 250);
	}
	else
	{
		irCalib.rawlPrev = irCalib.rawl;
		irCalib.rawl = kalmanSt.l;
		irCalib.rawl = (irCalib.rawl * 3 + irCalib.rawlPrev * 7) / 10;

		irCalib.rawrPrev = irCalib.rawr;
		irCalib.rawr = kalmanSt.r;
		irCalib.rawr = (irCalib.rawr * 3 + irCalib.rawrPrev * 7) / 10;
	}
}

/* ---- 标定曲线拟合系数 logV = fitK*logD + fitD (仅本模块用) ---- */
static double fitK, fitD; /* 仅本模块用 */

/**
 * @brief 标定数据有效性校验: 非法时点亮全部 LED 并死循环(标定错误指示)
 * @param logV1 logV2 两点的电压对数值
 * @param logD1 logD2 两点的距离对数值
 * @note 除零保护: 距离相等/电压为0/斜率约0时进入死循环提示标定失败
 */
void IR_IfRight(double logV1, double logV2, double logD1, double logD2)
{
	if (logD1 == logD2 || logV1 == 0 || logV2 == 0 || ((logV1 * logD2 - logV2 * logD1) == 0))
	{
		LED1 = 1;
		LED2 = 1;
		LED3 = 1;
		LED4 = 1;
		LED5 = 1;
		LED6 = 1;
		LED7 = 1;
		LED8 = 1;
		while (1)
			;
	}
}

/**
 * @brief 红外标定曲线拟合: 由两点确定 logV = fitK*logD + fitD
 * @param Y1 X1 第一点的电压对数值/距离对数值
 * @param Y2 X2 第二点的电压对数值/距离对数值
 * @note 结果存入模块内 static fitK/fitD, 由 IR_InfCorrect 调用
 */
void IR_CurveFit(double Y1, double X1, double Y2, double X2)
{
	fitD = (Y1 * X2 - Y2 * X1) / (X2 - X1);
	fitK = (Y1 - fitD) / X1;
}

/**
 * @brief 按拟合曲线生成标定表并写入指定方向表
 * @param num 表长
 * @param infr 目标标定表
 * @param Name 传感器编号(F1/F2/L/R), 决定起始距离 Dec_dist
 * @note 末元素置 0(查表哨兵); 曲线式 temp = fitK*log(Dist+Dec_dist) + fitD
 */
void IR_CheckValue(u16 num, u16 infr[], u8 Name)
{
	u16 Vol, Dist;
	u16 Dec_dist = 0;
	double temp;
	switch (Name)
	{
	case F1:
		Dec_dist = 20;
		break;
	case F2:
		Dec_dist = 20;
		break;
	case L:
		Dec_dist = 21;
		break;
	case R:
		Dec_dist = 20;
		break;
	default:
		break;
	}
	for (Dist = 0; Dist < (num - 1); Dist++)
	{
		temp = fitK * log((double)(Dist + Dec_dist)) + fitD;
		Vol = exp(temp);
		infr[Dist] = (unsigned int)Vol;
	}
	infr[Dist] = 0;
}

/**
 * @brief 红外标定参数校正: 由远近两点重算标定表
 * @param vol1 dist1 近点的实际 ADC 值与距离
 * @param vol2 dist2 远点的实际 ADC 值与距离
 * @param num 表长
 * @param infr 目标标定表
 * @param Name 传感器编号(F1/F2/L/R)
 * @note 先取对数 -> IR_CurveFit 拟合 -> IR_CheckValue 生成表
 */
void IR_InfCorrect(double vol1, double dist1, double vol2, double dist2, u16 num, u16 infr[], u8 Name)
{
	double logV1, logV2, logD1, logD2;
	logV1 = log(vol1);
	logV2 = log(vol2);
	logD1 = log(dist1);
	logD2 = log(dist2);
	IR_CurveFit(logV1, logD1, logV2, logD2);
	IR_CheckValue(num, infr, Name);
}

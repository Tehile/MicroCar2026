/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: floodfill.c —— 洪水填充距离图生成(全闭图等高图/全开图最短路)
*********************************************************************************************************
*/

#include "floodfill.h"
/**
 * @brief 全闭图(未知=有墙)等高线填充: 以目标点为源, 单栈贪心
 * @param cX 目标格横坐标
 * @param cY 目标格纵坐标
 * @note 结果写入 mapStepClose[][]; 栈保存岔路点, 无路可走时弹栈回溯。
 *       判墙读低四位(WALL_*_OPEN), 未访问格低四位=0(全闭: 未知按有墙),
 *       等高图仅沿已探明走廊扩展, 起点等高值即已探明路径长度(上界)
 */
void FLOODFILL_Close(char cX, char cY)
{
    uchar n = 0;      /*  mazeState.stackClose[]下标              */
    uchar ucStep = 0; /*  等高值                      */
    uchar ucStat = 0; /*  统计可前进的方向数          */
    uchar i, j;

    mazeState.stackClose[n].cX = cX; /*  起点X值入栈                 */
    mazeState.stackClose[n].cY = cY; /*  起点Y值入栈                 */
    n++;
    /*
     *  初始化各坐标等高值
     */
    for (i = 0; i < MAZETYPE; i++)
    {
        for (j = 0; j < MAZETYPE; j++)
        {
            mazeState.mapStepClose[i][j] = 0xff;
        }
    }
    /*
     *  制作等高图，直到堆栈中所有数据处理完毕
     */
    while (n)
    {
        mazeState.mapStepClose[cX][cY] = ucStep++; /*  填入等高值                  */

        /*
         *  对当前坐标格里可前进的方向统计
         */
        ucStat = 0;
        if ((mazeState.mapWall[cX][cY] & WALL_UP_OPEN) && /*  前方有路                    */
            (mazeState.mapStepClose[cX][cY + 1] > (ucStep)))
        {             /*  前方等高值大于计划设定值    */
            ucStat++; /*  可前进方向数加1             */
        }
        if ((mazeState.mapWall[cX][cY] & WALL_RIGHT_OPEN) && /*  右方有路                    */
            (mazeState.mapStepClose[cX + 1][cY] > (ucStep)))
        {             /*  右方等高值大于计划设定值    */
            ucStat++; /*  可前进方向数加1             */
        }
        if ((mazeState.mapWall[cX][cY] & WALL_DOWN_OPEN) &&
            (mazeState.mapStepClose[cX][cY - 1] > (ucStep)))
        {
            ucStat++; /*  可前进方向数加1             */
        }
        if ((mazeState.mapWall[cX][cY] & WALL_LEFT_OPEN) &&
            (mazeState.mapStepClose[cX - 1][cY] > (ucStep)))
        {
            ucStat++; /*  可前进方向数加1             */
        }
        /*
         *  没有可前进的方向，则跳转到最近保存的分支点
         *  否则任选一可前进方向前进
         */
        if (ucStat == 0)
        {
            n--;
            cX = mazeState.stackClose[n].cX;
            cY = mazeState.stackClose[n].cY;
            ucStep = mazeState.mapStepClose[cX][cY];
        }
        else
        {
            if (ucStat > 1)
            {                                  /*  有多个可前进方向，保存坐标  */
                mazeState.stackClose[n].cX = cX; /*  横坐标X值入栈               */
                mazeState.stackClose[n].cY = cY; /*  纵坐标Y值入栈               */
                n++;
            }
            /*
             *  任意选择一条可前进的方向前进
             */
            if ((mazeState.mapWall[cX][cY] & WALL_UP_OPEN) && /*  上方有路                    */
                (mazeState.mapStepClose[cX][cY + 1] > (ucStep)))
            {         /*  上方等高值大于计划设定值    */
                cY++; /*  修改坐标                    */
                continue;
            }
            if ((mazeState.mapWall[cX][cY] & WALL_RIGHT_OPEN) && /*  右方有路                    */
                (mazeState.mapStepClose[cX + 1][cY] > (ucStep)))
            {         /*  右方等高值大于计划设定值    */
                cX++; /*  修改坐标                    */
                continue;
            }
            if ((mazeState.mapWall[cX][cY] & WALL_DOWN_OPEN) && /*  下方有路                    */
                (mazeState.mapStepClose[cX][cY - 1] > (ucStep)))
            {         /*  下方等高值大于计划设定值    */
                cY--; /*  修改坐标                    */
                continue;
            }
            if ((mazeState.mapWall[cX][cY] & WALL_LEFT_OPEN) && /*  左方有路                    */
                (mazeState.mapStepClose[cX - 1][cY] > (ucStep)))
            {         /*  左方等高值大于计划设定值    */
                cX--; /*  修改坐标                    */
                continue;
            }
        }
    }
}

/**
 * @brief 全开图(未知=无墙)最短路填充: 以源格为源, 双栈逐层
 * @param X 起点横坐标
 * @param Y 起点纵坐标
 * @note 双栈 stackOpenNow(当前层)/stackOpenNext(下一层) 逐层扩散;
 *       判墙读高四位(WALL_*_MIRROR), 未访问格高四位=0xF(全开: 未知按无墙),
 *       可借道未探索格, 起点步数即理论最短(下界)
 */
void FLOODFILL_Open(char X, char Y)
{
    uchar n = 0;
    uchar m = 0;
    uchar ucStep = 0;
    uchar i, j;
    char cX = X;
    char cY = Y;

    for (i = 0; i < MAZETYPE; i++)
    {
        for (j = 0; j < MAZETYPE; j++)
        {
            mazeState.mapStepOpen[i][j] = 0xff;
        }
    }

    mazeState.mapStepOpen[cX][cY] = ucStep;

    // 起点格入"当前层"栈
    mazeState.stackOpenNow[n].cX = cX; /*  起点X值入栈                 */
    mazeState.stackOpenNow[n].cY = cY;
    n++;

    while (n)
    {
        // 进入下一层: 步数加1
        ucStep++;
        // 弹出当前层各格, 向未赋值的可走邻格扩散
        while (n)
        {
            cX = mazeState.stackOpenNow[n - 1].cX;
            cY = mazeState.stackOpenNow[n - 1].cY;
            if (cY < MAZETYPE - 1)
            {
                if ((mazeState.mapWall[cX][cY] & WALL_UP_MIRROR) && /*  前方有路                    */
                    (mazeState.mapStepOpen[cX][cY + 1] > (ucStep)))
                { /*  前方等高值大于计划设定值    */
                    mazeState.mapStepOpen[cX][cY + 1] = ucStep;
                    mazeState.stackOpenNext[m].cX = cX;
                    mazeState.stackOpenNext[m].cY = cY + 1;
                    m++;
                }
            }
            if (cX < MAZETYPE - 1)
            {
                if ((mazeState.mapWall[cX][cY] & WALL_RIGHT_MIRROR) && /*  右方有路                    */
                    (mazeState.mapStepOpen[cX + 1][cY] > (ucStep)))
                { /*  右方等高值大于计划设定值    */
                    mazeState.mapStepOpen[cX + 1][cY] = ucStep;
                    mazeState.stackOpenNext[m].cX = cX + 1;
                    mazeState.stackOpenNext[m].cY = cY;
                    m++;
                }
            }
            if (cY > 0)
            {
                if ((mazeState.mapWall[cX][cY] & WALL_DOWN_MIRROR) &&
                    (mazeState.mapStepOpen[cX][cY - 1] > (ucStep)))
                {
                    mazeState.mapStepOpen[cX][cY - 1] = ucStep;
                    mazeState.stackOpenNext[m].cX = cX;
                    mazeState.stackOpenNext[m].cY = cY - 1;
                    m++;
                }
            }
            if (cX > 0)
            {
                if ((mazeState.mapWall[cX][cY] & WALL_LEFT_MIRROR) &&
                    (mazeState.mapStepOpen[cX - 1][cY] > (ucStep)))
                {
                    mazeState.mapStepOpen[cX - 1][cY] = ucStep;
                    mazeState.stackOpenNext[m].cX = cX - 1;
                    mazeState.stackOpenNext[m].cY = cY;
                    m++;
                }
            }
            n--;
        }
        // 下一层栈整体搬回当前层栈, 准备下一轮扩散
        for (i = 0; i < m; i++)
        {
            mazeState.stackOpenNow[i].cX = mazeState.stackOpenNext[i].cX;
            mazeState.stackOpenNow[i].cY = mazeState.stackOpenNext[i].cY;
        }
        // 层计数交换: 下一层成为新的当前层
        n = m;
        m = 0;
    }
}

/**
 * @brief 复制全闭图距离图到 mapStepCloseCopy(冲刺距离图备份)
 * @param none
 * @note 供冲刺阶段使用已探明墙图, 不受实时更新影响
 */
void FLOODFILL_CloseCopy(void)
{
    uchar i, j;

    for (i = 0; i < MAZETYPE; i++)
    {
        for (j = 0; j < MAZETYPE; j++)
        {
            mazeState.mapStepCloseCopy[i][j] = mazeState.mapStepClose[i][j];
        }
    }
}

/**
 * @brief 复制全开图距离图到 mapStepOpenCopy(决策距离图备份)
 * @param none
 * @note 供死路剪枝/回程判定使用
 */
void FLOODFILL_OpenCopy(void)
{
    uchar i, j;

    for (i = 0; i < MAZETYPE; i++)
    {
        for (j = 0; j < MAZETYPE; j++)
        {
            mazeState.mapStepOpenCopy[i][j] = mazeState.mapStepOpen[i][j];
        }
    }
}

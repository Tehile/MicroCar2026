/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: maze.c —— 迷宫状态定义、坐标/墙信息更新与墙/访问标记查询
*********************************************************************************************************
*/
/* 本文件在算法中位置: 迷宫核心层--洪水填充(FLOODFILL_Close/Open)生成距离图、8方向斜切路径生成(PATH_Fastway8Dir)、岔路决策与死路剪枝。是冲刺与搜索的策略大脑。 */

#include "maze.h"
/* ---- 迷宫状态全部封装为 MazeState ---- */
MazeState mazeState; /* 零初始化; 非零初值由 MAZE_Init() 运行时设置 */
/**
 * @brief 迷宫状态初始化: 赋终点坐标/冲刺测试动作, 墙图全格闭合
 * @param none
 * @note 由 MOUSE_Init() 调用; ARMCC v5 不支持指定初始化器, 须逐字段赋值
 */
void MAZE_Init(void)
{
    int i, j;
    mazeState.xGoal0 = XDST0;
    mazeState.xGoal1 = XDST1;
    mazeState.yGoal0 = YDST0;
    mazeState.yGoal1 = YDST1;
    mazeState.goalX = 7;
    mazeState.goalY = 7;
    mazeState.spurtTestAction[0] = STRAIGHT90L;
    mazeState.spurtTestAction[1] = STRAIGHT90L;
    mazeState.spurtTestAction[2] = STRAIGHT90L;
    mazeState.spurtTestAction[3] = R180L;
    // 全格墙图初始化为未写入态: 低四位=0(全闭图: 未知按有墙), 高四位=0xF(全开图: 未知按无墙)
    for (i = 0; i < MAZETYPE; i++)
        for (j = 0; j < MAZETYPE; j++)
            mazeState.mapWall[i][j] = 0xf0;
    mazeState.mapWall[0][0] = 0x11;
    mazeState.mapWall[1][0] = 0x70;
}
/* 全局变量定义 */

/**
 * @brief 紧急停机: 清任务并原地等待(调试用)
 * @param none
 * @note 当前未使用
 */
void MAZE_Stop()
{
    motionCtrl.task = NULL;
    mazeState.mouseTask = 99;
    speedSt.speedTargetL = 0;
    speedSt.speedTargetR = 0;
    while (1)
        ;
}

/**
 * @brief 根据当前方向更新鼠标坐标, 并检测当前格墙壁
 * @param none
 * @note 前进一格后调用 MAZE_WallCheck() 更新墙信息
 */
void MAZE_CoorUpdate(void)
{
    // 按绝对方向前进一格(0=上/1=右/2=下/3=左)
    switch (mazeState.mouseDirection)
    {

    case 0:
        mazeState.mousePos.cY++;
        break;

    case 1:
        mazeState.mousePos.cX++;
        break;

    case 2:
        mazeState.mousePos.cY--;
        break;

    case 3:
        mazeState.mousePos.cX--;
        break;

    default:
        break;
    }
    MAZE_WallCheck();
}

/**
 * @brief 高速返回坐标更新: 一次跨越 num 格并检测当前格墙壁
 * @param num 一次前进的格数
 * @note 沿已规划路径快速回程用; 移动后仍调用 MAZE_WallCheck() 同步墙信息
 */
void MAZE_CoorUpdateFast(u8 num)
{
    // 按绝对方向前进 num 格(0=上/1=右/2=下/3=左)
    switch (mazeState.mouseDirection)
    {

    case 0:
        mazeState.mousePos.cY += num;
        break;

    case 1:
        mazeState.mousePos.cX += num;
        break;

    case 2:
        mazeState.mousePos.cY -= num;
        break;

    case 3:
        mazeState.mousePos.cX -= num;
        break;

    default:
        break;
    }
    MAZE_WallCheck();
}

/**
 * @brief 根据传感器检测结果更新当前格墙壁信息并同步相邻镜像格
 * @param none
 * @note 仅当前格低四位为0(未写入)时写入; 墙位镜像到相邻格高四位
 */
void MAZE_WallCheck(void)
{
    uchar ucMap = 0;
    uchar temp, temp1;
    /*
      用绝对方向来表示相对方向上的资料表位置
#define MOUSEWAY_F          (1 <<   mouseDirection)            若 mouseDirection=UP=0  0000 0001  左移    mouseDirection 位            0000 0001
#define MOUSEWAY_R          (1 << ((mouseDirection + 1) % 4))                       0000 0001  左移    （mouseDirection+1)% 4 位    0000 0010
#define MOUSEWAY_B          (1 << ((mouseDirection + 2) % 4))                       0000 0001  左移    (mouseDirection + 2) % 4 位  0000 0100
#define MOUSEWAY_L          (1 << ((mouseDirection + 3) % 4))                       0000 0001  左移    (mouseDirection + 3) % 4 位  0000 1000
#define UP                  0
#define RIGHT               1
#define DOWN                2
#define LEFT                3
    bit3~bit0分别代表 左 下 右 上              0:该方向无路，1:该方向有路       0是有墙，1是没有墙    */

    mazeState.mapVisited[mazeState.mousePos.cX][mazeState.mousePos.cY] = 1;

    ucMap |= MOUSEWAY_B;
    if (wallSns.left == 1)
    {
        ucMap &= ~MOUSEWAY_L;
    }
    else
    {
        ucMap |= MOUSEWAY_L;
    }
    if (wallSns.front == 1)
    {
        ucMap &= ~MOUSEWAY_F;
    }
    else
    {
        ucMap |= MOUSEWAY_F;
    }
    if (wallSns.right == 1)
    {
        ucMap &= ~MOUSEWAY_R;
    }
    else
    {
        ucMap |= MOUSEWAY_R;
    }
    ucMap = (ucMap & WALL_LOW_NIBBLE);
    temp1 = ucMap; //  低四位   和   高四位   一样
    temp = (temp1 << 4);
    ucMap = (ucMap | temp);
    // 初始时 认为低四位=0 "有墙"   高四位=f  认为"没有墙"
    if ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & WALL_LOW_NIBBLE) == 0x00)
    {
        mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] = ucMap;

        if (mazeState.mousePos.cX > 0)
        {
            if ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & WALL_LEFT_OPEN) == 0x00) // 左 下 右 上 1101 1111   1111 0111
            {
                mazeState.mapWall[mazeState.mousePos.cX - 1][mazeState.mousePos.cY] = ((mazeState.mapWall[mazeState.mousePos.cX - 1][mazeState.mousePos.cY]) & ~WALL_RIGHT_MIRROR);
            }
        }

        if (mazeState.mousePos.cX < MAZETYPE - 1)
        {
            if ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & WALL_RIGHT_OPEN) == 0x00) //   0111 1111      1111 1101
            {
                mazeState.mapWall[mazeState.mousePos.cX + 1][mazeState.mousePos.cY] = ((mazeState.mapWall[mazeState.mousePos.cX + 1][mazeState.mousePos.cY]) & ~WALL_LEFT_MIRROR);
            }
        }

        if (mazeState.mousePos.cY > 0)
        {
            if ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & WALL_DOWN_OPEN) == 0x00) // 1110 1111   1111 1011
            {
                mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY - 1] = ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY - 1]) & ~WALL_UP_MIRROR);
            }
        }

        if (mazeState.mousePos.cY < MAZETYPE - 1)
        {
            if ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & WALL_UP_OPEN) == 0x00) // 1011 1111 1111 1110
            {
                mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY + 1] = ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY + 1]) & ~WALL_DOWN_MIRROR);
            }
        }
    }
}

/**
 * @brief 判断搜索是否到达岔路节点(前方有墙且左右至少一侧无墙)
 * @param none
 * @return 1=需跳出当前动作; 0=继续
 */
u8 MAZE_BreakOrNot() // 判断是否需要跳出执行算法
{
    if (wallSns.front == 1)
        return 1;
    else if (wallSns.left == 0)
        return 1;
    else if (wallSns.right == 0)
        return 1;
    else
        return 0;
}
/**
 * @brief 将相对方向转换为绝对方向并计算相邻格坐标
 * @param ucDir 相对方向(MOUSEFRONT/MOUSELEFT/MOUSERIGHT)
 * @param cX 相邻格X坐标(出参)
 * @param cY 相邻格Y坐标(出参)
 * @note MAZE_WallDataGet/MAZE_VisitedDataGet 共用; 以当前朝向为基准
 */
static void MAZE_NeighborCoor(uchar ucDir, char *cX, char *cY)
{
    /*
     *  把电脑鼠的相对方向转换为绝对方向
     */
    switch (ucDir)
    {

    case MOUSEFRONT:
        ucDir = mazeState.mouseDirection;
        break;

    case MOUSELEFT:
        ucDir = (mazeState.mouseDirection + 3) % 4;
        break;

    case MOUSERIGHT:
        ucDir = (mazeState.mouseDirection + 1) % 4;
        break;

    default:
        break;
    }

    /*
     *  根据绝对方向计算该方向上相邻格的坐标
     */
    switch (ucDir)
    {

    case 0:
        *cX = mazeState.mousePos.cX;
        *cY = mazeState.mousePos.cY + 1;
        break;

    case 1:
        *cX = mazeState.mousePos.cX + 1;
        *cY = mazeState.mousePos.cY;
        break;

    case 2:
        *cX = mazeState.mousePos.cX;
        *cY = mazeState.mousePos.cY - 1;
        break;

    case 3:
        *cX = mazeState.mousePos.cX - 1;
        *cY = mazeState.mousePos.cY;
        break;

    default:
        break;
    }
}

/**
 * @brief 获取指定相对方向相邻格的墙壁资料
 * @param ucDirTemp 相对方向(MOUSEFRONT/MOUSELEFT/MOUSERIGHT)
 * @return 相邻格 mapWall 墙壁资料
 */
uchar MAZE_WallDataGet(uchar ucDirTemp)
{
    char cX = 0, cY = 0;
    MAZE_NeighborCoor(ucDirTemp, &cX, &cY);

    return (mazeState.mapWall[cX][cY]); /* 返回该格墙壁资料 */
}

/**
 * @brief 获取指定相对方向相邻格的访问标记
 * @param ucDirTemp 相对方向(MOUSEFRONT/MOUSELEFT/MOUSERIGHT)
 * @return 相邻格 mapVisited 访问标记
 */
uchar MAZE_VisitedDataGet(uchar ucDirTemp)
{
    char cX = 0, cY = 0;
    MAZE_NeighborCoor(ucDirTemp, &cX, &cY);

    return (mazeState.mapVisited[cX][cY]); /* 返回该格访问标记 */
}

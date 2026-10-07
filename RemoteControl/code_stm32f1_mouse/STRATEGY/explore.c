/*
********************************************************************************************************
*                                               示例代码
*                                             EXAMPLE  CODE
*
*               The code is for internal use only, not for commercial transactions(开源学习,请勿商用).
*               The code ADAPTS the corresponding hardware mouse(代码适配对应人工智能电脑鼠/mouse).
*               the specific details consult the professional(欢迎联系我们).
*       ------------------------------------------------------------------------------------------------
*               Function: explore.c —— 贴墙探索法则/中心法则调度/岔路与死路决策
*********************************************************************************************************
*/
/* 贴墙探索命中方向后的转向动作码 */
#include "explore.h"
typedef enum
{
    WALLFOLLOW_NO_TURN = 0, /*  前方命中: 不转弯            */
    WALLFOLLOW_TURN_RIGHT,  /*  右方命中: 右转             */
    WALLFOLLOW_TURN_LEFT    /*  左方命中: 左转             */
} WallFollowTurn_t;

/**
 * @brief 贴墙探索单方向检查: 该方向有路且未走过则按 turnCode 转向
 * @param wayMask 该方向墙壁位掩码(MOUSEWAY_F/R/L, 依赖 mouseDirection)
 * @param visitedDir 该方向的相对方向常量(MOUSEFRONT/RIGHT/LEFT)
 * @param turnCode 命中后的转向动作(WallFollowTurn_t)
 * @return 1=命中(调用方应返回); 0=未命中(继续检查下一优先方向)
 * @note 供6个贴墙法则共用; 各法则仅三方向的检查优先顺序不同
 */
static uchar EXPLORE_WallFollowStep(uchar wayMask, uchar visitedDir, WallFollowTurn_t turnCode)
{
    if ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & wayMask) && /*  该方向有路                  */
        (MAZE_VisitedDataGet(visitedDir) == 0))
    { /*  该方向没有走过              */
        if (turnCode == WALLFOLLOW_TURN_RIGHT)
        {
            MOTION_MouseTurnRight(); /*  电脑鼠右转                  */
        }
        else if (turnCode == WALLFOLLOW_TURN_LEFT)
        {
            MOTION_MouseTurnLeft(); /*  电脑鼠左转                  */
        }
        return 1; /*  命中, 调用方应return        */
    }
    return 0; /*  未命中, 检查下一优先方向    */
}
/**
 * @brief 右手法则: 优先向右, 其次向前, 最后向左
 * @param none
 */
void EXPLORE_WallFollowRight(void)
{
    if (EXPLORE_WallFollowStep(MOUSEWAY_R, MOUSERIGHT, WALLFOLLOW_TURN_RIGHT))
        return; /*  右手法则: 右->前->左  */
    if (EXPLORE_WallFollowStep(MOUSEWAY_F, MOUSEFRONT, WALLFOLLOW_NO_TURN))
        return;
    if (EXPLORE_WallFollowStep(MOUSEWAY_L, MOUSELEFT, WALLFOLLOW_TURN_LEFT))
        return;
}

/**
 * @brief 右左法则: 优先向右, 其次向左, 最后向前
 * @param none
 */
void EXPLORE_WallFollowRightLeft(void)
{
    if (EXPLORE_WallFollowStep(MOUSEWAY_R, MOUSERIGHT, WALLFOLLOW_TURN_RIGHT))
        return; /*  右左法则: 右->左->前  */
    if (EXPLORE_WallFollowStep(MOUSEWAY_L, MOUSELEFT, WALLFOLLOW_TURN_LEFT))
        return;
    if (EXPLORE_WallFollowStep(MOUSEWAY_F, MOUSEFRONT, WALLFOLLOW_NO_TURN))
        return;
}
/**
 * @brief 左手法则: 优先向左, 其次向前, 最后向右
 * @param none
 */
void EXPLORE_WallFollowLeft(void)
{
    if (EXPLORE_WallFollowStep(MOUSEWAY_L, MOUSELEFT, WALLFOLLOW_TURN_LEFT))
        return; /*  左手法则: 左->前->右  */
    if (EXPLORE_WallFollowStep(MOUSEWAY_F, MOUSEFRONT, WALLFOLLOW_NO_TURN))
        return;
    if (EXPLORE_WallFollowStep(MOUSEWAY_R, MOUSERIGHT, WALLFOLLOW_TURN_RIGHT))
        return;
}

/**
 * @brief 左右法则: 优先向左, 其次向右, 最后向前
 * @param none
 */
void EXPLORE_WallFollowLeftRight(void)
{
    if (EXPLORE_WallFollowStep(MOUSEWAY_L, MOUSELEFT, WALLFOLLOW_TURN_LEFT))
        return; /*  左右法则: 左->右->前  */
    if (EXPLORE_WallFollowStep(MOUSEWAY_R, MOUSERIGHT, WALLFOLLOW_TURN_RIGHT))
        return;
    if (EXPLORE_WallFollowStep(MOUSEWAY_F, MOUSEFRONT, WALLFOLLOW_NO_TURN))
        return;
}
/**
 * @brief 中右法则: 优先向前, 其次向右, 最后向左
 * @param none
 */
void EXPLORE_WallFollowFrontRight(void)
{
    if (EXPLORE_WallFollowStep(MOUSEWAY_F, MOUSEFRONT, WALLFOLLOW_NO_TURN))
        return; /*  中右法则: 前->右->左  */
    if (EXPLORE_WallFollowStep(MOUSEWAY_R, MOUSERIGHT, WALLFOLLOW_TURN_RIGHT))
        return;
    if (EXPLORE_WallFollowStep(MOUSEWAY_L, MOUSELEFT, WALLFOLLOW_TURN_LEFT))
        return;
}
/**
 * @brief 中左法则: 优先向前, 其次向左, 最后向右
 * @param none
 */
void EXPLORE_WallFollowFrontLeft(void)
{
    if (EXPLORE_WallFollowStep(MOUSEWAY_F, MOUSEFRONT, WALLFOLLOW_NO_TURN))
        return; /*  中左法则: 前->左->右  */
    if (EXPLORE_WallFollowStep(MOUSEWAY_L, MOUSELEFT, WALLFOLLOW_TURN_LEFT))
        return;
    if (EXPLORE_WallFollowStep(MOUSEWAY_R, MOUSERIGHT, WALLFOLLOW_TURN_RIGHT))
        return;
}
/**
 * @brief 中心法则: 按当前格所在象限与朝向选择贴墙法则
 * @param none
 * @note 以 cX&0x08/cY&0x08 判断四象限(1/4迷宫中心线), 配合朝向与对角线位置选法则
 */
void EXPLORE_CentralMethod(void)
{
    if (mazeState.mousePos.cX & 0x08)
    {
        if (mazeState.mousePos.cY & 0x08)
        {

            /*
             *  此时电脑鼠在迷宫的右上角
             */
            switch (mazeState.mouseDirection)
            {

            case UP:                      /*  当前电脑鼠向上              */
                EXPLORE_WallFollowLeft(); /*  左手法则                    */
                break;

            case RIGHT:                    /*  当前电脑鼠向右              */
                EXPLORE_WallFollowRight(); /*  右手法则                    */
                break;

            case DOWN: /*  当前电脑鼠向下              */
                if (mazeState.mousePos.cX <= mazeState.mousePos.cY)
                    EXPLORE_WallFollowFrontRight(); /*  中右法则                    */
                else
                    EXPLORE_WallFollowRight();
                break;

            case LEFT: /*  当前电脑鼠向左              */
                if (mazeState.mousePos.cX >= mazeState.mousePos.cY)
                    EXPLORE_WallFollowFrontLeft(); /*  中左法则                    */
                else
                    EXPLORE_WallFollowLeft();
                break;

            default:
                break;
            }
        }
        else
        {

            /*
             *  此时电脑鼠在迷宫的右下角
             */
            switch (mazeState.mouseDirection)
            {

            case UP: /*  当前电脑鼠向上              */
                if (mazeState.mousePos.cX - 7 <= 8 - mazeState.mousePos.cY)
                    EXPLORE_WallFollowFrontLeft(); /*  中左法则                    */
                else
                    EXPLORE_WallFollowLeft();
                break;

            case RIGHT:                   /*  当前电脑鼠向右              */
                EXPLORE_WallFollowLeft(); /*  左手法则                    */
                break;

            case DOWN:                     /*  当前电脑鼠向下              */
                EXPLORE_WallFollowRight(); /*  右手法则                    */
                break;

            case LEFT: /*  当前电脑鼠向左              */
                if (mazeState.mousePos.cX - 7 >= 8 - mazeState.mousePos.cY)
                    EXPLORE_WallFollowFrontRight(); /*  中右法则                    */
                else
                    EXPLORE_WallFollowRight();
                break;

            default:
                break;
            }
        }
    }
    else
    {
        if (mazeState.mousePos.cY & 0x08)
        {

            /*
             *  此时电脑鼠在迷宫的左上角
             */
            switch (mazeState.mouseDirection)
            {

            case UP:                       /*  当前电脑鼠向上              */
                EXPLORE_WallFollowRight(); /*  右手法则                    */
                break;

            case RIGHT: /*  当前电脑鼠向右              */
                if (8 - mazeState.mousePos.cX >= mazeState.mousePos.cY - 7)
                    EXPLORE_WallFollowFrontRight(); /*  中右法则                    */
                else
                    EXPLORE_WallFollowRight();
                break;

            case DOWN: /*  当前电脑鼠向下              */
                if (8 - mazeState.mousePos.cX <= mazeState.mousePos.cY - 7)
                    EXPLORE_WallFollowFrontLeft(); /*  中左法则                    */
                else
                    EXPLORE_WallFollowLeft();
                break;

            case LEFT:                    /*  当前电脑鼠向左              */
                EXPLORE_WallFollowLeft(); /*  左手法则                    */
                break;

            default:
                break;
            }
        }
        else
        {

            /*
             *  此时电脑鼠在迷宫的左下角
             */
            switch (mazeState.mouseDirection)
            {

            case UP: /*  当前电脑鼠向上              */
                if (8 - mazeState.mousePos.cX <= 8 - mazeState.mousePos.cY)
                    EXPLORE_WallFollowFrontRight(); /*  中右法则                    */
                else
                    EXPLORE_WallFollowRight();
                break;

            case RIGHT: /*  当前电脑鼠向右              */
                if (8 - mazeState.mousePos.cX >= 8 - mazeState.mousePos.cY)
                    EXPLORE_WallFollowFrontLeft(); /*  中左法则                    */
                else
                    EXPLORE_WallFollowLeft();
                break;

            case DOWN:                    /*  当前电脑鼠向下              */
                EXPLORE_WallFollowLeft(); /*  左手法则                    */
                break;

            case LEFT:                     /*  当前电脑鼠向左              */
                EXPLORE_WallFollowRight(); /*  右手法则                    */
                break;

            default:
                break;
            }
        }
    }
}
/**
 * @brief 统计某坐标格未走过的支路数(岔路判定)
 * @param cX 检测格横坐标
 * @param cY 检测格纵坐标
 * @return 未走过的支路数
 */
uchar EXPLORE_CrosswayCheck(char cX, char cY)
{
    uchar ucCt = 0;
    if ((mazeState.mapWall[cX][cY] & WALL_UP_OPEN) && /*  绝对方向，迷宫上方有路      */
        (mazeState.mapVisited[cX][cY + 1] == 0))
    {           /*  绝对方向，迷宫上方未走过    */
        ucCt++; /*  可前进方向数加1             */
    }
    if ((mazeState.mapWall[cX][cY] & WALL_RIGHT_OPEN) && /*  绝对方向，迷宫右方有路      */
        (mazeState.mapVisited[cX + 1][cY] == 0))
    {           /*  绝对方向，迷宫右方没有走过  */
        ucCt++; /*  可前进方向数加1             */
    }
    if ((mazeState.mapWall[cX][cY] & WALL_DOWN_OPEN) && /*  绝对方向，迷宫下方有路      */
        (mazeState.mapVisited[cX][cY - 1] == 0))
    {           /*  绝对方向，迷宫下方未走过    */
        ucCt++; /*  可前进方向数加1             */
    }
    if ((mazeState.mapWall[cX][cY] & WALL_LEFT_OPEN) && /*  绝对方向，迷宫左方有路      */
        (mazeState.mapVisited[cX - 1][cY] == 0))
    {           /*  绝对方向，迷宫左方未走过    */
        ucCt++; /*  可前进方向数加1             */
    }
    return ucCt;
}
/**
 * @brief 岔路决策: 比较前/左/右三个方向的全开图步数选择贴墙法则
 * @param none
 * @note 依 sF/sL/sR 大小关系落入不同法则; 均不满足则退回中心法则
 */
void EXPLORE_CrosswayChoice(void)
{
    uchar sF, sL, sR;
    // 以目标格为源重算全开图最短路, 得到各格到目标的距离
    FLOODFILL_Open(mazeState.goalX, mazeState.goalY);
    sF = EXPLORE_StepGet(MOUSEFRONT);
    sL = EXPLORE_StepGet(MOUSELEFT);
    sR = EXPLORE_StepGet(MOUSERIGHT);

    // 右<前<=左: 右手法则
    if ((sR < sF) && (sF <= sL))
    {
        EXPLORE_WallFollowRight();
    }
    // 右<左<=前: 右左法则
    else if ((sR < sL) && (sL <= sF))
    {
        EXPLORE_WallFollowRightLeft();
    }
    // 左<前<=右: 左手法则
    else if ((sL < sF) && (sF <= sR))
    {
        EXPLORE_WallFollowLeft();
    }
    // 左<右<=前: 左右法则
    else if ((sL < sR) && (sR <= sL))
    {
        EXPLORE_WallFollowLeftRight();
    }
    // 前<右<=左: 中右法则
    else if ((sF < sR) && (sR <= sL))
    {
        EXPLORE_WallFollowFrontRight();
    }
    // 前<左<=右: 中左法则
    else if ((sF < sL) && (sL <= sR))
    {
        EXPLORE_WallFollowFrontLeft();
    }
    // 其余情况: 中心法则兜底
    else
    {
        EXPLORE_CentralMethod();
    }
}
/**
 * @brief 目标格墙位同步: 低四位墙位镜像到高四位
 * @param goalX 目标格横坐标
 * @param goalY 目标格纵坐标
 * @note 先清终点四格高四位(仅保留低四位), 再写入目标格镜像, 供洪水填充判墙
 */
void EXPLORE_GoalWallChange(char goalX, char goalY)
{
    uchar temp, temp1;
    mazeState.mapWall[mazeState.xGoal0][mazeState.yGoal0] = ((mazeState.mapWall[mazeState.xGoal0][mazeState.yGoal0]) & WALL_LOW_NIBBLE);
    mazeState.mapWall[mazeState.xGoal0][mazeState.yGoal1] = ((mazeState.mapWall[mazeState.xGoal0][mazeState.yGoal1]) & WALL_LOW_NIBBLE);
    mazeState.mapWall[mazeState.xGoal1][mazeState.yGoal0] = ((mazeState.mapWall[mazeState.xGoal1][mazeState.yGoal0]) & WALL_LOW_NIBBLE);
    mazeState.mapWall[mazeState.xGoal1][mazeState.yGoal1] = ((mazeState.mapWall[mazeState.xGoal1][mazeState.yGoal1]) & WALL_LOW_NIBBLE);
    temp1 = mazeState.mapWall[goalX][goalY];
    temp = (temp1 << 4);
    mazeState.mapWall[goalX][goalY] = ((mazeState.mapWall[goalX][goalY]) | temp);
}

/**
 * @brief 回退点选择: 在未访问且更近的支路方向转弯
 * @param cX 目标格横坐标
 * @param cY 目标格纵坐标
 * @note 优先找距目标更近(全开图步数小于目标)的未访问方向; 否则只看未访问方向
 */
void EXPLORE_BackPointChoice(char cX, char cY)
{
    if ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & MOUSEWAY_F) && /*  电脑鼠的前方有路            */
        (MAZE_VisitedDataGet(MOUSEFRONT) == 0) &&
        (EXPLORE_StepGet(MOUSEFRONT) < mazeState.mapStepOpen[cX][cY]))
    {
        return; /*  电脑鼠不用转弯              */
    }
    if ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & MOUSEWAY_L) && /*  电脑鼠的左边有路            */
        (MAZE_VisitedDataGet(MOUSELEFT) == 0) &&
        (EXPLORE_StepGet(MOUSELEFT) < mazeState.mapStepOpen[cX][cY]))
    {
        MOTION_MouseTurnLeft(); /*  电脑鼠左转                  */
        return;
    }
    if ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & MOUSEWAY_R) && /*  电脑鼠的右边有路            */
        (MAZE_VisitedDataGet(MOUSERIGHT) == 0) &&
        (EXPLORE_StepGet(MOUSERIGHT) < mazeState.mapStepOpen[cX][cY]))
    {
        MOTION_MouseTurnRight(); /*  电脑鼠右转                  */
        return;
    }
    if ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & MOUSEWAY_F) && /*  电脑鼠的前方有路            */
        (MAZE_VisitedDataGet(MOUSEFRONT) == 0))
    {
        return; /*  电脑鼠不用转弯              */
    }
    if ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & MOUSEWAY_L) && /*  电脑鼠的左边有路            */
        (MAZE_VisitedDataGet(MOUSELEFT) == 0))
    {
        MOTION_MouseTurnLeft(); /*  电脑鼠左转                  */
        return;
    }
    if ((mazeState.mapWall[mazeState.mousePos.cX][mazeState.mousePos.cY] & MOUSEWAY_R) && /*  电脑鼠的右边有路            */
        (MAZE_VisitedDataGet(MOUSERIGHT) == 0))
    {
        MOTION_MouseTurnRight(); /*  电脑鼠右转                  */
        return;
    }
}

/**
 * @brief 获取指定相对方向相邻格在全开图距离图中的步数
 * @param ucDirTemp 相对方向(MOUSEFRONT/MOUSELEFT/MOUSERIGHT)
 * @return 全开图距离图步数; 坐标越界返回 255
 */
uchar EXPLORE_StepGet(uchar ucDirTemp)
{
    char cX = 0, cY = 0;
    switch (ucDirTemp)
    {
    case MOUSEFRONT:
        ucDirTemp = mazeState.mouseDirection;
        break;
    case MOUSELEFT:
        ucDirTemp = (mazeState.mouseDirection + 3) % 4;
        break;
    case MOUSERIGHT:
        ucDirTemp = (mazeState.mouseDirection + 1) % 4;
        break;
    default:
        break;
    }
    switch (ucDirTemp)
    {
    case 0:
        cX = mazeState.mousePos.cX;
        cY = mazeState.mousePos.cY + 1;
        break;
    case 1:
        cX = mazeState.mousePos.cX + 1;
        cY = mazeState.mousePos.cY;
        break;
    case 2:
        cX = mazeState.mousePos.cX;
        cY = mazeState.mousePos.cY - 1;
        break;
    case 3:
        cX = mazeState.mousePos.cX - 1;
        cY = mazeState.mousePos.cY;
        break;

    default:
        break;
    }
    if ((cX <= 15) && (cY <= 15))
    {
        return (mazeState.mapStepOpen[cX][cY]);
    }
    else
    {
        return 255;
    }
}

// 死路剪枝
/**
 * @brief 死路剪枝: 临时封路->重算连通性->回退
 * @param cross 当前岔路索引
 * @return 应回退到的岔路索引
 * @note 临时封闭某支路后重算全开图最短路连通性, 目标不可达则回退封路, 减少冗余探索
 */
uchar EXPLORE_DeadwayCheck(uchar cross)
{
    uchar backcross = cross; // 死区返回点
    uchar n = cross;
    uchar m;
    uchar bx = mazeState.mousePos.cX;
    uchar by = mazeState.mousePos.cY;
    uchar countmap = 0;

    uchar tempRoadState;
    uchar tempx, tempy;
    uchar tempBlock;

    FLOODFILL_Open(mazeState.goalX, mazeState.goalY);
    FLOODFILL_OpenCopy();

    // 从当前岔路索引往前逐个检查支路
    while (--n)
    {
        // 该岔路未走过的支路数
        tempRoadState = EXPLORE_CrosswayCheck(mazeState.crosswayBuf[n].cX, mazeState.crosswayBuf[n].cY);
        if (tempRoadState)
        {
            tempx = mazeState.crosswayBuf[n].cX;
            tempy = mazeState.crosswayBuf[n].cY;
            // 仅处理比当前回退点更近的支路
            if (mazeState.mapStepOpenCopy[tempx][tempy] < mazeState.mapStepOpenCopy[bx][by])
            {
                tempBlock = mazeState.mapWall[tempx][tempy];
                // 临时封死该格
                mazeState.mapWall[tempx][tempy] = 0x00;
                FLOODFILL_Open(mazeState.mousePos.cX, mazeState.mousePos.cY);
                countmap++;
                mazeState.mapWall[tempx][tempy] = tempBlock;
                // 封路后目标不可达(连通性被破坏)
                if (mazeState.mapStepOpen[mazeState.goalX][mazeState.goalY] == 0xff)
                {
                    m = cross;
                    while (--m > n)
                    {
                        if (mazeState.mapStepOpen[mazeState.crosswayBuf[m].cX][mazeState.crosswayBuf[m].cY] != 0xff)
                        {
                            continue;
                        }
                        else
                        {
                            goto enddead;
                        }
                    }
                    bx = tempx;
                    by = tempy;
                    backcross = n;
                }
                // 重算次数上限, 防止死循环
                if (countmap >= 20)
                {
                    break;
                }
            }
            else
            {
                continue;
            }
        }
        else
        {

            continue;
        }
    }
enddead:
    return backcross;
}


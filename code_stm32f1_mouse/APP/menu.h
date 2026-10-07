#ifndef _MENU_H_
#define _MENU_H_
#include "main.h"

/**
 * @brief  按键菜单选择与分发
 * @note   仅当选中档位 0/1 时返回(继续主流程); 其余档位进入不返回的功能循环。
 */
void MENU_Run(void);

#endif

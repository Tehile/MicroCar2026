from docx import Document
from docx.shared import Pt

document = Document()

document.add_heading('电脑鼠代码分析报告', 0)

document.add_heading('1. 代码整体结构与功能模块', level=1)
table = document.add_table(rows=1, cols=3)
table.style = 'Table Grid'
hdr_cells = table.rows[0].cells
hdr_cells[0].text = '文件路径'
hdr_cells[1].text = '主要功能'
hdr_cells[2].text = '详细说明'

data1 = [
    ('USER/test.c', '主程序入口 & 状态机', '包含 main() 函数。负责硬件初始化、按键模式选择（搜索、冲刺、调试等）、以及最高层的任务调度（GucMouseTask）。它决定了电脑鼠是处于“搜索迷宫”、“回冲起点”还是“冲刺终点”的状态。'),
    ('STRATAGY/stratagy.c', '迷宫算法 & 路径规划', '核心大脑。包含 mazeSearch()（迷宫搜索算法，如洪水填充）、mapStepEdit（计算等高图）、fastway_90（生成冲刺路径）。它告诉电脑鼠“下一步去哪个格子”。'),
    ('HARDWARE/TIMER/timer.c', '运动控制 & PID闭环', '核心小脑。包含 SysTick_Handler（1ms中断服务函数）。负责读取传感器（ADC）、读取编码器、执行PID算法控制电机速度、以及根据 task 变量执行具体的动作（如直行、转弯、校准）。'),
    ('HARDWARE/ADC/adc.c', '传感器采集', '负责红外传感器和陀螺仪的ADC数据采集，提供原始数据给控制层。'),
    ('HARDWARE/STMFLASH/stmflash.c', '数据存储', '负责将迷宫地图、校准数据写入Flash，确保掉电不丢失，实现“一次搜索，多次冲刺”。')
]

for path, func, desc in data1:
    row_cells = table.add_row().cells
    row_cells[0].text = path
    row_cells[1].text = func
    row_cells[2].text = desc

document.add_heading('2. 核心参数与调参指南', level=1)
document.add_paragraph('为了实现稳定识别终点、回到起点并冲刺，您需要重点关注以下参数。这些参数主要集中在 HARDWARE/TIMER/timer.c 和 USER/test.c 中。')

document.add_heading('A. 基础运动与稳定性 (HARDWARE/TIMER/timer.c)', level=2)
table2 = document.add_table(rows=1, cols=4)
table2.style = 'Table Grid'
hdr_cells2 = table2.rows[0].cells
hdr_cells2[0].text = '参数名'
hdr_cells2[1].text = '所在文件'
hdr_cells2[2].text = '功能描述'
hdr_cells2[3].text = '调参建议 (重要)'

data2 = [
    ('speed_ss', 'timer.c', '搜索速度。电脑鼠在未知迷宫中探索时的基础速度。', '适中。太快容易撞墙或漏读墙壁；太慢效率低。建议 30-50 左右。'),
    ('speed_turn', 'timer.c', '转弯速度。原地转弯或行进间转弯的速度基准。', '低。保证转弯精准，防止过冲。建议 20-30。'),
    ('GOSTRAIGHT_kp', 'timer.c', '直行陀螺仪P系数。用于 GOSTRAIGHT 任务，保持直线行驶不偏航。', '关键。增大可提高抗干扰能力，但过大会导致震荡。观察直线行驶是否走S形来调整。'),
    ('turn_kp, turn_kd', 'timer.c', '转弯PID系数。控制转弯的角度精度。', '关键。kp不足转不到位，kp过大转过头。kd用于抑制过冲。需反复调试直到转弯正好90度/180度。'),
    ('sidewall_threshold', 'timer.c', '侧墙阈值。红外传感器读数大于此值认为有墙。', '基础。必须根据实际场地光线和传感器灵敏度校准。在 test.c 的传感器调试模式下测得有墙/无墙的中间值。'),
    ('keep_dis', 'timer.c', '靠墙距离。沿墙走时，期望的红外传感器读数。', '基础。让车停在跑道正中间，读取此时的传感器数值填入。保证车在正中间行驶。')
]

for name, file, func, advice in data2:
    row_cells = table2.add_row().cells
    row_cells[0].text = name
    row_cells[1].text = file
    row_cells[2].text = func
    row_cells[3].text = advice

document.add_heading('B. 冲刺与高速控制 (HARDWARE/TIMER/timer.c)', level=2)
table3 = document.add_table(rows=1, cols=4)
table3.style = 'Table Grid'
hdr_cells3 = table3.rows[0].cells
hdr_cells3[0].text = '参数名'
hdr_cells3[1].text = '所在文件'
hdr_cells3[2].text = '功能描述'
hdr_cells3[3].text = '调参建议 (重要)'

data3 = [
    ('speed_fastss', 'timer.c', '冲刺速度。已知地图后的直线最高速度。', '高。逐步增加，直到出现打滑或控制不住。这是比赛成绩的关键。'),
    ('SPURT90IR_kp1/2', 'timer.c', '冲刺红外校正系数。高速下利用红外传感器修正位置的力度。', '极重要。高速下如果修正太猛会直接撞墙（震荡），修正太弱会慢慢偏离。分段调节（大误差用kp1，小误差用kp2）。'),
    ('SPURT90GYRO_kp', 'timer.c', '盲跑陀螺仪系数。无墙区域（盲跑）依靠陀螺仪走直线的力度。', '重要。在长直道无墙段，完全依赖此参数保持航向。')
]

for name, file, func, advice in data3:
    row_cells = table3.add_row().cells
    row_cells[0].text = name
    row_cells[1].text = file
    row_cells[2].text = func
    row_cells[3].text = advice

document.add_heading('C. 策略与路径规划 (USER/test.c & STRATAGY/stratagy.c)', level=2)
table4 = document.add_table(rows=1, cols=4)
table4.style = 'Table Grid'
hdr_cells4 = table4.rows[0].cells
hdr_cells4[0].text = '参数名'
hdr_cells4[1].text = '所在文件'
hdr_cells4[2].text = '功能描述'
hdr_cells4[3].text = '调参建议 (重要)'

data4 = [
    ('GucXGoal0, GucYGoal0', 'stratagy.c', '终点坐标。迷宫终点的X, Y坐标。', '必须正确。通常比赛规则规定终点在中心或特定位置。'),
    ('stepMax', 'test.c', '等高表最大步数限制。可能用于限制搜索深度或优化路径计算。', '一般默认即可，若迷宫极大可能需调整。'),
    ('LTopSpeed, L45TopSpeed', 'test.c', '冲刺最高限速。限制直线和斜线的最大速度。', '在 test.c 的 MAZESEARCH 状态下，回冲前会设置这些值。根据电机性能和场地摩擦力设定上限。')
]

for name, file, func, advice in data4:
    row_cells = table4.add_row().cells
    row_cells[0].text = name
    row_cells[1].text = file
    row_cells[2].text = func
    row_cells[3].text = advice

document.add_heading('3. 关键函数解析', level=1)
document.add_paragraph('为了实现您的需求，代码执行流程如下，涉及的关键函数：')

document.add_heading('1. 搜索阶段 (Search):', level=2)
document.add_paragraph('mazeSearch() (stratagy.c): 核心算法，判断当前位置，更新墙壁信息，计算下一步去哪。', style='List Bullet')
document.add_paragraph('mapStepEdit() (stratagy.c): 洪水填充算法（Floodfill），计算全图到终点的步数，生成“等高图”。', style='List Bullet')
document.add_paragraph('task = SMARTSTRAIGHT / task = TURN (timer.c): 底层执行单元，根据上层指令控制电机。', style='List Bullet')

document.add_heading('2. 到达终点与回冲 (Return):', level=2)
document.add_paragraph('在 test.c 的 MAZESEARCH case 中，检测到 ((GmcMouse.cX==GucXGoal0)...) 后：', style='List Bullet')
document.add_paragraph('Block_Write(): 保存地图到Flash。', style='List Bullet')
document.add_paragraph('mapStepEditS(GucXStart, GucYStart): 反向计算，以起点为目标生成等高图。', style='List Bullet')
document.add_paragraph('fastway_90(GucXStart, GucYStart): 生成从终点回到起点的最短路径。', style='List Bullet')
document.add_paragraph('specialmouseTurnback(): 执行原地掉头。', style='List Bullet')

document.add_heading('3. 冲刺阶段 (Sprint):', level=2)
document.add_paragraph('回到起点后，程序通常会进入等待按键或自动开始冲刺的状态。', style='List Bullet')
document.add_paragraph('fastway_90(GoalX, GoalY): 再次计算从起点到终点的最短冲刺路径。', style='List Bullet')
document.add_paragraph('task = SPURT90IR / SPURT90BLIND (timer.c): 切换到高速PID参数，执行冲刺。', style='List Bullet')

document.add_heading('4. 调试建议总结', level=1)
document.add_paragraph('第一步：传感器校准。在 test.c 中使用校准模式（Case 6），确保 sidewall_threshold 和 keep_dis 准确。这是所有运动的基础。', style='List Number')
document.add_paragraph('第二步：低速搜索稳定性。调整 GOSTRAIGHT_kp 和 turn_kp，确保搜索时走直线不抖动，转弯正好90度。', style='List Number')
document.add_paragraph('第三步：回冲逻辑验证。在 test.c 中，确保到达终点后的 mapStepEditS 和 fastway_90 调用参数正确（目标是起点坐标）。', style='List Number')
document.add_paragraph('第四步：冲刺速度递增。先用较低的 speed_fastss 测试冲刺路径是否正确，然后逐步提高速度，并配合调整 SPURT90IR_kp 防止高速撞墙。', style='List Number')

document.save('电脑鼠代码分析报告.docx')

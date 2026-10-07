# 电脑鼠代码 新旧变量对照表

> **用途**: reverse-map 验证时把新名映回原始变量名。
> **背景**: 最初代码**无结构体,全部为全局变量**;P7 全局变量治理后封装为「结构体.成员」。
> 注释体系重构(清理 `(原xxx)`)后,本表为旧名追溯唯一依据。

## 一、封装入结构体

### odomStr(OdomStruct, 编码器/里程计)
| 原始变量名 | 新变量名 |
|---|---|
| leftb | odomStr.encL |
| rightb | odomStr.encR |
| leftbh | odomStr.encLPrev |
| rightbh | odomStr.encRPrev |
| loopahead | odomStr.encWrapCnt |
| absoluteangle | odomStr.angleAbs |
| baseangle | odomStr.angleBase |
| leftd | odomStr.distL |
| rightd | odomStr.distR |
| leftdh | odomStr.distLPrev |
| rightdh | odomStr.distRPrev |
| leftcycle | odomStr.distLCycle |
| rightcycle | odomStr.distRCycle |
| absolutetrack | odomStr.trackAbs |
| absolutetrackl | odomStr.trackAbsL |
| absolutetrackr | odomStr.trackAbsR |
| basetrack | odomStr.trackBase |
| basetrackl | odomStr.trackBaseL |
| basetrackr | odomStr.trackBaseR |
| trackl | odomStr.trackL |
| trackr | odomStr.trackR |
| cellOdom | odomStr.cellOdom |
| angle | odomStr.angle |
| angleskip | odomStr.angleSkip |
| anglehis | odomStr.anglehis[50] |
| newindex | odomStr.newindex |
| anglehismax | odomStr.anglehismax |
| anglehismin | odomStr.anglehismin |

### spurtCfg(SpurtConfig, 冲刺PID)
| 原始变量名 | 新变量名 |
|---|---|
| SPURT90IR_kp1 | spurtCfg.irKp1 |
| SPURT90IR_kp2 | spurtCfg.irKp2 |
| SPURT90IR_kd | spurtCfg.irKd |
| SPURT90GYRO_kp | spurtCfg.gyroKp |
| SPURT90GYRO_kd | spurtCfg.gyroKd |

### motionCtrl(MotionCtrl, 运动PID)
| 原始变量名 | 新变量名 |
|---|---|
| pidErr | motionCtrl.pidErr |
| pidErrPrev | motionCtrl.pidErrPrev |
| pidErrInteg | motionCtrl.pidErrInteg |
| isrTick | motionCtrl.tick |
| motionTask | motionCtrl.task |
| step | motionCtrl.step |

### irDist(IrDist, 红外距离)
| 原始变量名 | 新变量名 |
|---|---|
| disf1 / disf2 | irDist.f1 / irDist.f2 |
| disf | irDist.f |
| disl / disr | irDist.l / irDist.r |
| disl45 / disr45 | irDist.l45 / irDist.r45 |
| dislh / disrh | irDist.lPrev / irDist.rPrev |
| dislhh / disrhh | irDist.lPrev2 / irDist.rPrev2 |
| disl45h / disr45h | irDist.l45Prev / irDist.r45Prev |
| disl45hh / disr45hh | irDist.l45Prev2 / irDist.r45Prev2 |

### gyroStr(GyroStruct, 陀螺状态)
> 旧名为 P6.7-B 规范化后的裸全局名(gyro 前缀);更早期原名(gyro/gyro0/gdiff/gangle 等)见 git 历史。
| 原始变量名 | 新变量名 |
|---|---|
| gyroRaw | gyroStr.raw |
| gyroBias | gyroStr.bias |
| gyroDiff | gyroStr.diff |
| gyroDiffPrev | gyroStr.diffPrev |
| gyroAngle | gyroStr.angle |
| gyroAngleAcc | gyroStr.angleAcc |
| gyroAngleShort | gyroStr.angleShort |
| gyroAnglePrev | gyroStr.anglePrev |
| gyroAnglePrev2 | gyroStr.anglePrev2 |
| gyroAnglePeak | gyroStr.anglePeak |
| gyroAnglePeakPrev | gyroStr.anglePeakPrev |
| gyroInitFlag | gyroStr.initFlag |
| gyroSwitch | gyroStr.switchFlag |
| gyroAdjFlag | gyroStr.adjFlag |
| gyroTarget | gyroStr.target |
| angleCounter | gyroStr.adjCounter |
| gangleSee | gyroStr.correctionEnable |

### sysCfg(SysConfig, 系统配置)
| 原始变量名 | 新变量名 |
|---|---|
| gangleratio | sysCfg.gyroRatio |
| dis2wall | sysCfg.distToWall |
| angleratio | sysCfg.encAngleRatio |
| TrackRatioL / TrackRatioR | sysCfg.trackRatioL / sysCfg.trackRatioR |
| SpeedRatioL / SpeedRatioR | sysCfg.speedRatioL / sysCfg.speedRatioR |
| trackcoor | sysCfg.trackCoord |

### speedCfg(SpeedConfig, 速度配置)
| 原始变量名 | 新变量名 |
|---|---|
| speedSs | speedCfg.search |
| speedTurn | speedCfg.turn |
| speedFastss | speedCfg.fastSprint |
| forwardirdis | speedCfg.forwardIr |

### speedSt(SpeedState, 运行时速度/电机PWM)
| 原始变量名 | 新变量名 |
|---|---|
| speedl / speedr | speedSt.speedL / speedSt.speedR |
| speed | speedSt.avgSpeed |
| setvl / setvr | speedSt.speedTargetL / speedSt.speedTargetR |
| pwml / pwmr | speedSt.pwmMotorL / speedSt.pwmMotorR |
| stoptick | speedSt.stoptick |
| turnaroundTick | speedSt.turnaroundTick |
| freemode | speedSt.freemode |
| sleepmode | speedSt.sleepmode |

### flashSt(FlashState, Flash保存状态)
| 原始变量名 | 新变量名 |
|---|---|
| flashwrite | flashSt.flashwrite |
| flashenable | flashSt.flashenable |
| saveDataNum | flashSt.saveDataNum |
| saveDataTime | flashSt.saveDataTime |
| blockWriteFlag | flashSt.blockWriteFlag |

### pidGain(PidGainConfig, PID增益)
| 原始变量名 | 新变量名 |
|---|---|
| GOSTRAIGHT_kp | pidGain.goStraight |
| SMARTSTRAIGHTFAST_bigerror_kp | pidGain.smartFastBig |
| SMARTSTRAIGHTFAST_smallerror_kp | pidGain.smartFastSm |
| STRAIGHT45_kp / STRAIGHT45_kd | pidGain.straight45P / pidGain.straight45D |
| turn_kp / turn_kd | pidGain.turnP / pidGain.turnD |
| accratio / decratio | pidGain.accRatio / pidGain.decRatio |

### turnTbl(TurnTable, 转弯表指针)
| 原始变量名 | 新变量名 |
|---|---|
| turn_gyro | turnTbl.gyro |
| turn_vl | turnTbl.vl |
| turn_vr | turnTbl.vr |

### wallSns(WallSensor, 墙检测+红外控制)
| 原始变量名 | 新变量名 |
|---|---|
| lefthaswall | wallSns.left |
| righthaswall | wallSns.right |
| forwardhaswall | wallSns.front |
| lefthaswallh | wallSns.leftPrev |
| righthaswallh | wallSns.rightPrev |
| lefthaswall45 | wallSns.left45 |
| righthaswall45 | wallSns.right45 |
| lefthaswallh45 | wallSns.left45Prev |
| righthaswallh45 | wallSns.right45Prev |
| leftwallcritical | wallSns.critL |
| rightwallcritical | wallSns.critR |
| leftwallcriticalh / leftwallcriticalhh / leftwallcriticalhhh | wallSns.critL1 / critL2 / critL3 |
| rightwallcriticalh / rightwallcriticalhh / rightwallcriticalhhh | wallSns.critR1 / critR2 / critR3 |
| dislpeak / disrpeak | wallSns.peakL / wallSns.peakR |
| leftpeaktrack / rightpeaktrack | wallSns.peakTrkL / wallSns.peakTrkR |
| irswitch | wallSns.irSwitch |
| sidewallThreshold | wallSns.wallThresh |

### emergMon / battMon / cpuMon(安全/电池/CPU监控)
| 原始变量名 | 新变量名 |
|---|---|
| emergencyOn | emergMon.enabled |
| isemergency | emergMon.active |
| emergency_count | emergMon.counter |
| emergency_delay | emergMon.delay |
| showbattery | battMon.enabled |
| battary / battaryh | battMon.voltage / battMon.voltagePrev |
| battarypercent | battMon.percent |
| cpu / cpu0 | cpuMon.ticks / cpuMon.ticksPrev |

### irCalib(IrCalib, 红外标定状态)
| 原始变量名 | 新变量名 |
|---|---|
| rawf1 / rawf2 / rawl / rawr | irCalib.rawf1 / irCalib.rawf2 / irCalib.rawl / irCalib.rawr |
| rawf1h / rawf2h / rawlh / rawrh | irCalib.rawf1Prev / irCalib.rawf2Prev / irCalib.rawlPrev / irCalib.rawrPrev |
| rawf1avrgnear…rawravrgfar | irCalib.rawf1avrgnear…irCalib.rawravrgfar |
| biaodingmode | irCalib.calibMode |

### kalmanSt(KalmanState, 卡尔曼滤波) 与 motor(MotorVars/PidStruct, 电机)
> kalmanSt 为逐通道成员(f1/f2/l/r × value/cov/tmp/covtmp/k/Q/P);motor 侧分 MotorVars(测速)与 motorPidL/R(PidStruct 闭环)。
| 原始变量名 | 新变量名 |
|---|---|
| g / P / Q / kg / gtmp / covg / covgtmp | kalmanSt 各通道成员(Qf1/Pf1/kf1… 逐通道: 通道名+Q/P/k) |
| kpl / kil / kdl | motorPidL.kp / motorPidL.ki / motorPidL.kd |
| kpr / kir / kdr | motorPidR.kp / motorPidR.ki / motorPidR.kd |
| errl / errlh / errls | motorPidL.err / motorPidL.errPrev / motorPidL.errInteg |
| errr / errrh / errrs | motorPidR.err / motorPidR.errPrev / motorPidR.errInteg |
| lw / lwh | motor.wheelAngleL / motor.whAngPrevL |
| rw / rwh | motor.wheelAngleR / motor.whAngPrevR |

### taskSt(TaskState, 任务状态)
| 原始变量名 | 新变量名 |
|---|---|
| goaheadnum | taskSt.goaheadnum |
| backSearchFlag | taskSt.backSearch |
| searchOrSpurtFlag | taskSt.spurtFlag |
| goOrBackFlag | taskSt.goOrBack |

### searchSt(SearchState) / searchSm(SearchStateMachine) / spurtSpd(SpurtSpeedConfig)
| 原始变量名 | 新变量名 |
|---|---|
| lastSearchAction | searchSt.lastSearchAct |
| afterturnFlag | searchSt.afterTurnSide |
| goalKeepFlag | searchSt.goalCorrectDir |
| goaheadbugflag | searchSt.skipCoorUpdate |
| n(支路栈索引) | searchSm.crossIdx(mazeState.stackIdx) |
| l45TopSpeed / L45BottomSpeed | spurtSpd.l45TopSpeed / spurtSpd.l45BottomSpeed |
| objectGoToTopSpeed / ObjectGoTo_BottomSpeed | spurtSpd.objGoToTopSpd / spurtSpd.objGoToBtmSpd |
| lTopSpeed / LBottomSpeed | spurtSpd.lTopSpeed / spurtSpd.lBottomSpeed |
| l90TopSpeed / L90BottomSpeed | spurtSpd.l90TopSpeed / spurtSpd.l90BottomSpeed |
| backTopSpeed / BackBottomSpeed | spurtSpd.backTopSpeed / spurtSpd.backBottomSpeed |
| topSpeed / BottomSpeed | spurtSpd.topSpeed / spurtSpd.bottomSpeed |
| spurtAccRatio45 / SpurtDecRatio45 | spurtSpd.spurtAccRatio45 / spurtSpd.spurtDecRatio45 |
| spurtAccRatio90 / SpurtDecRatio90 | spurtSpd.spurtAccRatio90 / spurtSpd.spurtDecRatio90 |
| spurtAccRatio / SpurtDecRatio | spurtSpd.spurtAccRatio / spurtSpd.spurtDecRatio |

### 速度表(下划线 → 小驼峰, 表值红线不可改)
| 原始变量名 | 新变量名 |
|---|---|
| vouter_spurt / vinner_spurt | vouterSpurt / vinnerSpurt |
| gyro_l_spurt / gyro_r_spurt | gyroLSpurt / gyroRSpurt |
| vouter_search / vinner_search | vouterSearch / vinnerSearch |
| gyro_l_search / gyro_r_search | gyroLSearch / gyroRSearch |
| gyro_around / vl_around / vr_around | gyroAround / vlAround / vrAround |
| gyro_l_point / gyro_r_point | gyroLPoint / gyroRPoint |
| vouter_point / vinner_point | vouterPoint / vinnerPoint |
| vouter_t90_low / vinner_t90_low | vouterT90Low / vinnerT90Low |
| gyro_lt90_low / gyro_rt90_low | gyroLt90Low / gyroRt90Low |
| vouter_v90_low / vinner_v90_low | vouterV90Low / vinnerV90Low |
| gyro_lv90_low / gyro_rv90_low | gyroLv90Low / gyroRv90Low |
| vouter_t45_low / vinner_t45_low | vouterT45Low / vinnerT45Low |
| gyro_lt45_low / gyro_rt45_low | gyroLt45Low / gyroRt45Low |
| vouter_v45_low / vinner_v45_low | vouterV45Low / vinnerV45Low |
| gyro_lv45_low / gyro_rv45_low | gyroLv45Low / gyroRv45Low |
| vouter_t135_low / vinner_t135_low | vouterT135Low / vinnerT135Low |
| gyro_lt135_low / gyro_rt135_low | gyroLt135Low / gyroRt135Low |
| vouter_v135_low / vinner_v135_low | vouterV135Low / vinnerV135Low |
| gyro_lv135_low / gyro_rv135_low | gyroLv135Low / gyroRv135Low |
| vouter_180_low / vinner_180_low | vouter180Low / vinner180Low |
| gyro_l180_low / gyro_r180_low | gyroL180Low / gyroR180Low |

## 二、简单改名(非结构体)

| 原始变量名 | 新变量名 | 说明 |
|---|---|---|
| ts | taskSt | TaskState 实例 |
| mc | motionCtrl | MotionCtrl 实例 |
| odom | odomStr | OdomStruct 实例 |
| spurt | spurtCfg | SpurtConfig 实例 |
| save_num | saveNum | Flash 存储上限(u16 const) |
| per_timer | perTimer | Flash 存储间隔(u8 const) |
| blocknum | straightCellCount | 连续直线格数(u8) |
| lastside | lastWallSide | 上一次贴墙侧(u8, isr.c static) |
| turnDelay | turnDelay | 转弯延时(类型 int→float) |
| motor | motor | MotorVars 实例(保留原名, 曾试改 motorVars 已回退) |
| fitK / fitD | fitK / fitD | irs.c 拟合系数(保留原名) |

## 三、已删除的死全局

wtf、iii、testi、test、tim2Cnt、testTimeFlag、ledcontrol、turndir、blocknum45、anglei、angleterm、queuei、queuenum、infrskipround、saveDataTemp、gdspeedl、gdspeedr、kiturn、GOSTRAIGHT_kp 等(已封装/删除, 详见 git 历史)

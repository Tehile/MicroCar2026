#ifndef V5_SEARCH_GUARD_H
#define V5_SEARCH_GUARD_H
#include "sys.h"

/* Competition search only. Task 46 is a zero-speed hold, never a path action. */
#define V5_FRONT_RECHECK_TASK 46
#define V5_FRONT_TRIP 10
#define V5_FRONT_CLEAR 30
#define V5_RECHECK_MIN_TICKS 50
#define V5_RECHECK_MAX_TICKS 500
#define V5_RECHECK_STABLE_SAMPLES 20
#define V5_DEADEND_CORRECTION_DIVISOR 3
#define V5_DIAG_MAGIC 0x56354431UL
#define V5_DIAG_PENDING 0
#define V5_DIAG_RECOVERED 1
#define V5_DIAG_PERSISTENT 2
#define V5_DIAG_ABORTED 3

typedef struct {
    u32 sample_seq;
    long track, absolute_track;
    long front1, front2, front_mean, left, right;
    int filtered_adc_f1, filtered_adc_f2, filtered_adc_l, filtered_adc_r;
    long target_l, target_r;
    s16 speed_l, speed_r, yaw;
    u8 task, last_action, x, y, direction, left_wall, right_wall, front_wall;
    u16 front_adc1, front_adc2;
} V5_STOP_SNAPSHOT;

typedef struct {
    u32 magic, sample_seq;
    u16 events, recovered, persistent, result;
    u8 source, active;
    u16 hold_ticks;
    u16 front_adc1, front_adc2;
    u8 deadend_limit_active, reserved;
    V5_STOP_SNAPSHOT first, last, recheck;
} V5_STOP_DIAGNOSTICS;

/* 64 fewer flight records reserve this 256-byte block and 256 stack bytes. */
typedef char v5_diag_fits_reserved_ram[(sizeof(V5_STOP_DIAGNOSTICS)<=256)?1:-1];
extern volatile V5_STOP_DIAGNOSTICS v5_diag;
#endif


#include <stdlib.h>
#include <string.h>
#include <setjmp.h>
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned long u32;
typedef short s16;
typedef struct {char cX,cY;} MAZECOOR;
#define API __declspec(dllexport)
#ifndef V5_SEARCH_GUARD_H
#define V5_SEARCH_GUARD_H



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


typedef char v5_diag_fits_reserved_ram[(sizeof(V5_STOP_DIAGNOSTICS)<=256)?1:-1];
extern volatile V5_STOP_DIAGNOSTICS v5_diag;
#endif

#define MAXPWM	1500
#define ACCV0 10
#define SPEED_HAHA 350
#define NULL 0
#define GOSTRAIGHT 1
#define TURN 2
#define TURNAROUND 4
#define STOP 5
#define SUPERSTRAIGHT 12
#define INFRSTRAIGHT 13
#define TURNLEFTFAST 14
#define TURNRIGHTFAST 15
#define EMERGENCY 16
#define STRAIGHT45 17
#define FASTSTRAIGHT 18
#define BEFORE90TURN 27
#define BEFORE180TURN 28
#define AFTER180TURN 29
#define SMARTSTRAIGHT 30
#define CHANGESPEED 31
#define SMARTSTRAIGHTFAST 32  
#define STRAIGHTEYECLOSECOORADJ 33
#define STRAIGHTPOLE 34
#define ACC 35
#define SLOWDOWN 36
#define SPEEDUP 37
#define KEEPWALLDISTANCE 38
#define POINTTURN90 39
#define EDGESTRAIGHT 40
#define FORWARDIRTEST 41
#define SPURT90BLIND 42
#define SPURT90IR 43
#define SMARTSTRAIGHTSPURT 44
#define SMARTSTRAIGHTSPURTL 45
volatile V5_STOP_DIAGNOSTICS v5_diag;
MAZECOOR GmcMouse;
long track,absolutetrack,basetrack,disf1,disf2,disf,disl,disr,setvl,setvr;
long e,eh;
int rawf1,rawf2,rawl,rawr,speedl,speedr,gangle;
u8 task,LastSearchAction,GucMouseDir,lefthaswall,righthaswall,forwardhaswall,sleepmode,flashwrite;
u16 isemergency,speed_turn=30,turnaround_tick,tick;
const int dis2wall=40;
int mode,clear_after,settle_after,sim_steps,printing,print_calls,print_moving,coast_mm;
int irq_mask,first_zero_tick=-1;
static jmp_buf escape;
u32 __get_PRIMASK(void){return irq_mask;}
void __disable_irq(void){irq_mask=1;}
void __enable_irq(void){irq_mask=0;}
void v5HostTick(void);
int printf(const char *format,...){
    int i;(void)format;print_calls++;
    if(speedl<-1 || speedl>1 || speedr<-1 || speedr>1)print_moving++;
    printing=1;for(i=0;i<2;i++)v5HostTick();printing=0;
    return 0;
}
void controller(void){switch(task){
#ifndef MOUSE_REMOTE
    case V5_FRONT_RECHECK_TASK:
        if(v5_diag.hold_ticks<65535)v5_diag.hold_ticks++;
        setvl=0;setvr=0;
        break;
#endif
	case BEFORE180TURN:
 
		if(righthaswall==1 && lefthaswall==0 && disf>60) 
		{
			setvl=speed_turn+(disr-dis2wall);
			setvr=speed_turn-(disr-dis2wall);	
		}
		else if(righthaswall==0 && lefthaswall==1 && disf>60)
		{
			setvl=speed_turn-(disl-dis2wall);
			setvr=speed_turn+(disl-dis2wall);	
		}
		else if(righthaswall==1 && lefthaswall==1 && disf>60)
		{
			setvl=speed_turn+(disr-disl);
			setvr=speed_turn-(disr-disl);	
		}
		else
		{
			setvl=(disf-20)*0.5+(disf1-20)*1;			 
			setvr=(disf-20)*0.5+(disf2-20)*1;
			if(setvl>speed_turn) setvl=speed_turn;
			if(setvl<-speed_turn) setvl=-speed_turn;
			if(setvr>speed_turn) setvr=speed_turn;
			if(setvr<-speed_turn) setvr=-speed_turn;
			if( (disf-20)<-20 && abs(disf1-disf2)<100 )	 
			{
				setvl=-speed_turn;
				setvr=-speed_turn;
			}
			if( (disf-20)>20 && abs(disf1-disf2)<100 )
			{
				setvl=speed_turn;
				setvr=speed_turn;
			}
			if(speedl<2&&speedr<2&&speedl>-2&&speedr>-2)tick++;
			else tick=0;
			turnaround_tick++;
			if(turnaround_tick>100)tick=100; 
		}
#ifndef MOUSE_REMOTE
        if(v5_diag.deadend_limit_active && disf>60 &&
           (lefthaswall || righthaswall))
        {
            long correction=(setvl-setvr)/2;
            long bound=speed_turn/V5_DEADEND_CORRECTION_DIVISOR;
            if(correction>bound)correction=bound;
            if(correction<-bound)correction=-bound;
            setvl=speed_turn+correction;setvr=speed_turn-correction;
        }
#endif
		break;
		
 
	
default:break;
}}
void v5HostTick(void){
    int clear;
    if(++sim_steps>1500)longjmp(escape,99);
    if(irq_mask)longjmp(escape,98);
    controller();
    if(setvl==0 && setvr==0 && first_zero_tick<0)first_zero_tick=sim_steps;
    if(task==V5_FRONT_RECHECK_TASK && (setvl!=0 || setvr!=0))longjmp(escape,97);
    speedl=speedr=(mode==3 || sim_steps<settle_after)?8:0;
    if(speedl && sim_steps%3==0){track++;absolutetrack++;coast_mm++;}
    if(mode!=2){
        clear=(sim_steps>=clear_after && mode!=1);
        if(mode==4)clear=(sim_steps%25<19);
        if(mode==10 && printing)clear=0;
        disf1=disf2=clear?249:3;disf=(disf1+disf2)/2;
        if(mode==5){disf1=disf2=disf=1000;}
        if(mode==6){disf1=disf2=249;disf=0;}
        rawf1=rawf2=clear?80:3000;
        v5_diag.front_adc1=(u16)rawf1;v5_diag.front_adc2=(u16)rawf2;
        v5_diag.sample_seq++;
    }
    if(sim_steps==5){
        if(mode==7){isemergency=1;task=0;}
        if(mode==8)sleepmode=1;
        if(mode==9)task=0;
    }
}
static void v5CaptureStop(volatile V5_STOP_SNAPSHOT *s)
{
    s->sample_seq=v5_diag.sample_seq;
    s->track=track;s->absolute_track=absolutetrack;
    s->front1=disf1;s->front2=disf2;s->front_mean=disf;
    s->left=disl;s->right=disr;
    s->filtered_adc_f1=rawf1;s->filtered_adc_f2=rawf2;
    s->filtered_adc_l=rawl;s->filtered_adc_r=rawr;
    s->target_l=setvl;s->target_r=setvr;
    s->speed_l=speedl;s->speed_r=speedr;s->yaw=gangle;
    s->task=task;s->last_action=LastSearchAction;
    s->x=GmcMouse.cX;s->y=GmcMouse.cY;s->direction=GucMouseDir;
    s->left_wall=lefthaswall;s->right_wall=righthaswall;s->front_wall=forwardhaswall;
    s->front_adc1=v5_diag.front_adc1;s->front_adc2=v5_diag.front_adc2;
}
static void v5PrintSnapshot(const char *label,volatile V5_STOP_SNAPSHOT *s)
{
    printf("V5_%s seq=%lu task=%u last=%u xy=%u,%u dir=%u walls=%u,%u,%u\r\n",
           label,s->sample_seq,s->task,s->last_action,s->x,s->y,s->direction,
           s->left_wall,s->right_wall,s->front_wall);
    printf("V5_%s_IR adc=%u,%u filtered=%d,%d side_adc=%d,%d dist=%ld,%ld mean=%ld side=%ld,%ld\r\n",
           label,s->front_adc1,s->front_adc2,s->filtered_adc_f1,s->filtered_adc_f2,
           s->filtered_adc_l,s->filtered_adc_r,s->front1,s->front2,s->front_mean,s->left,s->right);
    printf("V5_%s_MOTION track=%ld absolute=%ld target=%ld,%ld speed=%d,%d yaw=%d\r\n",
           label,s->track,s->absolute_track,s->target_l,s->target_r,s->speed_l,s->speed_r,s->yaw);
}
static void v5PrintStop(void)
{
    printf("V5_STOP event=%u source=%u result=%u recovered=%u persistent=%u\r\n",
           v5_diag.events,v5_diag.source,v5_diag.result,v5_diag.recovered,v5_diag.persistent);
    v5PrintSnapshot("TRIGGER",&v5_diag.last);
    v5PrintSnapshot("RECHECK",&v5_diag.recheck);
}
static u8 v5FrontClear(void)
{
    
    return (disf1>=V5_FRONT_CLEAR && disf1<=249 &&
            disf2>=V5_FRONT_CLEAR && disf2<=249 &&
            disf==(disf1+disf2)/2);
}
static u8 v5FrontRecheck(u8 source)
{
    u32 mask,seen;
    u16 stable=0,stopped=0;
    u8 previous_task,previous_flash,printed=0;

    mask=__get_PRIMASK();__disable_irq();
    if(disf>=V5_FRONT_TRIP)
    {
        if(!mask)__enable_irq();
        return 1;
    }
    previous_task=task;previous_flash=flashwrite;
    v5CaptureStop(&v5_diag.last);
    if(v5_diag.magic!=V5_DIAG_MAGIC)
    {
        v5_diag.magic=V5_DIAG_MAGIC;v5_diag.events=0;
        v5_diag.recovered=0;v5_diag.persistent=0;
    }
    if(v5_diag.events==0)v5_diag.first=v5_diag.last;
    if(v5_diag.events<65535)v5_diag.events++;
    v5_diag.source=source;v5_diag.result=V5_DIAG_PENDING;
    v5_diag.active=1;v5_diag.hold_ticks=0;
    task=V5_FRONT_RECHECK_TASK;setvl=0;setvr=0;flashwrite=0;
    seen=v5_diag.sample_seq;
    if(!mask)__enable_irq();

    
    while(v5_diag.hold_ticks<V5_RECHECK_MAX_TICKS)
    {
        if(sleepmode || isemergency || task!=V5_FRONT_RECHECK_TASK)break;
        v5HostTick();if(seen==v5_diag.sample_seq)continue;
        mask=__get_PRIMASK();__disable_irq();
        seen=v5_diag.sample_seq;
        if(speedl>-2 && speedl<2 && speedr>-2 && speedr<2)
        {
            if(stopped<V5_RECHECK_STABLE_SAMPLES)stopped++;
            if(v5FrontClear())stable++;else stable=0;
        }
        else {stopped=0;stable=0;}
        if(stopped>=V5_RECHECK_STABLE_SAMPLES && !printed)
        {
            v5CaptureStop(&v5_diag.recheck);
            if(!mask)__enable_irq();
            
            v5PrintStop();printed=1;stable=0;
            seen=v5_diag.sample_seq;
            continue;
        }
        if(printed && stable>=V5_RECHECK_STABLE_SAMPLES &&
           v5_diag.hold_ticks>=V5_RECHECK_MIN_TICKS &&
           !sleepmode && !isemergency && task==V5_FRONT_RECHECK_TASK)
        {
            v5CaptureStop(&v5_diag.recheck);
            v5_diag.result=V5_DIAG_RECOVERED;
            if(v5_diag.recovered<65535)v5_diag.recovered++;
            v5_diag.active=0;e=0;eh=0;
            flashwrite=previous_flash;task=previous_task;
            if(!mask)__enable_irq();
            return 1;
        }
        if(!mask)__enable_irq();
    }
    mask=__get_PRIMASK();__disable_irq();
    v5CaptureStop(&v5_diag.recheck);
    v5_diag.result=(sleepmode || isemergency || task!=V5_FRONT_RECHECK_TASK)?
                   V5_DIAG_ABORTED:V5_DIAG_PERSISTENT;
    if(v5_diag.persistent<65535)v5_diag.persistent++;
    v5_diag.active=0;task=NULL;setvl=0;setvr=0;flashwrite=0;
    if(!mask)__enable_irq();
    v5PrintStop();
    return 0;
}
API int run(int m,int after,int settle,int source,int previous,int front1,int front2){
    int error=setjmp(escape);if(error)return error;
    memset((void*)&v5_diag,0,sizeof(v5_diag));
    mode=m;clear_after=after;settle_after=settle;sim_steps=printing=print_calls=print_moving=coast_mm=irq_mask=0;
    first_zero_tick=-1;track=50;absolutetrack=150;basetrack=100;
    task=previous;LastSearchAction=2;GmcMouse.cX=5;GmcMouse.cY=6;GucMouseDir=3;
    disf1=front1;disf2=front2;disf=(front1+front2)/2;disl=40;disr=200;
    lefthaswall=1;righthaswall=0;forwardhaswall=1;gangle=-3;
    rawf1=3111;rawf2=3222;rawl=444;rawr=555;
    v5_diag.front_adc1=3333;v5_diag.front_adc2=3444;
    setvl=79;setvr=81;speedl=speedr=80;flashwrite=1;sleepmode=0;isemergency=0;
    v5_diag.sample_seq=17;
    return v5FrontRecheck((u8)source);
}
API long value(int i){switch(i){
case 0:return v5_diag.events;case 1:return v5_diag.recovered;case 2:return v5_diag.persistent;
case 3:return v5_diag.result;case 4:return task;case 5:return v5_diag.active;case 6:return flashwrite;
case 7:return track;case 8:return absolutetrack;case 9:return basetrack;case 10:return first_zero_tick;
case 11:return sim_steps;case 12:return v5_diag.hold_ticks;case 13:return v5_diag.last.front1;
case 14:return v5_diag.last.front2;case 15:return v5_diag.last.front_mean;
case 16:return v5_diag.last.filtered_adc_f1;case 17:return v5_diag.last.front_adc1;
case 18:return v5_diag.last.target_l;case 19:return v5_diag.last.target_r;case 20:return v5_diag.last.task;
case 21:return v5_diag.last.track;case 22:return v5_diag.last.absolute_track;case 23:return v5_diag.last.sample_seq;
case 24:return GmcMouse.cX;case 25:return GmcMouse.cY;case 26:return GucMouseDir;case 27:return LastSearchAction;
case 28:return v5_diag.recheck.sample_seq;case 29:return setvl;case 30:return setvr;case 31:return print_calls;
case 32:return print_moving;case 33:return coast_mm;case 34:return v5_diag.source;case 35:return irq_mask;
case 36:return v5_diag.first.track;case 37:return v5_diag.last.yaw;case 38:return sizeof(V5_STOP_DIAGNOSTICS);
}return -999;}
API int again(void){
    int error=setjmp(escape);if(error)return error;
    sim_steps=0;mode=0;clear_after=5;settle_after=10;task=SMARTSTRAIGHT;flashwrite=1;
    track=75;absolutetrack=175;setvl=setvr=speedl=speedr=30;disf=disf1=disf2=3;
    return v5FrontRecheck(1);
}
API long limited(int left,int right,int dl,int dr,int df,int active,int speed,int wheel){
    lefthaswall=left;righthaswall=right;disl=dl;disr=dr;
    disf=disf1=disf2=df;v5_diag.deadend_limit_active=active;speed_turn=speed;
    task=BEFORE180TURN;speedl=speedr=0;tick=turnaround_tick=0;
    controller();return wheel?setvr:setvl;
}

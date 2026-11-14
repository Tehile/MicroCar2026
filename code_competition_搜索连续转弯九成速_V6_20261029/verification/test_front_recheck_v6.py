"""Extract actual V5 C helpers and IRQ cases; inject fresh sensor/wheel samples."""
from pathlib import Path
import ctypes as C,json,os,re,subprocess
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'verification/continuous_turn_v6_20261029/guard_host'
OUT.mkdir(parents=True,exist_ok=True)
def clean(p):return re.sub(r'/\*.*?\*/|//[^\n]*','',p.read_bytes().decode('latin1').replace('\r\n','\n'),flags=re.S)
def function(s,name):
    m=re.search(r'^(?:static\s+)?(?:void|int|u8|u16)\s+'+name+r'\s*\([^;]*?\)\s*\{',s,re.M)
    assert m,name
    i=s.index('{',m.start());j=i+1;depth=1
    while depth:
        if s[j]=='{':depth+=1
        if s[j]=='}':depth-=1
        j+=1
    return s[m.start():j]
u=clean(ROOT/'UTILITY/utility.c');timer=clean(ROOT/'HARDWARE/TIMER/timer.c')
header=clean(ROOT/'UTILITY/search_guard_v5.h').replace('#include "sys.h"','')
defs='\n'.join(l for l in clean(ROOT/'HARDWARE/TIMER/timer.h').splitlines() if re.match(r'#define\s+\w+\s+\d+\s*$',l))
irq=timer[timer.index('#ifndef MOUSE_REMOTE\n    case V5_FRONT_RECHECK_TASK:'):timer.index('case FORWARDIRTEST:')]
helpers='\n'.join(function(u,n) for n in ('v5CaptureStop','v5PrintSnapshot','v5PrintStop','v5FrontClear','v5FrontRecheck'))
helpers=helpers.replace('if(seen==v5_diag.sample_seq)continue;','v5HostTick();if(seen==v5_diag.sample_seq)continue;')
prefix=r'''
#include <stdlib.h>
#include <string.h>
#include <setjmp.h>
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned long u32;
typedef short s16;
typedef struct {char cX,cY;} MAZECOOR;
#define API __declspec(dllexport)
'''+header+'\n'+defs+r'''
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
'''+irq+r'''
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
'''
exports=r'''
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
'''
source=prefix+helpers+exports
(OUT/'host.c').write_text(source,encoding='ascii')
msvc=Path(r'D:\vs\VC\Tools\MSVC\14.41.34120');sdk=Path(r'D:\Windows Kits\10');ver='10.0.22621.0'
env=os.environ.copy();env['PATH']=str(msvc/'bin/Hostx64/x64')+';'+env['PATH']
cmd=[str(msvc/'bin/Hostx64/x64/cl.exe'),'/nologo','/LD','/Od','/W3','/I'+str(msvc/'include'),'/I'+str(sdk/'Include'/ver/'ucrt'),'host.c','/Fohost.obj','/link','/LIBPATH:'+str(msvc/'lib/x64'),'/LIBPATH:'+str(sdk/'Lib'/ver/'ucrt/x64'),'/LIBPATH:'+str(sdk/'Lib'/ver/'um/x64'),'/OUT:host.dll','/IMPLIB:host.lib']
p=subprocess.run(cmd,cwd=OUT,env=env,capture_output=True);(OUT/'compile.log').write_bytes(p.stdout+p.stderr)
if p.returncode:raise RuntimeError((p.stdout+p.stderr).decode('mbcs',errors='replace'))
dll=C.CDLL(str(OUT/'host.dll'));dll.run.argtypes=[C.c_int]*7;dll.value.restype=C.c_long;dll.limited.argtypes=[C.c_int]*8;dll.limited.restype=C.c_long
def val(i):return dll.value(i)
checks=0;traces=[]
for source_id in (1,2):
    for previous in (30,31,32,33,34):
        for delay in (1,5,25,75,150,300):
            for settle in (1,10,50):
                result=dll.run(0,delay,settle,source_id,previous,2,4)
                assert result==1,(delay,settle,result)
                assert [val(i) for i in (0,1,2,3,4,5,6)]==[1,1,0,1,previous,0,1]
                assert val(7)==50+val(33) and val(8)==150+val(33) and val(9)==100
                assert [val(i) for i in (13,14,15,16,17,18,19,20,21,22,23)]==[2,4,3,3111,3333,79,81,previous,50,150,17]
                assert [val(i) for i in (24,25,26,27)]==[5,6,3,2]
                assert val(28)>val(23) and val(10)==1 and val(12)>=50
                assert val(32)==0 and val(35)==0 and val(34)==source_id and val(37)==-3
                checks+=1
for m in range(1,10):
    result=dll.run(m,1,10,2,30,2,4)
    assert result==0,(m,result)
    assert [val(i) for i in (0,1,2,4,5,6,29,30)]==[1,0,1,0,0,0,0,0]
    assert val(3)==(3 if m in (7,8,9) else 2)
    assert val(11)<1000 and val(35)==0
    assert [val(i) for i in (24,25,26,27)]==[5,6,3,2]
    traces.append({'case':m,'result':val(3),'ticks':val(12),'trigger_mean':val(15)})
    checks+=1
# A rebound during UART output must receive a new clean confirmation window.
assert dll.run(10,1,10,2,30,2,4)==1 and val(12)>=60;checks+=1
# A single close channel with a far second channel is not the inherited trip.
for f1,f2 in ((0,249),(249,0),(10,10),(30,30),(249,249)):
    assert dll.run(0,1,10,2,30,f1,f2)==1
    assert val(0)==0 and val(4)==30 and val(7)==50 and val(31)==0;checks+=1
# First capture survives a later event; last capture follows the later event.
assert dll.run(0,1,10,2,30,2,4)==1 and dll.again()==1
assert val(0)==val(1)==2 and val(36)==50 and val(21)==75 and val(34)==1;checks+=1
assert val(38)==256;checks+=1
# Sweep one-wall, two-wall and no-wall cases; only the side-wall approach clips.
for left,right in ((0,0),(0,1),(1,0),(1,1)):
    for dl,dr in ((5,129),(129,5),(40,40),(90,40),(40,90)):
        for df in (20,40,60,61,100,200):
            before=[dll.limited(left,right,dl,dr,df,0,30,w) for w in (0,1)]
            after=[dll.limited(left,right,dl,dr,df,1,30,w) for w in (0,1)]
            if df>60 and (left or right):
                assert all(20<=v<=40 for v in after),(left,right,dl,dr,df,after)
                assert sum(after)==60
            else:assert after==before
            checks+=1
report={'status':'PASS','checks':checks,'traces':traces,'method':'Actual extracted production capture/recheck/print functions and hold/BEFORE180TURN IRQ cases. Only host IRQ delivery, UART duration, sensor readings and wheel feedback are synthetic. Includes transient/permanent trip, repeated triggers, stale samples, moving wheels, invalid lookup values, mismatched mean, emergency, sleep, task cancellation, UART rebound, coordinate/encoder continuity and local correction range. Not a physical dynamics or optical accuracy test.'}
(OUT.parent/'guard_test_results.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps({'status':'PASS','checks':checks,'snapshot_bytes':val(38)}))

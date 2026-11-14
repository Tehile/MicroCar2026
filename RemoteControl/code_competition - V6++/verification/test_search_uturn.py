from pathlib import Path
import ctypes as C
import random
import re
import subprocess
import os
import json

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'verification'
OUT.mkdir(exist_ok=True)
original=ROOT.parent/'code_competition'

def clean(path):
    s=path.read_bytes().decode('latin1').replace('\r\n','\n')
    return re.sub(r'/\*.*?\*/|//[^\n]*','',s,flags=re.S)

def function(s,name):
    m=re.search(r'^(?:static\s+)?(?:void|int|u8|u16)\s+'+name+r'\s*\([^;]*?\)\s*\{',s,re.M)
    if not m: raise ValueError(name)
    b=s.index('{',m.start());depth=1;e=b+1
    while depth:
        if s[e]=='{':depth+=1
        elif s[e]=='}':depth-=1
        e+=1
    return s[m.start():e]

source=clean(ROOT/'UTILITY/utility.c')
base=clean(original/'UTILITY/utility.c')
defs='\n'.join(x for x in clean(ROOT/'UTILITY/utility.h').splitlines() if re.match(r'#define\s',x) and not '__UTILITY' in x)
state=source[source.index('#define spurt_plan_start_dir'):source.index('extern const u8 trackcoor;')]
common=r'''
#include <string.h>
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned char uchar;
typedef struct {char cx,cy,action;} FASTWAYACTION;
typedef struct {char cX,cY;} MAZECOOR;
#define MOUSE_REMOTE
#undef NULL
#define NULL 0
#define SMARTSTRAIGHTFAST 32
#define STRAIGHTEYECLOSECOORADJ 33
#define SMARTSTRAIGHTSPURTL 45
#define SMARTSTRAIGHT 30
#define BEFORE90TURN 27
#define MAZETYPE 16
u8 actionsequence[200],action_correct_sequence[200];
FASTWAYACTION FastwayAction[256];
u8 FastwayCount,GucMouseDir,sleepmode,task,goaheadbugflag;
u8 sidewall_threshold,LastSearchAction,turn_delay,keep_dis,flashwrite;
u8 lefthaswall,righthaswall,lefthaswallh,righthaswallh,forwardhaswall;
u16 isemergency,speed_ss,speed_turn,speed_fastss;
long disf,disl,disr,dislh,dislhh,disrh,disrhh;
volatile long track,absolutetrack;
long basetrack,e,eh,esum;
int turno,turnrighto,turnlefto,setvl,setvr;
u8 dis2wall=40;
const u8 trackcoor=127;
MAZECOOR GmcMouse;
double kpl,kil,kdl,kpr,kir,kdr,turn_kp,turn_kd;
int call_count,turn_codes[20],delay_dist[20],poll_count,abort_at,abort_flag;
int active_parameter_errors,edge_at;
void beforemouseturn(u8 p) {turn_codes[call_count++]=1000+p;}
void ledoff(int n) {(void)n;}
void ledon(int n) {(void)n;}
void delay_ms(int n) {(void)n;}
void settrack(int t);
void taskturn(u8 code) {
    turn_codes[call_count]=code;
    delay_dist[call_count]=track;
    call_count++;
    if(speed_ss!=80 || speed_turn!=30 || turn_delay!=9 ||
       kpl!=13 || kil!=0.74 || kdl!=0.9 || turn_kp!=4 || turn_kd!=4)
        active_parameter_errors++;
    settrack(0);
}
u8 LP_AbortRequested(void) {
    poll_count++;
    if(abort_at && poll_count>=abort_at) abort_flag=1;
    if(!abort_flag) {track++;absolutetrack++;}
    if(edge_at>0 && poll_count>=edge_at) lefthaswallh=righthaswallh=1;
    return (u8)abort_flag;
}
void LP_AbortRequest(void) {abort_flag=1;}
void mouseTurnright(void);
void mouseTurnleft(void);
void parameter_search(void);
'''
names=['settrack','before90turn','mouseTurnright','mouseTurnleft','spurtSearchRunning',
       'spurtSearchWaitTrack','spurtIsSearchUturn','spurtSearchPose','spurtSourceForAction',
       'spurtSearchParameters','spurtSearchUturn','mouseturn','getsum','actiongenerate']
exports=r'''
#define API __declspec(dllexport)
API void load_case(FASTWAYACTION *a,int count,int dir) {
    memset(FastwayAction,0,sizeof(FastwayAction));
    memcpy(FastwayAction,a,count*sizeof(FASTWAYACTION));
    FastwayCount=(u8)count;GucMouseDir=(u8)dir;
    sleepmode=0;isemergency=0;task=0;
    kpl=11.1;kil=.61;kdl=.81;kpr=12.2;kir=.62;kdr=.82;
    turn_kp=5;turn_kd=3;speed_ss=95;speed_turn=70;speed_fastss=123;
    turn_delay=2;keep_dis=13;sidewall_threshold=110;
    LastSearchAction=TurnLeft;goaheadbugflag=1;
    lefthaswall=righthaswall=forwardhaswall=0;
    lefthaswallh=righthaswallh=1;
    disf=disl=disr=dislh=dislhh=disrh=disrhh=200;
    track=absolutetrack=0;basetrack=0;
    call_count=0;poll_count=0;abort_at=0;abort_flag=0;
    active_parameter_errors=0;edge_at=0;
    GmcMouse.cX=12;GmcMouse.cY=13;
}
API void generate(int mode) {actiongenerate((u8)mode);}
API int seq(int i) {return actionsequence[i];}
API int src(int i) {return spurtSourceForAction((u16)i);}
API void baseline(int mode) {baseline_actiongenerate((u8)mode);}
API void set_stale(int index,int value) {FastwayAction[index].action=(char)value;}
API void set_abort(int count) {abort_at=count;}
API void set_collision(int right) {if(right) disr=40;else disl=40;}
API void set_edge(int at) {edge_at=at;lefthaswallh=righthaswallh=0;}
API void set_front(int distance) {disf=distance;}
API void set_outer_wall(int right,int distance) {
    if(right) {lefthaswall=1;disl=dislh=dislhh=distance;}
    else {righthaswall=1;disr=disrh=disrhh=distance;}
}
API int run(int index) {
    spurt_exec_index=(u8)index;
    mouseturn(actionsequence[index]);
    spurt_exec_index=200;
    return spurt_search_failed;
}
API int value(int n) {
    switch(n) {
    case 0:return GucMouseDir;case 1:return GmcMouse.cX;case 2:return GmcMouse.cY;
    case 3:return call_count;case 4:return sleepmode;case 5:return abort_flag;
    case 6:return active_parameter_errors;case 7:return task;
    case 8:return speed_ss;case 9:return speed_turn;case 10:return speed_fastss;
    case 11:return turn_delay;case 12:return keep_dis;case 13:return sidewall_threshold;
    case 14:return LastSearchAction;case 15:return goaheadbugflag;
    case 16:return spurt_search_active;case 17:return spurt_plan_start_dir;
    case 18:return poll_count;case 19:return (int)track;
    default:return -999;
    }
}
API double gain(int n) {
    switch(n) {
    case 0:return kpl;case 1:return kil;case 2:return kdl;case 3:return kpr;
    case 4:return kir;case 5:return kdr;case 6:return turn_kp;default:return turn_kd;
    }
}
API int code(int n) {return turn_codes[n];}
API int delay(int n) {return delay_dist[n];}
'''
program=common+'\n'+defs+'\n'+state+'\n'+function(clean(ROOT/'USER/user.c'),'parameter_search')+'\n'
program+='\n\n'.join(function(source,n) for n in names)
program+='\n'+function(base,'actiongenerate').replace('void actiongenerate','void baseline_actiongenerate')+'\n'+exports
(OUT/'host_test.c').write_text(program,encoding='ascii')
msvc=Path(r'D:\vs\VC\Tools\MSVC\14.41.34120')
sdk=Path(r'D:\Windows Kits\10')
version='10.0.22621.0'
env=os.environ.copy();env['PATH']=str(msvc/'bin/Hostx64/x64')+';'+env['PATH']
cmd=[str(msvc/'bin/Hostx64/x64/cl.exe'),'/nologo','/LD','/Od','/W3',
     '/I'+str(msvc/'include'),'/I'+str(sdk/'Include'/version/'ucrt'),
     str(OUT/'host_test.c'),'/Fo'+str(OUT/'host_test.obj'),'/link',
     '/LIBPATH:'+str(msvc/'lib/x64'),'/LIBPATH:'+str(sdk/'Lib'/version/'ucrt/x64'),
     '/LIBPATH:'+str(sdk/'Lib'/version/'um/x64'),'/OUT:'+str(OUT/'host_test.dll'),
     '/IMPLIB:'+str(OUT/'host_test.lib')]
p=subprocess.run(cmd,env=env,capture_output=True,cwd=OUT)
(OUT/'host_compile.log').write_bytes(p.stdout+p.stderr)
if p.returncode:
    print(ascii((p.stdout+p.stderr).decode('utf-8',errors='replace')));raise SystemExit(p.returncode)

class FA(C.Structure):
    _fields_=[('cx',C.c_byte),('cy',C.c_byte),('action',C.c_byte)]
dll=C.CDLL(str(OUT/'host_test.dll'))
dll.load_case.argtypes=[C.POINTER(FA),C.c_int,C.c_int]
dll.gain.restype=C.c_double

def load(actions,dir=0,coords=None):
    coords=coords or [(0,0)]*len(actions)
    arr=(FA*len(actions))(*(FA(x,y,a) for a,(x,y) in zip(actions,coords)))
    dll.load_case(arr,len(arr),dir)

def sequence():
    a=[dll.seq(i) for i in range(200)]
    return a[:a.index(0)] if 0 in a else a

def route(steps,initial=0):
    x=y=6;coords=[];raw=[];last=initial*2;previous=None
    for way in steps:
        coords.append((x,y));world=way*2
        if previous is None: heading=world
        elif way==previous:heading=world
        else:heading=(previous*2+(1 if (way-previous)%4==1 else -1))%8
        raw.append((heading-last)%8);last=heading;previous=way
        x+=[0,1,0,-1][way];y+=[1,0,-1,0][way]
    coords.append((x,y));raw.append((steps[-1]*2-last)%8)
    return raw,coords

results=[];checked=0
random.seed(20261022)
for test in range(2000):
    initial=random.randrange(4);way=initial;steps=[]
    for n in range(random.randrange(3,100)):
        steps.append(way);way=(way+random.choice([0,0,1,3]))%4
    raw,coords=route(steps,initial)
    for mode in (61,62,63):
        load(raw,initial,coords);dll.generate(mode);new=sequence()
        for i,token in enumerate(new):
            if token in (13,14,33,34,53,54):
                start=dll.src(i)
                assert 0<start and start+2<len(raw),(test,i,start)
                assert raw[start:start+3] in ([1,2,1],[7,6,7])
        dll.baseline(mode);old=sequence()
        assert new==old,(test,mode,raw,new,old)
        checked+=1
results.append({'case':'actual C generator vs original, random grid routes / 3 modes','passed':checked})

for count in (0,1):
    load([0]*count);dll.generate(61);assert sequence()==[]
for raw in ([0,1],[0,1,2],[0,7],[0,7,6]):
    load(raw);dll.set_stale(len(raw),1 if raw[1]==1 else 7)
    dll.generate(61)
    assert not any(x in (13,14) for x in sequence())
results.append({'case':'empty, one node and truncated U with stale trailing memory','passed':6})

def checkpoint():
    return [dll.value(i) for i in range(8,16)]+[dll.gain(i) for i in range(8)]

motion_cases=0
for initial in range(4):
    for right in (True,False):
        delta=1 if right else 3
        patterns=[
            [initial,(initial+delta)%4,(initial+2)%4],
            [initial]*3+[(initial+delta)%4,(initial+2)%4]+[(initial+2)%4]*3,
            [initial,(initial+delta)%4,(initial+2)%4,(initial+2+delta)%4,initial],
            [initial,(initial+delta)%4,(initial+2)%4,(initial+1)%4,initial],
        ]
        for steps in patterns:
            raw,coords=route(steps,initial)
            for mode in (61,62,63):
                load(raw,initial,coords);dll.generate(mode);saved=checkpoint()
                for index,token in enumerate(sequence()):
                    if token not in (13,14,33,34,53,54):continue
                    start=dll.src(index);before=dll.value(3)
                    assert dll.run(index)==0,(initial,right,raw,index)
                    assert dll.value(3)==before+2
                    expected=102 if token in (14,34,54) else 101
                    assert [dll.code(before),dll.code(before+1)]==[expected,expected]
                    assert dll.value(6)==0,'search parameters not active during turn'
                    assert checkpoint()==saved,'sprint parameters not restored'
                    assert [dll.value(1),dll.value(2)]==list(coords[start+2])
                    heading=(initial*2+sum(raw[:start])+sum(raw[start:start+3]))%8//2
                    assert dll.value(0)==heading
                    assert dll.value(4)==0 and dll.value(5)==0 and dll.value(16)==0
                    assert dll.value(17)==initial and dll.value(19)==0
                    motion_cases+=1
results.append({'case':'left/right, 4 headings, terminal/straight/adjacent/other-turn / 3 modes; actual search turn functions','passed':motion_cases})

raw,coords=route([0,1,2])
load(raw,0,coords);dll.generate(61);dll.run(0);total_polls=dll.value(18)
for abort in range(1,total_polls+1):
    load(raw,0,coords);dll.generate(61);saved=checkpoint();dll.set_abort(abort)
    assert dll.run(0)==1
    assert dll.value(4)==1 and dll.value(5)==1 and dll.value(7)==0
    assert checkpoint()==saved and dll.value(16)==0
results.append({'case':'remote abort at every motion polling point; cleanup and parameter restore','passed':total_polls})

for right in (0,1):
    raw,coords=route([0,1 if right else 3,2]);load(raw,0,coords);dll.generate(61)
    dll.set_collision(right);assert dll.run(0)==1
    assert dll.value(3)==0 and dll.value(4)==1
load([0,1,2,1],0,[(0,0)]*4);dll.generate(61)
assert dll.run(0)==1 and dll.value(3)==0
results.append({'case':'blocked turn or inconsistent source coordinates stops before turn','passed':3})

for right in (0,1):
    raw,coords=route([0,1 if right else 3,2])
    load(raw,0,coords);dll.generate(61);dll.set_edge(12)
    assert dll.run(0)==0 and dll.value(3)==2
    for distance in (35,45):
        load(raw,0,coords);dll.generate(61);dll.set_outer_wall(right,distance)
        saved=checkpoint();assert dll.run(0)==0
        assert checkpoint()==saved and [dll.value(1),dll.value(2)]==list(coords[-1])
load(raw,0,coords);dll.generate(61);dll.set_edge(-1)
assert dll.run(0)==1 and dll.value(3)==0 and dll.value(4)==1
load(raw,0,coords);dll.generate(61);dll.set_edge(-1);dll.set_front(5)
assert dll.run(0)==1 and dll.value(3)==0 and dll.value(4)==1
results.append({'case':'delayed/missing wall edge, front obstruction, nonzero search wall-offset compensation','passed':8})

report={'status':'PASS','cases':results,'note':'Host simulation executes extracted production C. Motor/IR/tick input is simulated; this is not a physical maze test.'}
(OUT/'test_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=True,indent=2))

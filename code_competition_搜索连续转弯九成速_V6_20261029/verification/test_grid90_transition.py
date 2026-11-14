from pathlib import Path
import ctypes as C
import json
import os
import random
import re
import subprocess
from collections import deque

ROOT=Path(__file__).resolve().parents[1]
REF = Path(r'D:\WeChat Files\wxid_nuop8i99y99x22\FileStorage\File\2026-10\竞赛例程最终版\竞赛例程最终版\竞赛例程最终版')
OUT = ROOT / 'verification/grid90_transition'
OUT.mkdir(exist_ok=True)

def clean(path):
    return re.sub(r'/\*.*?\*/|//[^\n]*', '', path.read_bytes().decode('latin1').replace('\r\n', '\n'), flags=re.S)

def function(s, name):
    m = re.search(r'^(?:static\s+)?(?:void|int|u8|u16)\s+' + name + r'\s*\([^;]*?\)\s*\{', s, re.M)
    if not m:
        raise ValueError(name)
    brace = s.index('{', m.start())
    end, level = brace + 1, 1
    while level:
        if s[end] == '{': level += 1
        if s[end] == '}': level -= 1
        end += 1
    return s[m.start():end]

u = clean(ROOT / 'UTILITY/utility.c')
s = clean(ROOT / 'STRATAGY/stratagy.c')
timer = clean(ROOT / 'HARDWARE/TIMER/timer.c')
ref_u = clean(REF / 'UTILITY/utility.c')
defs = '\n'.join(l for l in clean(ROOT / 'UTILITY/utility.h').splitlines() if re.match(r'#define\s', l) and '__UTILITY' not in l)
defs += '\n' + '\n'.join(l for l in clean(ROOT / 'HARDWARE/TIMER/timer.h').splitlines() if re.match(r'#define\s+\w+\s+\d+\s*$', l))
state = u[u.index('#define spurt_plan_start_dir'):u.index('extern const u8 trackcoor;')]
common = r'''
#include <string.h>
#include <stdlib.h>
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned char uchar;
typedef struct {char cx,cy,action;} FASTWAYACTION;
typedef struct {char cX,cY;} MAZECOOR;
#define MAZETYPE 16
#define UP 0
#define RIGHT 1
#define DOWN 2
#define LEFT 3
#define COORADJUST
u8 actionsequence[200],action_correct_sequence[200];
u8 GucMapStep[16][16],GucMapBlock[16][16],Mapfastway_90[16][16];
FASTWAYACTION FastwayAction_90[256];
MAZECOOR GmcMouse,GmcStack[256];
u8 FastwayCount_90,GucMouseDir,sleepmode,task,goaheadbugflag;
u8 sidewall_threshold,LastSearchAction,turn_delay,keep_dis,flashwrite;
u8 lefthaswall,righthaswall,lefthaswallh,righthaswallh,forwardhaswall;
u8 lpeakfindenable,rpeakfindenable,blocknum;
u8 speedmode,ObjectGoTo_Flag,GoOrBackFlag,spurt_action_num,spurt_45_end_flag;
u16 isemergency,speed_ss,speed_turn,speed_fastss,decdistance,tick;
u16 dislpeak,disrpeak,leftpeaktrack,rightpeaktrack;
long disf,disl,disr,dislh,dislhh,disrh,disrhh;
volatile long track,absolutetrack;
long basetrack,e,eh,esum,gangle,gangleh,ganglehh,peakgangle,peakgangleh;
int turno,turnrighto,turnlefto,setvl,setvr,speed;
u8 dis2wall=40;
const u8 trackcoor=127;
char GoalX,GoalY;
double kpl,kil,kdl,kpr,kir,kdr,turn_kp,turn_kd;
float SPURT90IR_kp1,SPURT90IR_kp2,SPURT90IR_kd,SPURT90GYRO_kp,SPURT90GYRO_kd;
float SpurtAccRatio45=.7f,SpurtDecRatio45=.7f;
int LTopSpeed=140,LBottomSpeed=95;
int turn_count,stop_count,node_count,parameter_errors,fast_count,max_speed,coor_count;
int turn_codes[256],turn_at[256],fast_distance[256],fast_phase[256],node_track[256];
int sim_steps,front_plane,edge_at,edge_count,abort_at,stop_at,curve_abort;
void settrack(int t);
void sim_tick(void);
void ledoff(int n){(void)n;}
void ledon(int n){(void)n;}
void delay_ms(int n){(void)n;}
void taskturn(u8 code) {
    turn_codes[turn_count]=code;turn_at[turn_count++]=(int)absolutetrack;
    if(speed_ss!=70 || speed_turn!=70 || turn_delay!=10 ||
       kpl!=13 || kil!=.74 || kdl!=.9 || turn_kp!=4 || turn_kd!=4)
        parameter_errors++;
    if(curve_abort) isemergency=1;
    front_plane=0;disf=200;settrack(0);
}
void mouseTurnback(void) {
    turn_codes[turn_count]=180;turn_at[turn_count++]=(int)absolutetrack;
    GucMouseDir=(GucMouseDir+2)%4;
    GmcMouse.cX+=(int[]){0,1,0,-1}[GucMouseDir];
    GmcMouse.cY+=(int[]){1,0,-1,0}[GucMouseDir];
    LastSearchAction=0;settrack(0);
}
void __mouseCoorUpdate(void){coor_count++;}
void taskstop(void){stop_count++;if(decdistance!=30)parameter_errors++;}
void parameter_search(void);
void parameter_spurt(void);
'''

# Use the production ISR's actual wall-edge rebase blocks. Only encoder motion
# and IR inputs are synthesized. One simulated step is 1 mm for straight waits.
isr_cases = timer[timer.index('case STRAIGHTEYECLOSECOORADJ:'):timer.index('case SMARTSTRAIGHTSPURT:')]
simulator = r'''
void sim_tick(void) {
    if(++sim_steps>200000) {isemergency=1;return;}
    track++;absolutetrack++;speed=speed_ss;
    if((task==SPURT90IR || task==SPURT90BLIND) && speed_ss>max_speed)max_speed=speed_ss;
    if(front_plane)disf=front_plane-absolutetrack;
    if(abort_at>0 && absolutetrack>=abort_at)isemergency=1;
    if(stop_at>0 && absolutetrack>=stop_at)sleepmode=1;
    lefthaswallh=lefthaswall;righthaswallh=righthaswall;
    if(edge_at>0 && absolutetrack==edge_at) {
        lefthaswallh=1;lefthaswall=0;edge_count++;
    }
    switch(task) {
''' + isr_cases + r'''
    default:break;
    }
}
'''


common+='\n#define MAZESEARCH 2\n#define V6_TURN_CHAIN host_v6_chain\nu8 GucMouseTask=4,host_v6_chain;\n'
v6_selector=function(u,'v6UseSlowSearchTurn')
common+='\nu8 v6UseSlowSearchTurn(void);\nvoid v6SlowSearchTurn90(u8 d){parameter_errors++;taskturn(d==1?101:102);}\n'
functions = [v6_selector]

for name in ['settrack', 'before90turn', 'mouseTurnright', 'mouseTurnleft',
             'spurtSearchRunning', 'spurtSearchWaitTrack', 'gridSearchParameters',
             'gridFastStraight90', 'gridSearchWait90', 'gridSearchNode90', 'gridStraight90', 'spurt_grid90']:
    body = function(u, name)
    if name == 'gridFastStraight90':
        # Insert after declarations to keep source valid for both C compilers.
        body = body.replace('if(initial<10)', 'fast_distance[fast_count]=distance;fast_phase[fast_count++]=phase;\n    if(initial<10)', 1)
        body = body.replace('while(track<distance)\n    {', 'while(track<distance)\n    {\n        sim_tick();', 1)
    if name == 'gridSearchWait90':
        body = body.replace('while(track<distance)\n    {', 'while(track<distance)\n    {\n        sim_tick();', 1)
    if name == 'before90turn':
        body = body.replace('while(track<turn_delay)\n            {', 'while(track<turn_delay)\n            {\n                sim_tick();', 1)
        # Legacy non-grid branch, used only in separate state tests if called.
        body = re.sub(r'while\(1\)\s*\{', 'while(1) { sim_tick();', body)
    if name == 'gridSearchNode90':
        body = body.replace('gridSearchParameters();', 'node_track[node_count++]=(int)track;gridSearchParameters();', 1)
    functions.append(body)

ref_functions = []
for name in ['mazesearchhaswall', 'mazesearchnowall', 'mouseGoahead']:
    body = function(ref_u, name)
    for n in ['mazesearchhaswall', 'mazesearchnowall', 'mouseGoahead']:
        body = re.sub(r'\b' + n + r'\b', 'ref_' + n, body)
    body = re.sub(r'while\((track<[^)]*)\)\s*task=(\w+);', r'while(\1){task=\2;sim_tick();}', body)
    body = re.sub(r'while\((track<[^)]*)\);', r'while(\1){sim_tick();}', body)
    if name == 'mouseGoahead':
        body = re.sub(r'while\(1\)\s*\{', 'while(1) { if(track>90 && track<180)sim_tick();', body)
    ref_functions.append(body)

exports = r'''
#define API __declspec(dllexport)
API void setup(u8 *map,int x,int y,int dir,int gx,int gy) {
    memcpy(GucMapBlock,map,256);GmcMouse.cX=x;GmcMouse.cY=y;GucMouseDir=dir;
    GoalX=gx;GoalY=gy;FastwayCount_90=0;
    memset(FastwayAction_90,0,sizeof(FastwayAction_90));
    memset(action_correct_sequence,0,sizeof(action_correct_sequence));
    memset(actionsequence,0xaa,sizeof(actionsequence));
    sleepmode=0;isemergency=0;task=0;speed=70;speed_ss=80;
    lefthaswall=righthaswall=forwardhaswall=0;
    lefthaswallh=righthaswallh=1;
    disf=disl=disr=dislh=dislhh=disrh=disrhh=200;
    track=absolutetrack=0;basetrack=0;LastSearchAction=0;
    turn_count=stop_count=node_count=parameter_errors=fast_count=max_speed=0;
    sim_steps=front_plane=edge_at=edge_count=abort_at=stop_at=curve_abort=coor_count=0;
}
API int plan(void){gridPath90(GoalX,GoalY);return FastwayCount_90;}
API int run(void){return spurt_grid90();}
API int node(int i,int field) {
    if(field==0)return FastwayAction_90[i].cx;
    if(field==1)return FastwayAction_90[i].cy;
    return FastwayAction_90[i].action;
}
API void config(int n,int v) {
    if(n==0)front_plane=v;
    if(n==1)edge_at=v;
    if(n==2)abort_at=v;
    if(n==3)stop_at=v;
    if(n==4)curve_abort=v;
    if(n==5)lefthaswall=v;
    if(n==6)disr=v;
    if(n==7)disl=v;
}
API int direct(int cells,int initial,int last,int reference) {
    action_correct_sequence[198]=1;action_correct_sequence[197]=200;
    gridSearchParameters();LastSearchAction=last;settrack(initial);
    if(reference){ref_mouseGoahead((char)cells);return 1;}
    return gridStraight90((u16)cells);
}
API void corrupt(int i,int field,int value) {
    if(field==0)FastwayAction_90[i].cx=value;
    if(field==1)FastwayAction_90[i].cy=value;
    if(field==2)FastwayAction_90[i].action=value;
    if(field==3)FastwayCount_90=value;
}
API int value(int n) {
    switch(n){
    case 0:return GmcMouse.cX;case 1:return GmcMouse.cY;case 2:return GucMouseDir;
    case 3:return sleepmode;case 4:return task;case 5:return stop_count;
    case 6:return turn_count;case 7:return node_count;case 8:return parameter_errors;
    case 9:return spurt_action_num;case 10:return fast_count;case 11:return max_speed;
    case 12:return track;case 13:return spurt_search_active;
    case 14:return spurt_search_failed;case 15:return setvl|setvr;
    case 16:return absolutetrack;case 17:return edge_count;case 18:return LastSearchAction;
    default:return -999;
    }
}
API int turn(int i){return turn_codes[i];}
API int turnpos(int i){return turn_at[i];}
API int dist(int i){return fast_distance[i];}
API int phase(int i){return fast_phase[i];}
API int seq(int i){return actionsequence[i];}
'''
program = common + '\n' + defs + '\n' + state + '\n'
program += '\n'.join(function(clean(ROOT/'USER/user.c'), n) for n in ['parameter_search','parameter_spurt'])
program += '\n' + function(s, 'mapStepEdit') + '\n' + function(s, 'gridPath90')
program += '\n' + simulator + '\n' + '\n\n'.join(functions) + '\n' + '\n\n'.join(ref_functions) + '\n' + exports
(OUT / 'transition_host.c').write_text(program, encoding='ascii')
msvc = Path(r'D:\vs\VC\Tools\MSVC\14.41.34120')
sdk = Path(r'D:\Windows Kits\10'); version = '10.0.22621.0'
env = os.environ.copy(); env['PATH'] = str(msvc/'bin/Hostx64/x64') + ';' + env['PATH']
cmd = [str(msvc/'bin/Hostx64/x64/cl.exe'), '/nologo', '/LD', '/Od', '/W3',
       '/I'+str(msvc/'include'), '/I'+str(sdk/'Include'/version/'ucrt'),
       str(OUT/'transition_host.c'), '/Fo'+str(OUT/'transition_host.obj'), '/link',
       '/LIBPATH:'+str(msvc/'lib/x64'), '/LIBPATH:'+str(sdk/'Lib'/version/'ucrt/x64'),
       '/LIBPATH:'+str(sdk/'Lib'/version/'um/x64'), '/OUT:'+str(OUT/'transition_host.dll'),
       '/IMPLIB:'+str(OUT/'transition_host.lib')]
p = subprocess.run(cmd, capture_output=True, env=env, cwd=OUT)
(OUT/'host_compile.log').write_bytes(p.stdout+p.stderr)
if p.returncode:
    print(ascii((p.stdout+p.stderr).decode('utf-8', errors='replace')))
    raise SystemExit(p.returncode)
dll = C.CDLL(str(OUT/'transition_host.dll'))
dll.setup.argtypes = [C.POINTER(C.c_ubyte)] + [C.c_int]*5
DX, DY = (0,1,0,-1), (1,0,-1,0)

def link(m, a, b):
    x,y=a;xx,yy=b
    d=next(d for d in range(4) if (x+DX[d],y+DY[d])==b)
    m[x*16+y]|=1<<d;m[xx*16+yy]|=1<<((d+2)%4)

def setup(m, a, goal, direction):
    dll.setup((C.c_ubyte*256)(*m),*a,direction,*goal)

def path_map(path):
    m=[0]*256
    for a,b in zip(path,path[1:]):link(m,a,b)
    return m

def bfs(m,a,b):
    q=deque([(a,0)]);seen={a}
    while q:
        (x,y),dist=q.popleft()
        if (x,y)==b:return dist
        for d in range(4):
            nxt=x+DX[d],y+DY[d]
            if 0<=nxt[0]<16 and 0<=nxt[1]<16 and m[x*16+y]&(1<<d) and nxt not in seen:
                seen.add(nxt);q.append((nxt,dist+1))
    return None

def verify(m,a,b,direction,expected):
    setup(m,a,b,direction);count=dll.plan()
    assert count==expected+1,(a,b,direction,count,expected)
    raw=[dll.node(i,2) for i in range(count)]
    heading=direction;turns=[];distances=[];phases=[];last=0;i=0
    while i<count-1:
        code=raw[i];assert 0<=code<=3
        if code==0:
            n=1
            while i+n<count-1 and raw[i+n]==0:n+=1
            phase=120 if i==0 else (20 if last else 0)
            if n>1:distances.append(180*(n-1));phases.append(phase)
            last=0;i+=n
        else:
            heading=(heading+code)%4;turns.append({1:102,3:101,2:180}[code]);last=0 if code==2 else code;i+=1
    assert dll.run()==1,(raw,a,b,direction,dll.value(16))
    assert [dll.value(0),dll.value(1),dll.value(2)]==[*b,heading]
    assert [dll.value(n) for n in (3,4,5,8,9,12,13,14,15)]==[1,0,1,0,expected,0,0,0,0]
    assert [dll.turn(i) for i in range(dll.value(6))]==turns
    assert [dll.dist(i) for i in range(dll.value(10))]==distances
    assert [dll.phase(i) for i in range(dll.value(10))]==phases
    assert all(dll.seq(i)==0 for i in range(200)) and dll.value(11)<=140
    return dll.value(11)

results=[]
# Reproduce the failed geometry: with the reference's initial 120, a one-cell
# straight must reach its curve after 60+10 mm. The old runner reaches the
# front <20 guard at ~91 mm before ever calling its right curve.
first_cases=[]
for n in (1,2,5):
    for turn in (1,3):
        for wall in (0,1):
            path=[(4,y) for y in range(4,5+n)]
            path.append((4+(1 if turn==1 else -1),4+n))
            setup(path_map(path),path[0],path[-1],0);assert dll.plan()==n+2
            dll.config(0,180*n-120+50);dll.config(5,wall)
            assert dll.run()==1
            actual=dll.turnpos(0);expected=180*n-120+10
            assert actual==expected,(n,turn,wall,actual,expected)
            assert dll.value(8)==0
            first_cases.append({'cells':n,'turn':turn,'side_wall':wall,'curve_start_mm':actual})
results.append({'case':'first straight followed by left/right; 1/2/5 cells; side wall on/off; finite front wall','passed':len(first_cases),'traces':first_cases})

# Compare travel with the supplied reference C code without a fixed-tail stub.
comparisons=0
for cells in (1,2,5):
    for initial,last in ((120,0),(0,0),(0,1),(0,2),(0,3)):
        for wall in (0,1):
            # The old no-wall helper stores literal 1 (TurnLeft) as its last
            # action. Do not reproduce its repeated 20 mm deductions over
            # multiple straight cells. Compare that helper one node at a time.
            if cells>1 and not wall:
                continue
            travels=[]
            for reference in (0,1):
                setup([0]*256,(0,0),(0,0),0);dll.config(5,wall)
                assert dll.direct(cells,initial,last,reference)==1
                travels.append(dll.value(16))
            assert travels[0]==travels[1],(cells,initial,last,wall,travels)
            comparisons+=1
results.append({'case':'actual C straight distance versus supplied reference mouseGoahead; inherited 120 / straight / left / right / turnaround states','passed':comparisons})

edges=0
for wall in (0,1):
    for edge_at in (135,150,175):
        setup([0]*256,(0,0),(0,0),0);dll.config(5,wall);dll.config(1,edge_at)
        assert dll.direct(1,0,0,0)==1
        assert dll.value(17)==1 and dll.value(16)==edge_at+53,(wall,edge_at,dll.value(16))
        edges+=1
results.append({'case':'production ISR wall-edge rebase to 127, followed by 53 mm positioning; wall/no-wall branches','passed':edges})

paths=[[(0,0),(0,1)],[(0,0),(0,1),(0,2),(0,3),(0,4),(0,5)],
       [(4,4),(4,5),(5,5),(5,4)],[(4,4),(4,5),(3,5),(3,4)],
       [(0,0),(0,1),(1,1),(1,2),(2,2),(2,3),(3,3)],
       [(4,4),(4,5),(5,5),(5,4),(6,4),(6,5)],
       [(4,4),(4,5),(5,5),(6,5),(6,4),(6,3)],[(4,4)]]
for path in paths:
    for direction in range(4):verify(path_map(path),path[0],path[-1],direction,len(path)-1)
results.append({'case':'left/right wall U bypass, consecutive U, alternating corners, goal at corner, start at goal; four headings','passed':len(paths)*4})

random.seed(20261022)
for trial in range(200):
    m=[0]*256;stack=[(0,0)];visited={(0,0)}
    while stack:
        x,y=stack[-1];nxts=[(x+DX[d],y+DY[d]) for d in range(4) if 0<=x+DX[d]<16 and 0<=y+DY[d]<16 and (x+DX[d],y+DY[d]) not in visited]
        if not nxts:stack.pop();continue
        nxt=random.choice(nxts);link(m,(x,y),nxt);visited.add(nxt);stack.append(nxt)
    for _ in range(20):
        x,y=random.randrange(16),random.randrange(16);d=random.randrange(4);nxt=x+DX[d],y+DY[d]
        if 0<=nxt[0]<16 and 0<=nxt[1]<16:link(m,(x,y),nxt)
    a=(random.randrange(16),random.randrange(16));b=(random.randrange(16),random.randrange(16))
    for direction in range(4):verify(m,a,b,direction,bfs(m,a,b))
results.append({'case':'random connected 16x16 mazes; production planner versus independent BFS and production executor','passed':800})
path=[(0,y) for y in range(16)]
assert verify(path_map(path),path[0],path[-1],0,15)==140
results.append({'case':'long straight retains 140 speed limit and finishes with full search positioning cell','passed':1})

failures=0
path=[(0,0),(0,1),(1,1)]
for option,value in ((0,30),(2,25),(3,25),(4,1),(6,40)):
    setup(path_map(path),path[0],path[-1],0);assert dll.plan()==3;dll.config(option,value)
    assert dll.run()==0,(option,value)
    assert dll.value(3)==1 and dll.value(4)==0 and dll.value(14)==1 and dll.value(15)==0
    if option in (0,2,3,6):assert dll.value(6)==0
    failures+=1
for boundary,d in [((0,0),2),((0,0),3),((15,15),0),((15,15),1)]:
    m=[0]*256;m[boundary[0]*16+boundary[1]]=1<<d
    setup(m,(0,0),(1,1),0);assert dll.plan()==0;failures+=1
setup([0]*256,(0,0),(1,1),0);assert dll.plan()==0 and dll.run()==0;failures+=1
for field,value in ((0,7),(1,7),(2,4),(3,0)):
    setup(path_map(path),path[0],path[-1],0);assert dll.plan()==3
    dll.corrupt(0,field,value);assert dll.run()==0 and dll.value(6)==0 and dll.value(10)==0;failures+=1
results.append({'case':'front-wall abort, emergency/sleep during approach, curve abort, blocked turn side, invalid paths rejected','passed':failures})
snake=[(x,y) for y in range(16) for x in (range(16) if y%2==0 else range(15,-1,-1))]
m=path_map(snake);verify(m,snake[0],snake[254],1,254)
setup(m,snake[0],snake[255],1);assert dll.plan()==0
results.append({'case':'254-edge route accepted; 255-edge route refused without count wrap','passed':2})

# Confirm this fixture distinguishes the failed firmware from the fix. Compile
# the saved, unmodified former runner with the same synthetic front wall.
old_u=clean(ROOT/'verification/before_grid90_transition_fix_20261022/UTILITY/utility.c')
old_functions=[]
for name in ['settrack','before90turn','mouseTurnright','mouseTurnleft',
             'spurtSearchRunning','spurtSearchWaitTrack','gridSearchParameters',
             'gridFastStraight90','gridStraight90','spurt_grid90']:
    body=function(old_u,name)
    if name=='gridFastStraight90':
        body=body.replace('while(track<distance)\n    {','while(track<distance)\n    {\n        sim_tick();',1)
    if name=='before90turn':
        body=re.sub(r'while\(1\)\s*\{','while(1) { sim_tick();',body)
    old_functions.append(body)
old_program=common+'\n'+defs+'\n'+state+'\n'
old_program+='\n'.join(function(clean(ROOT/'USER/user.c'),n) for n in ['parameter_search','parameter_spurt'])
old_program+='\n'+function(s,'mapStepEdit')+'\n'+function(s,'gridPath90')+'\n'+simulator
old_program+='\n'+'\n\n'.join(ref_functions)
old_program+='\nvoid mazesearchhaswall(void){ref_mazesearchhaswall();}\nvoid mazesearchnowall(void){ref_mazesearchnowall();}\n'
old_program+='\n'+'\n\n'.join(old_functions)+'\n'+exports
(OUT/'previous_runner_host.c').write_text(old_program,encoding='ascii')
old_cmd=[arg.replace('transition_host','previous_runner_host') for arg in cmd]
p=subprocess.run(old_cmd,capture_output=True,env=env,cwd=OUT)
(OUT/'previous_host_compile.log').write_bytes(p.stdout+p.stderr)
assert p.returncode==0,(p.stdout+p.stderr).decode('mbcs',errors='replace')
previous=C.CDLL(str(OUT/'previous_runner_host.dll'))
previous.setup.argtypes=dll.setup.argtypes
path=[(0,0),(0,1),(1,1)];m=path_map(path)
previous.setup((C.c_ubyte*256)(*m),0,0,0,1,1);assert previous.plan()==3
previous.config(0,110)
assert previous.run()==0 and previous.value(6)==0
setup(m,(0,0),(1,1),0);assert dll.plan()==3;dll.config(0,110)
assert dll.run()==1 and dll.turnpos(0)==70
results.append({'case':'same finite-front-wall regression fixture: saved previous runner stops before right curve; fixed runner calls right curve at 70 mm','passed':1,'previous_straight_travel_mm':previous.value(16),'fixed_curve_start_mm':dll.turnpos(0)})

# The motors are stubbed in this host test; verify the actual embedded curve
# selection/length and all four arrays directly against the supplied reference.
for name in ['vouter_search','vinner_search','gyro_l_search','gyro_r_search']:
    pattern=r'const short '+name+r'\[\d+\]\s*=\s*\{([^}]+)\}'
    arrays=[list(map(int,re.findall(r'-?\d+',re.search(pattern,clean(p/'USER/user.c')).group(1)))) for p in (ROOT,REF)]
    assert len(arrays[0])==195 and arrays[0]==arrays[1],name
assert 'length=sizeof(gyro_l_search);' in function(u,'taskturn')
assert 'length=sizeof(gyro_r_search);' in function(u,'taskturn')
results.append({'case':'all four search curve arrays match reference (195 points each); competition uses full left/right search lengths','passed':5})

report={'status':'PASS','cases':results,'method':'Extracted production C planner, executor, straight positioning and 90-degree approach; production ISR wall-edge correction; supplied reference C straight functions for distance comparison. Synthetic encoder/IR inputs and motor curve/stop primitives. No flashing or physical run.'}
(OUT/'test_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=True,indent=2))

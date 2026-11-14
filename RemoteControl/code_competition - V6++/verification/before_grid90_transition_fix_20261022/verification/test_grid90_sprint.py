from pathlib import Path
import ctypes as C
import os,subprocess,re,json,random
from collections import deque

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'verification/grid90';OUT.mkdir(exist_ok=True)
def clean(path):
    return re.sub(r'/\*.*?\*/|//[^\n]*','',path.read_bytes().decode('latin1').replace('\r\n','\n'),flags=re.S)
def function(s,name):
    m=re.search(r'^(?:static\s+)?(?:void|int|u8|u16)\s+'+name+r'\s*\([^;]*?\)\s*\{',s,re.M)
    if not m:raise ValueError(name)
    i=s.index('{',m.start());level=1;e=i+1
    while level:
        if s[e]=='{':level+=1
        elif s[e]=='}':level-=1
        e+=1
    return s[m.start():e]
utility=clean(ROOT/'UTILITY/utility.c');strategy=clean(ROOT/'STRATAGY/stratagy.c')
defs='\n'.join(l for l in clean(ROOT/'UTILITY/utility.h').splitlines() if re.match(r'#define\s',l) and '__UTILITY' not in l)
state=utility[utility.index('#define spurt_plan_start_dir'):utility.index('extern const u8 trackcoor;')]
common=r'''
#include <string.h>
typedef unsigned char u8;
typedef unsigned short u16;
typedef unsigned char uchar;
typedef struct {char cx,cy,action;} FASTWAYACTION;
typedef struct {char cX,cY;} MAZECOOR;
/* This switch supplies an ISR polling hook in the extracted waiting loops.
 * The tested planner/runner are the actual competition functions, extracted
 * without their outer #ifndef guards. No remote protocol is exercised. */
#define MOUSE_REMOTE
#undef NULL
#define NULL 0
#define UP 0
#define RIGHT 1
#define DOWN 2
#define LEFT 3
#define MAZETYPE 16
#define SMARTSTRAIGHTFAST 32
#define STRAIGHTEYECLOSECOORADJ 33
#define SMARTSTRAIGHTSPURTL 45
#define SMARTSTRAIGHT 30
#define BEFORE90TURN 27
#define SPURT90BLIND 42
#define SPURT90IR 43
u8 actionsequence[200],action_correct_sequence[200];
u8 GucMapStep[16][16],GucMapBlock[16][16],Mapfastway_90[16][16];
FASTWAYACTION FastwayAction_90[256];
MAZECOOR GmcMouse,GmcStack[256];
u8 FastwayCount_90,GucMouseDir,sleepmode,task,goaheadbugflag;
u8 sidewall_threshold,LastSearchAction,turn_delay,keep_dis,flashwrite;
u8 lefthaswall,righthaswall,lefthaswallh,righthaswallh,forwardhaswall;
u8 speedmode,ObjectGoTo_Flag,GoOrBackFlag,spurt_action_num,spurt_45_end_flag;
u16 isemergency,speed_ss,speed_turn,speed_fastss,decdistance;
long disf,disl,disr,dislh,dislhh,disrh,disrhh;
volatile long track,absolutetrack;
long basetrack,e,eh,esum;
int turno,turnrighto,turnlefto,setvl,setvr,speed;
u8 dis2wall=40;
const u8 trackcoor=127;
char GoalX,GoalY;
double kpl,kil,kdl,kpr,kir,kdr,turn_kp,turn_kd;
float SPURT90IR_kp1,SPURT90IR_kp2,SPURT90IR_kd,SPURT90GYRO_kp,SPURT90GYRO_kd;
float SpurtAccRatio45=.7f,SpurtDecRatio45=.7f;
int LTopSpeed=140,LBottomSpeed=95;
int run_ticks,turn_count,stop_count,tail_count,parameter_errors,fast_count,max_speed;
int turn_codes[256],fast_distance[256],fast_phase[256];
void settrack(int t);
u8 LP_AbortRequested(void) {
    if(run_ticks) {
        track++;absolutetrack++;
        if(speed_ss>max_speed && (task==SPURT90IR || task==SPURT90BLIND))max_speed=speed_ss;
        speed=speed_ss;
    }
    return 0;
}
void ledoff(int n){(void)n;}
void ledon(int n){(void)n;}
void delay_ms(int n){(void)n;}
void taskturn(u8 code) {
    turn_codes[turn_count++]=code;
    if(speed_ss!=80 || speed_turn!=30 || turn_delay!=9 ||
       kpl!=13 || kil!=.74 || kdl!=.9 || turn_kp!=4 || turn_kd!=4)
        parameter_errors++;
    settrack(0);
}
void mouseTurnback(void) {
    turn_codes[turn_count++]=180;
    GucMouseDir=(GucMouseDir+2)%4;
    GmcMouse.cX+=(int[]){0,1,0,-1}[GucMouseDir];
    GmcMouse.cY+=(int[]){1,0,-1,0}[GucMouseDir];
    LastSearchAction=0;settrack(0);
}
void search_tail(void) {
    if(track!=100 || speed_ss!=80 || speed_turn!=30 || LastSearchAction!=0)
        parameter_errors++;
    tail_count++;absolutetrack+=80;settrack(180);
}
void mazesearchhaswall(void){search_tail();}
void mazesearchnowall(void){search_tail();LastSearchAction=1;}
void taskstop(void){stop_count++;if(decdistance!=30)parameter_errors++;}
void parameter_search(void);
void parameter_spurt(void);
'''
unames=['settrack','before90turn','mouseTurnright','mouseTurnleft','spurtSearchRunning',
        'spurtSearchWaitTrack','gridSearchParameters','gridFastStraight90','gridStraight90','spurt_grid90']
functions=[]
for name in unames:
    body=function(utility,name)
    if name=='gridFastStraight90':
        pos=body.index('{')+1
        body=body[:pos]+'\n fast_distance[fast_count]=distance;fast_phase[fast_count++]=phase;\n'+body[pos:]
    functions.append(body)
exports=r'''
#define API __declspec(dllexport)
API void setup(u8 *map,int x,int y,int dir,int gx,int gy) {
    memcpy(GucMapBlock,map,256);GmcMouse.cX=x;GmcMouse.cY=y;GucMouseDir=dir;
    GoalX=gx;GoalY=gy;FastwayCount_90=0;
    memset(FastwayAction_90,0,sizeof(FastwayAction_90));
    memset(action_correct_sequence,0,sizeof(action_correct_sequence));
    memset(actionsequence,0xaa,sizeof(actionsequence));
    sleepmode=0;isemergency=0;task=0;speed=30;speed_ss=80;
    lefthaswall=righthaswall=forwardhaswall=0;
    lefthaswallh=righthaswallh=1;
    disf=disl=disr=dislh=dislhh=disrh=disrhh=200;
    track=absolutetrack=0;basetrack=0;
    run_ticks=turn_count=stop_count=tail_count=parameter_errors=fast_count=max_speed=0;
}
API int plan(void){gridPath90(GoalX,GoalY);return FastwayCount_90;}
API int run(void){run_ticks=1;return spurt_grid90();}
API int node(int i,int field) {
    if(field==0)return FastwayAction_90[i].cx;
    if(field==1)return FastwayAction_90[i].cy;
    return FastwayAction_90[i].action;
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
    case 6:return turn_count;case 7:return tail_count;case 8:return parameter_errors;
    case 9:return spurt_action_num;case 10:return fast_count;case 11:return max_speed;
    case 12:return track;case 13:return spurt_search_active;
    case 14:return spurt_search_failed;case 15:return setvl|setvr;
    default:return -999;
    }
}
API int turn(int i){return turn_codes[i];}
API int dist(int i){return fast_distance[i];}
API int phase(int i){return fast_phase[i];}
API int seq(int i){return actionsequence[i];}
'''
program=common+'\n'+defs+'\n'+state+'\n'
program+='\n'.join(function(clean(ROOT/'USER/user.c'),name) for name in ('parameter_search','parameter_spurt'))
program+='\n'+function(strategy,'mapStepEdit')+'\n'+function(strategy,'gridPath90')
program+='\n'+'\n\n'.join(functions)+'\n'+exports
(OUT/'grid90_host.c').write_text(program,encoding='ascii')
msvc=Path(r'D:\vs\VC\Tools\MSVC\14.41.34120');sdk=Path(r'D:\Windows Kits\10');version='10.0.22621.0'
env=os.environ.copy();env['PATH']=str(msvc/'bin/Hostx64/x64')+';'+env['PATH']
cmd=[str(msvc/'bin/Hostx64/x64/cl.exe'),'/nologo','/LD','/Od','/W3',
     '/I'+str(msvc/'include'),'/I'+str(sdk/'Include'/version/'ucrt'),
     str(OUT/'grid90_host.c'),'/Fo'+str(OUT/'grid90_host.obj'),'/link',
     '/LIBPATH:'+str(msvc/'lib/x64'),'/LIBPATH:'+str(sdk/'Lib'/version/'ucrt/x64'),
     '/LIBPATH:'+str(sdk/'Lib'/version/'um/x64'),'/OUT:'+str(OUT/'grid90_host.dll'),'/IMPLIB:'+str(OUT/'grid90_host.lib')]
p=subprocess.run(cmd,capture_output=True,env=env,cwd=OUT)
(OUT/'host_compile.log').write_bytes(p.stdout+p.stderr)
if p.returncode:print(ascii((p.stdout+p.stderr).decode('utf-8',errors='replace')));raise SystemExit(p.returncode)
dll=C.CDLL(str(OUT/'grid90_host.dll'))
dll.setup.argtypes=[C.POINTER(C.c_ubyte)]+[C.c_int]*5
DX=(0,1,0,-1);DY=(1,0,-1,0)
def link(m,a,b):
    x,y=a;xx,yy=b;d=next(d for d in range(4) if (x+DX[d],y+DY[d])==b)
    m[x*16+y]|=1<<d;m[xx*16+yy]|=1<<((d+2)%4)
def setup(m,a,goal,dir):dll.setup((C.c_ubyte*256)(*m),*a,dir,*goal)
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
def verify(m,a,b,dir,expected):
    setup(m,a,b,dir);count=dll.plan()
    assert count==expected+1,(a,b,dir,count,expected)
    raw=[dll.node(i,2) for i in range(count)]
    coords=[(dll.node(i,0),dll.node(i,1)) for i in range(count)]
    assert coords[0]==a and coords[-1]==b and raw[-1]==0
    heading=dir;turns=[];distances=[];phases=[];last=0;i=0
    while i<count-1:
        code=raw[i];assert 0<=code<=3
        if code==0:
            n=1
            while i+n<count-1 and raw[i+n]==0:n+=1
            phase=20 if last else 0
            distances.append(n*180-phase-80);phases.append(phase)
            last=0;i+=n
        else:
            heading=(heading+code)%4
            turns.append({1:102,3:101,2:180}[code]);last=0 if code==2 else code;i+=1
    assert dll.run()==1,(raw,coords)
    assert [dll.value(0),dll.value(1)]==list(b) and dll.value(2)==heading
    assert dll.value(3)==1 and dll.value(4)==0 and dll.value(5)==1
    assert dll.value(8)==0 and dll.value(9)==expected
    assert dll.value(12)==0 and dll.value(13)==0 and dll.value(14)==0 and dll.value(15)==0
    assert [dll.turn(i) for i in range(dll.value(6))]==turns
    assert [dll.dist(i) for i in range(dll.value(10))]==distances
    assert [dll.phase(i) for i in range(dll.value(10))]==phases
    assert all(dll.seq(i)==0 for i in range(200))
    assert dll.value(11)<=140
    return dll.value(11)

results=[];focused=0
paths=[[(0,0),(0,1)],[(0,0),(0,1),(0,2),(0,3),(0,4),(0,5)],
       [(4,4),(4,5),(5,5),(5,4)],[(4,4),(4,5),(3,5),(3,4)],
       [(0,0),(0,1),(1,1),(1,2),(2,2),(2,3),(3,3)],
       [(4,4),(4,5),(5,5),(5,4),(6,4),(6,5)],
       [(4,4),(4,5),(5,5),(6,5),(6,4),(6,3)],[(4,4)]]
for path in paths:
    m=[0]*256
    for a,b in zip(path,path[1:]):link(m,a,b)
    for dir in range(4):verify(m,path[0],path[-1],dir,len(path)-1);focused+=1
results.append({'case':'straight, terminal turn, left/right single wall U, alternating corners, adjacent U, start-at-goal; four headings','passed':focused})

random.seed(20261022);random_count=0
for trial in range(200):
    m=[0]*256;stack=[(0,0)];visited={(0,0)}
    while stack:
        x,y=stack[-1];nexts=[(x+DX[d],y+DY[d]) for d in range(4) if 0<=x+DX[d]<16 and 0<=y+DY[d]<16 and (x+DX[d],y+DY[d]) not in visited]
        if not nexts:stack.pop();continue
        nxt=random.choice(nexts);link(m,(x,y),nxt);visited.add(nxt);stack.append(nxt)
    for _ in range(20):
        x,y=random.randrange(16),random.randrange(16);d=random.randrange(4);nxt=x+DX[d],y+DY[d]
        if 0<=nxt[0]<16 and 0<=nxt[1]<16:link(m,(x,y),nxt)
    a=(random.randrange(16),random.randrange(16));b=(random.randrange(16),random.randrange(16));expected=bfs(m,a,b)
    for dir in range(4):verify(m,a,b,dir,expected);random_count+=1
results.append({'case':'random connected 16x16 mazes: actual C flood/planner against independent BFS, then actual C executor','passed':random_count})

m=[0]*256
for y in range(1,16):link(m,(0,y-1),(0,y))
assert verify(m,(0,0),(0,15),0,15)==140
results.append({'case':'long straight reaches original 140 speed limit and stops at goal','passed':1})

invalid=0
for boundary,d in [((0,0),2),((0,0),3),((15,15),0),((15,15),1)]:
    m=[0]*256;m[boundary[0]*16+boundary[1]]=1<<d
    setup(m,(0,0),(1,1),0);assert dll.plan()==0;invalid+=1
setup([0]*256,(0,0),(1,1),0);assert dll.plan()==0
assert dll.run()==0 and dll.value(6)==0 and dll.value(10)==0;invalid+=1
for field,value in [(0,7),(1,7),(2,4),(3,0)]:
    m=[0]*256;link(m,(0,0),(0,1));setup(m,(0,0),(0,1),0);assert dll.plan()==2
    dll.corrupt(0,field,value);assert dll.run()==0 and dll.value(6)==0 and dll.value(10)==0
    assert dll.value(3)==1 and dll.value(4)==0;invalid+=1
results.append({'case':'unreachable goal, open boundary, malformed source coordinates/action/count rejected before motion','passed':invalid})

snake=[(x,y) for y in range(16) for x in (range(16) if y%2==0 else range(15,-1,-1))]
m=[0]*256
for a,b in zip(snake,snake[1:]):link(m,a,b)
verify(m,snake[0],snake[254],1,254)
setup(m,snake[0],snake[255],1);assert dll.plan()==0
results.append({'case':'254 edges plus sentinel fit; 255-edge route rejected without count wraparound','passed':2})

report={'status':'PASS','cases':results,'method':'Extracted production competition C planner and executor; production 90-degree search functions. ISR, search straight tail and motor primitives are simulated. No hardware or upper-computer test.'}
(OUT/'test_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=True,indent=2))

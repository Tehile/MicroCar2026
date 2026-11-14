"""Production turnaround/departure/first-turn flow; synthetic hardware inputs."""
from pathlib import Path
import ast
import ctypes as C
import json
import os
import re
import subprocess

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT.with_name('code_competition_V字路按搜索_20261022')
OUT=ROOT/'verification/continuous_turn_v6_20261029/turn_and_goal_host'
OUT.mkdir(parents=True,exist_ok=True)
def clean(path):
    return re.sub(r'/\*.*?\*/|//[^\n]*','',path.read_bytes().decode('latin1').replace('\r\n','\n'),flags=re.S)
def function(s,name):
    m=re.search(r'^(?:static\s+)?(?:void|int|u8|u16)\s+'+name+r'\s*\([^;]*?\)\s*\{',s,re.M)
    if not m:raise ValueError(name)
    i=s.index('{',m.start());j=i+1;depth=1
    while depth:
        if s[j]=='{':depth+=1
        if s[j]=='}':depth-=1
        j+=1
    return s[m.start():j]
u=clean(ROOT/'UTILITY/utility.c');old=clean(SOURCE/'UTILITY/utility.c')
timer=clean(ROOT/'HARDWARE/TIMER/timer.c');user=clean(ROOT/'USER/user.c');strategy=clean(ROOT/'STRATAGY/stratagy.c')
v5_source=clean(ROOT.with_name('code_competition_停车快照复核与掉头限幅_V5_20261029')/'UTILITY/utility.c')
tree=ast.parse((ROOT/'verification/test_grid90_transition.py').read_text(encoding='utf-8'))
common=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='common' for t in n.targets))
for name in ('taskturn','mouseTurnback','taskstop','__mouseCoorUpdate'):
    common=common.replace(function(common,name),'')
common=common.replace('#include <stdlib.h>','#include <stdlib.h>\n#include <setjmp.h>\nstatic jmp_buf escape;')
common=common.replace('void ledon(int n){(void)n;}','void ledon(int n){if(sleepmode==1 && n>=1 && n<=8)longjmp(escape,100+n);}')
common+=r'''
u8 afterturn_flag,step,block_write_flag,lastside;
u16 disbf180,accdistance=40,stoptick,turnaround_tick;
double accratio,decratio,GOSTRAIGHT_kp=.4;
double SMARTSTRAIGHTFAST_bigerror_kp=.5,SMARTSTRAIGHTFAST_smallerror_kp=.35;
long disf1,disf2;
int speedl,speedr,setgangle;
const short *turn_gyro,*turn_vl,*turn_vr;
int pivot_at,pivot_count,departure_calls,departure_at,departure_end;
int acc_calls,acc_end,save_calls,save_at,point_calls;
int post_left,post_right,post_sample;
int coor_at[256],coor_x[256],coor_y[256],coor_dir[256];
int entry_speed[256],entry_previous[256],entry_coor_count[256];
int command_errors;
void __mouseCoorUpdate(void);
void setangle(int a){gangle=-a;}
void __wallCheck(void){
    coor_at[coor_count]=(int)absolutetrack;
    coor_x[coor_count]=GmcMouse.cX;coor_y[coor_count]=GmcMouse.cY;coor_dir[coor_count]=GucMouseDir;
    coor_count++;
}
void SaveBlockData(void){save_calls++;save_at=(int)absolutetrack;}
void exit_sensors(void){
    front_plane=0;disf=disf1=disf2=300;
    lefthaswall=lefthaswallh=post_left;righthaswall=righthaswallh=post_right;
    disl=dislh=dislhh=post_left?post_sample:200;
    disr=disrh=disrhh=post_right?post_sample:200;
    forwardhaswall=0;
}
'''

common+='\ntypedef unsigned long u32;\ntypedef short s16;\n#include <stdio.h>\n'
common+=clean(ROOT/'UTILITY/search_guard_v5.h').replace('#include "sys.h"','')
common+='\nvolatile V5_STOP_DIAGNOSTICS v5_diag;\nint rawf1,rawf2,rawl,rawr;\n'
common+='\nu32 __get_PRIMASK(void){return 0;}\nvoid __disable_irq(void){}\nvoid __enable_irq(void){}\n'
v5_helpers='\n'.join(function(u,n) for n in ('v5CaptureStop','v5PrintSnapshot','v5PrintStop','v5FrontClear','v5FrontRecheck'))
common+='\n#define MAZESEARCH 2\n#define V6_TURN_CHAIN v5_diag.reserved\nu8 GucMouseTask;\nint host_straight_wall;\nint profile_slow[256],profile_ticks[256],profile_centre[256],profile_start[256],profile_end[256],profile_outer_peak[256];\nu8 breakornot(void);\nvoid mazesearchhaswall(void);\nvoid mazesearchnowall(void);\n'
common+=r"""
void capture_profile(int i){
    int k,a,b;profile_ticks[i]=tick;profile_centre[i]=(turn_vl[1]+turn_vr[1])/2;
    profile_start[i]=turn_gyro[0];profile_end[i]=turn_gyro[tick-1];profile_outer_peak[i]=0;
    for(k=1;k<=tick;k++){a=turn_vl[k];b=turn_vr[k];if(a>profile_outer_peak[i])profile_outer_peak[i]=a;if(b>profile_outer_peak[i])profile_outer_peak[i]=b;}
}
"""
v6_tables=clean(ROOT/'UTILITY/search_turn_v6.h')
v6_selector=function(u,'v6UseSlowSearchTurn')
v6_runner=function(u,'v6SlowSearchTurn90')
v6_runner=v6_runner.replace('u16 end_tick;','u16 end_tick;int profile_index=turn_count;\nturn_codes[turn_count]=direction==TurnLeft?LT90SEARCH:RT90SEARCH;turn_at[turn_count]=(int)absolutetrack;entry_speed[turn_count]=speed_turn;entry_previous[turn_count]=LastSearchAction;entry_coor_count[turn_count]=coor_count;profile_slow[turn_count]=1;turn_count++;')
v6_runner=v6_runner.replace('while(tick<end_tick);','while(tick<end_tick){sim_tick();}')
v6_runner=v6_runner.replace('settrack(0);','capture_profile(profile_index);settrack(0);gangle=0;',1)


defs='\n'.join(l for l in clean(ROOT/'UTILITY/utility.h').splitlines() if re.match(r'#define\s',l) and '__UTILITY' not in l)
defs+='\n'+'\n'.join(l for l in clean(ROOT/'HARDWARE/TIMER/timer.h').splitlines() if re.match(r'#define\s+\w+\s+\d+\s*$',l))
defs+='\n'+u[u.index('#ifndef GOAL_DEPARTURE_EXTRA_MM'):u.index('int turnrighto')]
state=u[u.index('#define spurt_plan_start_dir'):u.index('extern const u8 trackcoor;')]
# user.c owns the active 195-point search arrays; utility.c also contains an
# inactive search_speed_50 alternative. Prefer the active user.c definitions.
array_defs={}
for definition in re.findall(r'const short\s+\w+\s*\[\d+\]\s*=\s*\{[^}]*\};',u+'\n'+user):
    name=re.search(r'const short\s+(\w+)',definition)[1];array_defs[name]=definition
assert 'gyro_l_search[195]' in array_defs['gyro_l_search']
arrays='\n'.join(array_defs.values())
def case(a,b):return timer[timer.index('case '+a+':'):timer.index('case '+b+':')]
cases='\n'.join(case(a,b) for a,b in [('ACC','STOP'),('STOP','SLOWDOWN'),('TURN','TURNAROUND'),('TURNAROUND','POINTTURN90'),('POINTTURN90','BEFORE90TURN'),('BEFORE90TURN','BEFORE180TURN'),('BEFORE180TURN','FORWARDIRTEST'),('KEEPWALLDISTANCE','GOSTRAIGHT'),('GOSTRAIGHT','SMARTSTRAIGHT'),('SMARTSTRAIGHT','SMARTSTRAIGHTFAST'),('SMARTSTRAIGHTFAST','STRAIGHTEYECLOSECOORADJ'),('STRAIGHTEYECLOSECOORADJ','SMARTSTRAIGHTSPURT')])
simulator=r'''
void sim_tick(void){
    int move=1;
    if(++sim_steps>200000)longjmp(escape,999);
    if(task==TURN || task==TURNAROUND || task==POINTTURN90){
        move=0;gangle=(task==TURN || step==0)?turn_gyro[tick+1]:0;
    }else if(task==STOP){move=track<0?1:0;gangle=0;}
    else if(task==BEFORE180TURN || task==KEEPWALLDISTANCE){
        int target=task==BEFORE180TURN?20:keep_dis;
        move=disf>target?1:(disf<target?-1:0);gangle=0;
    }
    track+=move;absolutetrack+=move;
    speedl=speedr=move?1:0;speed=move?speed_turn:0;
    if(front_plane){disf=disf1=disf2=front_plane-absolutetrack;}
    switch(task){
'''+cases+r'''
    default:break;
    }
    if(departure_calls && departure_end<0 && task==SMARTSTRAIGHT &&
       post_left==0 && post_right==0 && (setvl!=speed_turn || setvr!=speed_turn))command_errors++;
}
'''
names=['settrack','taskacc','taskstop','taskgostraight','taskturn','taskturnaround','taskpointturnright','taskpointturnleft',
       'before180turn','after180turn','goalDepartureAfterTurnback','mouseTurnback','specialmouseTurnback',
       'mazeSearch','breakornot','before90turn','mouseTurnright','mouseTurnleft','mazesearchhaswall','mazesearchnowall','mouseGoahead','__mouseCoorUpdate','spurtSearchRunning']
def bodies(src):
    out=[]
    for name in names:
        if name=='goalDepartureAfterTurnback' and src is old:continue
        body=function(src,name)
        # Busy waits are the only places where the host runs a synthetic ISR.
        body=re.sub(r'while\((track<[^)]*)\)\s*task=(\w+);',r'while(\1){task=\2;sim_tick();}',body)
        body=re.sub(r'while\(((?:track|tick)<[^)]*)\);',r'while(\1){sim_tick();}',body)
        if name in ('taskturnaround','taskpointturnright','taskpointturnleft'):
            body=re.sub(r'while\(1\)\s*if\(([^\n]+)\) break;',r'while(1){sim_tick();if(\1)break;}',body)
        if name=='taskturnaround':
            pos=body.rfind('}')
            body=body[:pos]+'pivot_at=(int)absolutetrack;pivot_count++;exit_sensors();\n'+body[pos:]
        if name in ('taskpointturnright','taskpointturnleft'):
            body=body.replace('{','{point_calls++;',1)
        if name=='taskturn':
            body=body.replace('u16 length;','u16 length;int profile_index=turn_count;profile_slow[turn_count]=0;\nturn_codes[turn_count]=pattern;turn_at[turn_count]=(int)absolutetrack;entry_speed[turn_count]=speed_turn;entry_previous[turn_count]=LastSearchAction;entry_coor_count[turn_count]=coor_count;turn_count++;',1)
            body=body.replace('while(tick<length/2-2)\n    {','while(tick<length/2-2)\n    {sim_tick();',1)
            pos=body.rfind('}');body=body[:pos]+'capture_profile(profile_index);gangle=0;\n'+body[pos:]
        if name=='before90turn':
            body=re.sub(r'while\(1\)\s*\{','while(1){sim_tick();',body)
            body=body.replace('while(track<turn_delay)\n            {','while(track<turn_delay)\n            {sim_tick();',1)
        if name in ('mouseGoahead','mazeSearch'):body=re.sub(r'while\(1\)\s*\{','while(1){if(track>90 && track<180)sim_tick();',body)
        if name=='taskacc':
            body=body.replace('{','{acc_calls++;',1);pos=body.rfind('}');body=body[:pos]+'acc_end=(int)absolutetrack;\n'+body[pos:]
        if name=='taskstop':body=body.replace('task=STOP;','task=STOP;sim_tick();',1)
        if name=='goalDepartureAfterTurnback':
            body=body.replace('long distance=(long)disbf180-35;','long distance=(long)disbf180-35;departure_calls++;departure_at=(int)absolutetrack;',1)
            pos=body.rfind('}');body=body[:pos]+'departure_end=(int)absolutetrack;\n'+body[pos:]
        out.append(body)
    return '\n'.join(out)
exports=r'''
#define API __declspec(dllexport)
API void reset(int dir,int front,int side,int postwall,int right,int sample){
    memset(action_correct_sequence,0,sizeof(action_correct_sequence));
    memset(GucMapBlock,0,sizeof(GucMapBlock));
    parameter_search();speed_turn=30;speed=30;speedl=speedr=0;GucMouseTask=MAZESEARCH;disbf180=0;V6_TURN_CHAIN=0;host_straight_wall=0;
    GmcMouse.cX=GoalX=7;GmcMouse.cY=GoalY=7;GucMouseDir=dir;
    settrack(0);absolutetrack=basetrack=0;LastSearchAction=Stright;
    lefthaswall=lefthaswallh=(side&1)!=0;righthaswall=righthaswallh=(side&2)!=0;
    disl=dislh=dislhh=lefthaswall?40:200;disr=disrh=disrhh=righthaswall?40:200;
    forwardhaswall=front;front_plane=front?30:0;disf=disf1=disf2=front?30:300;
    post_left=right?postwall:0;post_right=right?0:postwall;post_sample=sample;
    e=eh=esum=gangle=0;turno=turnlefto=turnrighto=0;setvl=setvr=0;
    task=0;sleepmode=isemergency=0;accdistance=40;decdistance=10;afterturn_flag=0;
    tick=step=stoptick=turnaround_tick=0;sim_steps=0;block_write_flag=1;
    pivot_at=-1;pivot_count=0;departure_calls=0;departure_at=departure_end=-1;
    acc_calls=save_calls=point_calls=0;acc_end=save_at=-1;
    turn_count=coor_count=command_errors=0;ObjectGoTo_Flag=0;
}
API int route(u8 *map,int gx,int gy){
    int code=setjmp(escape);if(code)return code;memcpy(GucMapBlock,map,256);objectGoTo((char)gx,(char)gy);return 0;
}
API void frontdistance(int distance){front_plane=distance;disf=disf1=disf2=distance;}
API int back(int mode){
    int code=setjmp(escape);if(code)return code;
    if(mode==1){GoalX=0;GoalY=0;}
    if(mode==2){spurt_search_active=1;spurt_exec_index=200;speed_ss=speed_turn=70;turn_delay=10;}
    if(mode==3)specialmouseTurnback();else mouseTurnback();return 0;
}
API int ordinary(int right,int previous){
    int code=setjmp(escape);if(code)return code;LastSearchAction=previous;block_write_flag=0;
    if(right)mouseTurnright();else mouseTurnleft();return 0;
}
API int distance_case(int dis,int ramp){
    int code=setjmp(escape);if(code)return code;disbf180=(u16)dis;accdistance=(u16)ramp;settrack(0);
#ifdef HAS_GOAL_DEPARTURE
    goalDepartureAfterTurnback();
#endif
    return 0;
}
API int value(int i){switch(i){
case 0:return GmcMouse.cX;case 1:return GmcMouse.cY;case 2:return GucMouseDir;
case 3:return track;case 4:return LastSearchAction;case 5:return absolutetrack;
case 6:return pivot_at;case 7:return departure_calls;case 8:return departure_at;case 9:return departure_end;
case 10:return coor_count;case 11:return turn_count;case 12:return acc_calls;case 13:return save_calls;
case 14:return save_at;case 15:return afterturn_flag;case 16:return disbf180;case 17:return ObjectGoTo_Flag;
case 18:return point_calls;case 19:return command_errors;case 20:return pivot_count;case 21:return block_write_flag;case 22:return acc_end;
}return -999;}
API int turn(int i,int field){switch(field){case 0:return turn_codes[i];case 1:return turn_at[i];case 2:return entry_speed[i];case 3:return entry_previous[i];case 4:return entry_coor_count[i];}return -999;}
API int node(int i,int field){switch(field){case 0:return coor_x[i];case 1:return coor_y[i];case 2:return coor_dir[i];case 3:return coor_at[i];}return -999;}
'''

exports+=r"""
API int profile(int i,int field){switch(field){case 0:return profile_slow[i];case 1:return profile_ticks[i];case 2:return profile_centre[i];case 3:return profile_start[i];case 4:return profile_end[i];case 5:return profile_outer_peak[i];}return -999;}
API void straight_wall(int enable){host_straight_wall=enable;}
API int series(u8 *actions,int count,int stage,int active){
    int i,code=setjmp(escape);if(code)return code;
    GucMouseTask=stage;spurt_search_active=(u8)active;
    if(active)spurt_exec_index=200;
    for(i=0;i<count;i++){
        if(actions[i]==0){
            if(host_straight_wall){lefthaswall=righthaswall=1;disl=disr=40;}
            mouseGoahead(1);
            if(host_straight_wall)exit_sensors();
        }
        else {
            if(actions[i]==1)mouseTurnleft();else mouseTurnright();
            mazeSearch();
        }
    }
    return 0;
}
"""

prefix=common+'\n'+defs+'\n'+state+'\n'+arrays+'\n'+v6_tables+'\n'+simulator+'\n'+v5_helpers+'\n'+v6_selector+'\n'+v6_runner+'\n'
prefix+='\n'.join(function(user,n) for n in ('parameter_search','parameter_spurt'))+'\n'
suffix='\n'+function(strategy,'mapStepEdit')+'\n'+function(strategy,'objectGoTo')+'\n'+exports
msvc=Path(r'D:\vs\VC\Tools\MSVC\14.41.34120');sdk=Path(r'D:\Windows Kits\10');ver='10.0.22621.0'
env=os.environ.copy();env['PATH']=str(msvc/'bin/Hostx64/x64')+';'+env['PATH']
libraries={}
for label,extra,src in [('baseline',0,old),('v5_baseline',4,v5_source)]+[('extra_%dmm'%n,n,u) for n in (0,2,4,6)]:
    work=OUT/('host_'+label);work.mkdir(exist_ok=True)
    (work/'host.c').write_text(prefix+bodies(src)+suffix,encoding='ascii')
    cmd=[str(msvc/'bin/Hostx64/x64/cl.exe'),'/nologo','/LD','/Od','/W3','/DGOAL_DEPARTURE_EXTRA_MM=%d'%extra]
    if label!='baseline':cmd+=['/DHAS_GOAL_DEPARTURE']
    cmd+=['/I'+str(msvc/'include'),'/I'+str(sdk/'Include'/ver/'ucrt'),'host.c','/Fohost.obj','/link',
          '/LIBPATH:'+str(msvc/'lib/x64'),'/LIBPATH:'+str(sdk/'Lib'/ver/'ucrt/x64'),'/LIBPATH:'+str(sdk/'Lib'/ver/'um/x64'),'/OUT:host.dll','/IMPLIB:host.lib']
    p=subprocess.run(cmd,cwd=work,env=env,capture_output=True)
    (work/'compile.log').write_bytes(p.stdout+p.stderr)
    if p.returncode:raise RuntimeError((p.stdout+p.stderr).decode('mbcs',errors='replace'))
    dll=C.CDLL(str(work/'host.dll'));dll.route.argtypes=[C.POINTER(C.c_ubyte),C.c_int,C.c_int];libraries[label]=dll

DX,DY=(0,1,0,-1),(1,0,-1,0)
def path_for(dir,pattern):
    x=y=7;d=(dir+2)%4;path=[(x,y)]
    for rel in [0]+pattern:
        d=(d+rel)%4;x+=DX[d];y+=DY[d];path.append((x,y))
    return path
def run(dll,path,dir,front,side,postwall,right=0,sample=40,front_distance=None):
    m=[0]*256
    for (x,y),(xx,yy) in zip(path,path[1:]):
        d=next(d for d in range(4) if (x+DX[d],y+DY[d])==(xx,yy));m[x*16+y]|=1<<d;m[xx*16+yy]|=1<<((d+2)%4)
    dll.reset(dir,front,side,postwall,right,sample)
    if front_distance is not None:dll.frontdistance(front_distance)
    code=dll.route((C.c_ubyte*256)(*m),*path[-1]);assert code==0,('route stopped',code,path,dir,front,side,postwall)
    return snapshot(dll)
def snapshot(dll):
    return {'end':[dll.value(i) for i in (0,1,2,3,4)],'pivot_at':dll.value(6),
            'departure_calls':dll.value(7),'departure_start':dll.value(8),'departure_end':dll.value(9),
            'acc_calls':dll.value(12),'save_calls':dll.value(13),'save_at':dll.value(14),'point_calls':dll.value(18),
            'disbf180':dll.value(16),'afterturn_flag':dll.value(15),'command_errors':dll.value(19),'pivot_count':dll.value(20),
            'turns':[[dll.turn(i,f) for f in range(5)] for i in range(dll.value(11))],
            'nodes':[[dll.node(i,f) for f in range(4)] for i in range(dll.value(10))]}
checks=0;traces=[]
patterns={'immediate_left':[3],'immediate_right':[1],'straight_then_left':[0,3],'immediate_LL':[3,3]}
for name,pattern in patterns.items():
    for direction in range(4):
        path=path_for(direction,pattern)
        for front in (0,1):
            for side in range(4):
                for postwall in (0,1):
                    prior=run(libraries['baseline'],path,direction,front,side,postwall,name=='immediate_right')
                    assert prior['departure_calls']==0 and prior['pivot_count']==1
                    for extra in (0,2,4,6):
                        observed=run(libraries['extra_%dmm'%extra],path,direction,front,side,postwall,name=='immediate_right')
                        dep=(55 if front else 40)+extra
                        assert observed['departure_calls']==observed['acc_calls']==observed['pivot_count']==1
                        assert observed['departure_end']-observed['pivot_at']==dep
                        assert observed['nodes'][0][3]-observed['pivot_at']==dep
                        assert observed['point_calls']==0 and observed['save_calls']==1 and observed['save_at']==observed['pivot_at']
                        assert observed['afterturn_flag']==0 and observed['command_errors']==0
                        assert observed['end']==prior['end'] and observed['end'][:2]==list(path[-1])
                        assert [t[0] for t in observed['turns']]==[t[0] for t in prior['turns']]
                        assert [t[1]-p[1] for t,p in zip(observed['turns'],prior['turns'])]==[dep]*len(prior['turns'])
                        assert [n[:3] for n in observed['nodes']]==[n[:3] for n in prior['nodes']]
                        assert len(observed['nodes'])==len(path)-1
                        assert all(t[2]==30 for t in observed['turns'])
                        if name=='immediate_left':assert observed['turns'][0][1]-observed['pivot_at']==dep+7 and observed['turns'][0][4]==1
                        if name=='immediate_right':assert observed['turns'][0][1]-observed['pivot_at']==dep+6
                        if direction==0 and front==1 and side==0 and postwall==0:
                            traces.append({'case':name,'extra_mm':extra,'baseline_first_curve_after_pivot_mm':prior['turns'][0][1]-prior['pivot_at'],
                                           'departure_mm':dep,'first_curve_after_pivot_mm':observed['turns'][0][1]-observed['pivot_at'],
                                           'coordinate_commit_after_pivot_mm':observed['nodes'][0][3]-observed['pivot_at'],'end_xy_dir':observed['end'][:3]})
                        checks+=1
# Different side IR exit corrections must retain baseline behavior after the first turn.
for sample in (20,30,40,60,100):
    path=path_for(0,[3,3]);prior=run(libraries['baseline'],path,0,1,0,1,0,sample)
    for extra in (0,2,4,6):
        actual=run(libraries['extra_%dmm'%extra],path,0,1,0,1,0,sample)
        assert actual['end']==prior['end']
        assert [t[1]-p[1] for t,p in zip(actual['turns'],prior['turns'])]==[55+extra]*2;checks+=1
# Exercise real front-wall approach control, including readings above 60 mm
# where the original ISR first uses the side-wall approach branches.
for front_distance in (30,50,90,140):
    for side in range(4):
        path=path_for(0,[3]);prior=run(libraries['baseline'],path,0,1,side,0,front_distance=front_distance)
        for extra in (0,2,4,6):
            actual=run(libraries['extra_%dmm'%extra],path,0,1,side,0,front_distance=front_distance)
            assert actual['turns'][0][1]-actual['pivot_at']==62+extra
            assert actual['pivot_at']==prior['pivot_at'] and actual['end']==prior['end'];checks+=1
# Ordinary turns, non-goal dead ends, sprint-active goal turns and the special
# turnaround entry must remain exactly as the preserved source in this model.
for extra in (0,2,4,6):
    dll=libraries['extra_%dmm'%extra]
    for mode in (1,2,3):
        for front in (0,1):
            for side in range(4):
                original=libraries['baseline'];original.reset(0,front,side,0,0,40);dll.reset(0,front,side,0,0,40)
                assert original.back(mode)==dll.back(mode)==0
                assert snapshot(dll)==snapshot(original),(extra,mode,front,side,snapshot(dll),snapshot(original));checks+=1
    for right in (0,1):
        for previous in range(4):
            original=libraries['baseline'];original.reset(0,0,0,0,right,40);dll.reset(0,0,0,0,right,40)
            assert original.ordinary(right,previous)==dll.ordinary(right,previous)==0
            assert snapshot(dll)==snapshot(original);checks+=1
    for dis in (0,34,35,36,74,75,90):
        dll.reset(0,0,0,0,0,40);assert dll.distance_case(dis,40)==0
        assert dll.value(9)==max(40,dis-35)+extra and dll.value(15)==0;checks+=1
report={'status':'PASS','checks':checks,'representative_traces':traces,
        'method':'Extracted actual production before180turn, taskturnaround with its trajectory arrays and ISR, taskacc, goal departure, coordinate update, normal taskturn/before90turn, mouseGoahead, mapStepEdit and objectGoTo. Wait loops alone call a host ISR shim. Encoder and front/side IR are synthetic; yaw tracks its reference and settles ideally; curve translation is omitted and wall-map sensing/Flash writes are recorded stubs. This verifies control flow and encoder targets, not physical turning clearance or hardware dynamics.'}
(OUT/'goal_departure_test_results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'status':'PASS','checks':checks,'traces':len(traces)},ensure_ascii=True))

# Additional V6 behavior checks use the actual linked slow/normal C runners.
import math,random
continuous_checks=0;continuous_traces=[]
patterns={
 'LRL':[1,2,1], 'RLR':[2,1,2], 'LLL':[1,1,1], 'RRR':[2,2,2],
 'L_straight_RL':[1,0,2,1], 'R_straight_LR':[2,0,1,2],
 'single_L':[1], 'single_R':[2], 'long_alternating':[1,2,1,2,1,2,1],
 'restarts':[1,2,0,1,2,0,2],
}
def run_series(dll,actions,direction,stage=2,active=0,wall=0):
    dll.reset(direction,0,0,0,0,40)
    dll.straight_wall(wall)
    dll.series.argtypes=[C.POINTER(C.c_ubyte),C.c_int,C.c_int,C.c_int]
    code=dll.series((C.c_ubyte*len(actions))(*actions),len(actions),stage,active)
    assert code==0,('series stopped',code,actions,direction,stage,active)
    return snapshot(dll)
for extra in (0,2,4,6):
    dll=libraries['extra_%dmm'%extra]
    for direction in range(4):
        for name,actions in patterns.items():
            prior=run_series(libraries['v5_baseline'],actions,direction)
            actual=run_series(dll,actions,direction)
            assert actual==prior,('changed geometry/state',name,extra,direction,actual,prior)
            previous=0;index=0;observed=[]
            for action in actions:
                if action==0:previous=0;continue
                slow=int(previous in (1,2))
                expected_ticks=(210 if action==1 else 215) if slow else (189 if action==1 else 193)
                profile=[dll.profile(index,f) for f in range(6)]
                assert profile==[slow,expected_ticks,63 if slow else 70,90 if action==1 else -90,0 if action==1 else 2,98 if slow else 109],(name,index,profile,previous)
                observed.append(profile[:3]);previous=action;index+=1
            if extra==4 and direction==0:continuous_traces.append({'case':name,'actions':actions,'profiles_slow_ticks_speed':observed})
            continuous_checks+=1
    # All non-search states and active sprint bypasses retain V5 curves.
    for stage,active in [(0,0),(1,0),(3,0),(4,0),(5,0),(6,0),(2,1),(4,1),(5,1)]:
        actions=[1,2,1,0,2,1]
        prior=run_series(libraries['v5_baseline'],actions,0,stage,active)
        actual=run_series(dll,actions,0,stage,active)
        assert actual==prior
        for i in range(dll.value(11)):
            assert dll.profile(i,0)==0 and dll.profile(i,2)==70
            assert dll.profile(i,1)==libraries['v5_baseline'].profile(i,1)
        continuous_checks+=1
    # A predecessor 180 or a straight must keep the first turn normal.
    for previous in (0,3):
        for right in (0,1):
            dll.reset(0,0,0,0,right,40)
            assert dll.ordinary(right,previous)==0
            assert dll.profile(0,0)==0 and dll.profile(0,2)==70
            continuous_checks+=1
    # The first left turn after an actual goal turnaround stays on the old curve;
    # the immediately following left is the first slowed curve.
    path=path_for(0,[3,3]);run(dll,path,0,1,0,0)
    assert [dll.profile(i,0) for i in range(2)]==[0,1]
    assert [dll.profile(i,1) for i in range(2)]==[189,210]
    assert dll.value(9)-dll.value(6)==55+extra
    continuous_checks+=1
    # Repeat the reset test through the actual has-wall straight branch.
    for actions in ([1,0,2,1],[2,0,1,2]):
        assert run_series(dll,actions,0,wall=1)==run_series(libraries['v5_baseline'],actions,0,wall=1)
        assert [dll.profile(i,0) for i in range(3)]==[0,0,1]
        continuous_checks+=1
    # Actual ordinary/special turnaround breaks the previous turn chain.
    for mode in (1,3):
        dll.reset(0,0,0,0,0,40)
        assert dll.ordinary(0,0)==0 and dll.back(mode)==0 and dll.ordinary(0,0)==0
        assert dll.profile(0,0)==dll.profile(1,0)==0
        continuous_checks+=1
rng=random.Random(20261029)
for case_index in range(100):
    actions=[rng.choice((0,1,2)) for _ in range(7)]
    direction=case_index%4;dll=libraries['extra_4mm']
    assert run_series(dll,actions,direction)==run_series(libraries['v5_baseline'],actions,direction)
    previous=0;i=0
    for a in actions:
        if a==0:previous=0;continue
        assert dll.profile(i,0)==int(previous in (1,2));previous=a;i+=1
    continuous_checks+=1

# Independently integrate the compiled source tables against the original angle
# references. This tests spatial endpoints, not only the scaling recipe.
table_source=clean(ROOT/'UTILITY/search_turn_v6.h')
def read_array(src,name):
    m=re.search(r'(?:static\s+)?const short\s+'+name+r'\[\d+\]\s*=\s*\{([^}]*)\}',src);assert m,name
    return list(map(int,re.findall(r'-?\d+',m[1])))
geometry=[]
for side,n,m in [('left',189,210),('right',193,215)]:
    g=read_array(table_source,'v6_'+side+'_gyro');vi=read_array(table_source,'v6_'+side+'_inner');vo=read_array(table_source,'v6_'+side+'_outer')
    original=read_array(user,'gyro_l_search' if side=='left' else 'gyro_r_search')
    assert len(g)==len(vi)==len(vo)==m+2
    assert g[0]==original[0] and g[m-1]==original[n-1]
    assert all(vi[k]+vo[k]==126 for k in range(1,m+1))
    assert max(vo[:m+1])==98 and min(vi[:m+1])==28
    def endpoint(speed,angle,ticks):
        return [sum(speed[k]*.01*f(math.radians(angle[k]-angle[0])) for k in range(ticks)) for f in (math.sin,math.cos)]
    original_end=endpoint([70]*n,original,n);slow_end=endpoint([(vi[k+1]+vo[k+1])/2 for k in range(m)],g,m)
    error=math.hypot(*[a-b for a,b in zip(original_end,slow_end)])
    assert error<1.0 and 1.10<m/n<1.12
    geometry.append({'side':side,'original_ticks':n,'slow_ticks':m,'endpoint_difference_mm':error,'reference_end_degrees':g[m-1]})
    continuous_checks+=1
v6_report={'status':'PASS','checks':continuous_checks,'goal_regression_checks':checks,
 'representative_traces':continuous_traces,'geometry':geometry,
 'method':'Extracted actual V6 selector, slow runner, left/right entry functions and original timer TURN case; actual V5 baseline for comparison. Full goal departure/ordinary-turn/straight/mazeSearch/objectGoTo flow; hardware sensors, encoder and yaw are synthetic. Curve translation is omitted in C state comparison, then independently estimated from wheel speed and angle reference tables. Covers second-and-later turns, straight resets, four headings/goal targets, sprint and non-search exclusions. Physical grip and clearance need track testing.'}
(OUT.parent/'continuous_turn_test_results.json').write_text(json.dumps(v6_report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'status':'PASS','goal_checks':checks,'continuous_checks':continuous_checks,'geometry':geometry},ensure_ascii=True))

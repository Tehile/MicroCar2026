
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

#define MAZESEARCH 2
#define V6_TURN_CHAIN host_v6_chain
u8 GucMouseTask=4,host_v6_chain;

u8 v6UseSlowSearchTurn(void);
void v6SlowSearchTurn90(u8 d){parameter_errors++;taskturn(d==1?101:102);}

#define Stright 0
#define TurnLeft 1
#define TurnRight 2
#define TURN180 3
#define LT90SEARCH 101	
#define	RT90SEARCH 102
#define POINTR90 103
#define POINTL90 104
#define STRAIGHT_spurt_90 105
#define LT90SPURT 106
#define	RT90SPURT 107
#define LT90L 1	  
#define	RT90L 2   
#define LV90L 3   
#define RV90L 4    
#define LT45L 5    
#define RT45L 6     
#define LV45L 7    
#define RV45L 8     
#define LT135L 9    
#define RT135L 10    
#define LV135L 11    
#define RV135L 12  
#define L180L  13    
#define R180L  14
#define STRAIGHT90L 15  
#define STRAIGHT45L 16  
#define LT90M 21	
#define	RT90M 22
#define LV90M 23
#define RV90M 24
#define LT45M 25
#define RT45M 26
#define LV45M 27
#define RV45M 28
#define LT135M 29
#define RT135M 30
#define LV135M 31
#define RV135M 32
#define L180M  33
#define R180M  34
#define STRAIGHT90M 35
#define STRAIGHT45M 36
#define LT90H 41	
#define	RT90H 42
#define LV90H 43
#define RV90H 44
#define LT45H 45
#define RT45H 46
#define LV45H 47
#define RV45H 48
#define LT135H 49
#define RT135H 50
#define LV135H 51
#define RV135H 52
#define L180H  53
#define R180H  54
#define STRAIGHT90H 55
#define STRAIGHT45H 56
#define LOW_90 60
#define LOW 61
#define MEDIUM 62
#define HIGH 63
#define BOTHSIDE 80
#define LEFTSIDE 81
#define RIGHTSIDE 82
#define NOSIDE		83
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
#define spurt_plan_start_dir action_correct_sequence[196]
#define spurt_exec_index     action_correct_sequence[197]
#define spurt_search_active  action_correct_sequence[198]
#define spurt_search_failed  action_correct_sequence[199]
static u8 spurtSearchRunning(void);

void parameter_search(void)
{
	kpl=13;kil=0.74;kdl=0.9;	
	kpr=13;kir=0.74;kdr=0.9;	
	speed_ss = 80;		
	
	keep_dis = 12;		
	turn_delay = 9;	
}
void parameter_spurt(void)
{
	kpl=13;kil=0.7;kdl=0.8;	
	kpr=13;kir=0.7;kdr=0.8;	
	speed_ss = 100;		
	speed_turn = 70;	
	keep_dis = 12;		
	turn_delay = 0;	
}
void mapStepEdit (char  cX, char  cY)
{
    uchar n         = 0;                                                 
    uchar ucStep    = 0;                                                 
    uchar ucStat    = 0;                                                 
    uchar i,j;
    
    GmcStack[n].cX  = cX;                                                
    GmcStack[n].cY  = cY;                                                
    n++;
     
    for (i = 0; i < MAZETYPE; i++)
	{
        for (j = 0; j < MAZETYPE; j++)
		{
            GucMapStep[i][j] = 0xff;
        }
    }
     
    while (n)
	{
#ifdef MOUSE_REMOTE
        
        if (LP_AbortRequested()) return;
#endif
        GucMapStep[cX][cY] = ucStep++;                                   

         
        ucStat = 0;
        if ((GucMapBlock[cX][cY] & 0x01) &&                              
            (GucMapStep[cX][cY + 1] > (ucStep))) {                       
            ucStat++; 
        }
        if ((GucMapBlock[cX][cY] & 0x02) &&                              
            (GucMapStep[cX + 1][cY] > (ucStep))) {                       
            ucStat++;                                                    
        }
        if ((GucMapBlock[cX][cY] & 0x04) &&
            (GucMapStep[cX][cY - 1] > (ucStep))) {
            ucStat++;                                                    
        }
        if ((GucMapBlock[cX][cY] & 0x08) &&
            (GucMapStep[cX - 1][cY] > (ucStep))) {
            ucStat++;                                                    
        }
         
        if (ucStat == 0) {
          
					n--;
            cX = GmcStack[n].cX;
            cY = GmcStack[n].cY;
            ucStep = GucMapStep[cX][cY];
        } else {
					  
            if (ucStat > 1) {                                            
                GmcStack[n].cX = cX;                                     
                GmcStack[n].cY = cY;                                     
                n++;
            }
             
            if ((GucMapBlock[cX][cY] & 0x01) &&                          
                (GucMapStep[cX][cY + 1] > (ucStep))) {                   
                cY++;                                                    
                continue;
            }
            if ((GucMapBlock[cX][cY] & 0x02) &&                          
                (GucMapStep[cX + 1][cY] > (ucStep))) {                   
                cX++;                                                    
                continue;
            }
            if ((GucMapBlock[cX][cY] & 0x04) &&                          
                (GucMapStep[cX][cY - 1] > (ucStep))) {                   
                cY--;                                                    
                continue;
            }
            if ((GucMapBlock[cX][cY] & 0x08) &&                          
                (GucMapStep[cX - 1][cY] > (ucStep))) {                   
                cX--;                                                    
                continue;
            }
        }
    }
}
static void gridPath90(uchar GX,uchar GY)
{
    static const signed char dx[4]={0,1,0,-1};
    static const signed char dy[4]={1,0,-1,0};
    int x=GmcMouse.cX,y=GmcMouse.cY,nx,ny;
    u16 i,j;
    u8 heading=GucMouseDir,dir,candidate,chosen;
    FastwayCount_90=0;
    for(i=0;i<MAZETYPE;i++)
        for(j=0;j<MAZETYPE;j++) Mapfastway_90[i][j]=0xff;
    if(x<0 || y<0 || x>=MAZETYPE || y>=MAZETYPE ||
       GX>=MAZETYPE || GY>=MAZETYPE || heading>3) return;
    for(i=0;i<MAZETYPE;i++)
        if((GucMapBlock[i][0]&(1<<DOWN)) ||
           (GucMapBlock[i][MAZETYPE-1]&(1<<UP)) ||
           (GucMapBlock[0][i]&(1<<LEFT)) ||
           (GucMapBlock[MAZETYPE-1][i]&(1<<RIGHT))) return;
    mapStepEdit(GX,GY);
    while(x!=GX || y!=GY)
    {
        if(FastwayCount_90>=254 || GucMapStep[x][y]==0xff) goto invalid;
        chosen=0xff;
        for(candidate=0;candidate<5;candidate++)
        {
            dir=(candidate==0)?heading:(candidate-1);
            nx=x+dx[dir];ny=y+dy[dir];
            if(nx<0 || ny<0 || nx>=MAZETYPE || ny>=MAZETYPE) continue;
            if((GucMapBlock[x][y]&(1<<dir)) &&
               GucMapStep[nx][ny]<GucMapStep[x][y])
            {
                chosen=dir;break;
            }
        }
        if(chosen==0xff) goto invalid;
        i=FastwayCount_90++;
        FastwayAction_90[i].cx=(char)x;
        FastwayAction_90[i].cy=(char)y;
        FastwayAction_90[i].action=(4+chosen-heading)%4;
        Mapfastway_90[x][y]=chosen;
        heading=chosen;x+=dx[chosen];y+=dy[chosen];
    }
    i=FastwayCount_90++;
    FastwayAction_90[i].cx=(char)x;
    FastwayAction_90[i].cy=(char)y;
    FastwayAction_90[i].action=0;
    return;
invalid:
    FastwayCount_90=0;
}

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
case STRAIGHTEYECLOSECOORADJ:	 
 
		eh=e;
		e=gangle;
		setvl=speed_ss-e*0.25;		
		setvr=speed_ss+e*0.25;
		#ifdef COORADJUST
		 
		 
		{												
			if(lefthaswallh==1&&lefthaswall==0){settrack(trackcoor);}
			if(righthaswallh==1&&righthaswall==0){settrack(trackcoor);}
		}
		#endif
		break;
		
 
	case STRAIGHTPOLE:			 
 
 
 
 
 
 
 
 
 
		e=gangle;
		setvl=speed_ss-e*0.5;		
		setvr=speed_ss+e*0.5;

		#ifdef COORADJUST
		 
		{
			if(lefthaswallh==1 && lefthaswall==0) settrack(trackcoor);
			if(righthaswallh==1 && righthaswall==0) settrack(trackcoor);
		}
		#endif
		break;	

 
 
 
 
 
 
 
	
    default:break;
    }
}

void ref_mazesearchhaswall()		 
{
	turno=0;
	if(disf<50) 
	{
	 	flashwrite=0;
		setvl=0;setvr=0;
		task=NULL;
		delay_ms(300);
		sleepmode=1;
		while(1){sleepmode=1;ledoff(0);delay_ms(500);ledon(1);ledon(2);delay_ms(500);}
	}
	
	if(LastSearchAction!=Stright) settrack(track+20);	 
	LastSearchAction=Stright;
	 
	while(track<120+8){task=SMARTSTRAIGHTFAST;sim_tick();}			 
	eh=0;e=0;
	while(track<180){task=STRAIGHTEYECLOSECOORADJ;sim_tick();}		 
}

void ref_mazesearchnowall()			 
{
	turno=0;
	if(disf<50) 
	{
	 	flashwrite=0;
		setvl=0;setvr=0;
		task=NULL;
		delay_ms(300);
		sleepmode=1;
		while(1){sleepmode=1;ledoff(0);delay_ms(500);ledon(3);ledon(4);delay_ms(500);}
	}
	if(LastSearchAction!=Stright) settrack(track+20);
	LastSearchAction=1;
	 

	while(track<120+8){task=GOSTRAIGHT;sim_tick();}

	dislpeak=200;
	disrpeak=200;
	lpeakfindenable=0;
	rpeakfindenable=0;
	leftpeaktrack=0;
	rightpeaktrack=0;
	tick=0;
	task=STRAIGHTPOLE;
	while(track<180){sim_tick();} 
}

void ref_mouseGoahead(char cNBlock)	 
{	   
	u8 n=0;			 
	goaheadbugflag=1;
	eh=0;
	e=0;
	gangleh=0;
	ganglehh=0;
	peakgangle=0;
	peakgangleh=0;
	task=SMARTSTRAIGHT;
	while(1) { if(track>90 && track<180)sim_tick();
		if(track>=180) 
		{	
			blocknum++; 
			settrack(0);
			__mouseCoorUpdate();
			n++;
			if(n==cNBlock)
			{
				blocknum=1;
				break;
			}
		}
		if(track>90&&track<180); 
		else
		{	  
			if(lefthaswall==1 || righthaswall==1) ref_mazesearchhaswall();	 
			else if(lefthaswall==0 && righthaswall==0) ref_mazesearchnowall();
		}
	}
}
void mazesearchhaswall(void){ref_mazesearchhaswall();}
void mazesearchnowall(void){ref_mazesearchnowall();}

void settrack(int t) 
{
	basetrack=absolutetrack-t;
	track=t;
}

void before90turn(u8 Direction)				 
{
 
 
 
		e=0;eh=0;
		task=BEFORE90TURN;
		while(1) { sim_tick();
			if(spurt_search_active==1 && !spurtSearchRunning()) return;
 
		
			if(LastSearchAction==TURN180)					 
			{
				if(Direction==TurnLeft)						 
				{
					if(track>=turn_delay+27) 
					{
						turno=0; break;
					}
				}
				else
				{
					if(track>=turn_delay+20)						 
					{
						turno=0; break;
					}
				}
			} 
			
			
			if(LastSearchAction==Stright)					 
			{
				if(Direction==TurnLeft)						 
				{
					if(track>=turn_delay-2)
					{
						turno=0; break;
					}
				}
				else
				{
					if(track>=turn_delay-3)						 
					{
						turno=0; break;
					}
				}
			}
			else if(LastSearchAction==TurnLeft)
			{
				if(Direction==TurnLeft)						 
				{
					if(track>=turn_delay)
					{
						turno=0; break;
					}
				}
				else
				{
					if(track>=turn_delay-5)						  
					{
						turno=0; break;
					}
				}
			}
			else if(LastSearchAction==TurnRight)
			{
				if(Direction==TurnLeft)					 
				{
					if(track>=turn_delay-4)    
					{
						turno=0; break;
					}
				}
				else
				{
					if(track>=turn_delay-1)						 
					{
						turno=0; break;
					}
				}
			}
		}
 
}

void mouseTurnright()
{	 
	goaheadbugflag=0;
	before90turn(TurnRight);
	if(spurt_search_active==1 && (!spurtSearchRunning() || disr<50))
	{
		spurt_search_failed=1;
		return;
	}
	LastSearchAction=TurnRight;
	if(lefthaswall==1)
	{
		turnrighto=dis2wall-((disl+dislh+dislhh)/3);
		turno=(disr+disrh+disrhh)/3-dis2wall;
	}
	else {turnrighto=0;turno=0;}
	if(disr<50) 
	{
		flashwrite=0;
	 	setvl=0;setvr=0;
		task=NULL;
		delay_ms(50);
		sleepmode=1;
		while(1){sleepmode=1;ledoff(0);delay_ms(500);ledon(7);ledon(8);delay_ms(500);}
	}
	taskturn(RT90SEARCH);
	if(spurt_search_active==1 && !spurtSearchRunning()) return;
	task=SMARTSTRAIGHT;
	GucMouseDir=(GucMouseDir + 1) % 4; 
	settrack(180-turnrighto); 

 
}

void mouseTurnleft()
{	 
	goaheadbugflag=0;
	before90turn(TurnLeft);
	if(spurt_search_active==1 && (!spurtSearchRunning() || disl<50))
	{
		spurt_search_failed=1;
		return;
	}
	LastSearchAction=TurnLeft;
	if(righthaswall==1)
	{
		turnlefto=(disr+disrh+disrhh)/3-dis2wall;
		turno=(disr+disrh+disrhh)/3-dis2wall;
	}
	else {turnlefto=0;turno=0;}
	if(disl<50) 
	{
	 	flashwrite=0;
		setvl=0;setvr=0;
		task=NULL;
	delay_ms(50);
		sleepmode=1;
		while(1){sleepmode=1;ledoff(0);delay_ms(500);ledon(5);ledon(6);delay_ms(500);}
	}		   	
	taskturn(LT90SEARCH);
	if(spurt_search_active==1 && !spurtSearchRunning()) return;
	task=SMARTSTRAIGHT;
	GucMouseDir=(GucMouseDir + 3) % 4;
	settrack(180+turnlefto);


}

static u8 spurtSearchRunning(void)
{
#ifdef MOUSE_REMOTE
    if(LP_AbortRequested()) return 0;
#endif
    return (isemergency==0 && sleepmode==0 && spurt_search_failed==0);
}

static u8 spurtSearchWaitTrack(long distance,u8 motion)
{
    task=motion;
    while(track<distance)
    {
        if(!spurtSearchRunning()) return 0;
    }
    return spurtSearchRunning();
}

static void gridSearchParameters(void)
{
    parameter_search();
    speed_turn=30;
    turn_kp=4;turn_kd=4;
}

static u8 gridFastStraight90(u16 distance,u8 phase)
{
    u16 acc=(u16)(SpurtAccRatio45*100.0f);
    u16 dec=(u16)(SpurtDecRatio45*100.0f);
    int initial=speed,target,braking;
    long at;
    if(initial<10) initial=10;
    if(acc==0) acc=1;
    if(dec==0) dec=1;
    e=0;eh=0;turno=0;
    settrack(0);
    while(track<distance)
    {
        sim_tick();
        if(!spurtSearchRunning()) return 0;
        at=track;
        target=initial+(at*acc)/100;
        braking=LBottomSpeed+((distance-at)*dec)/100;
        if(target>braking) target=braking;
        if(target>LTopSpeed) target=LTopSpeed;
        if(target<10) target=10;
        speed_ss=(u16)target;
        if((at+phase)%180>20 && (at+phase)%180<100) task=SPURT90BLIND;
        else task=(lefthaswall || righthaswall)?SPURT90IR:SPURT90BLIND;
        if(disf<20) isemergency=1;
    }
    return spurtSearchRunning();
}

static u8 gridStraight90(u16 cells)
{
    u16 distance=180*cells;
    u8 phase=(LastSearchAction!=Stright)?20:0;
    
    distance-=phase;
    parameter_spurt();
    speedmode=LOW;
    SPURT90IR_kp1=0.17;SPURT90IR_kp2=0.1;SPURT90IR_kd=0;
    SPURT90GYRO_kp=0.3;SPURT90GYRO_kd=0;
    if(!gridFastStraight90(distance-80,phase)) return 0;

    
    gridSearchParameters();
    LastSearchAction=Stright;
    settrack(100);
    if(lefthaswall || righthaswall) mazesearchhaswall();
    else mazesearchnowall();
    if(!spurtSearchRunning()) return 0;
    LastSearchAction=Stright;
    goaheadbugflag=0;
    settrack(0);
    return 1;
}

u8 spurt_grid90(void)
{
    static const signed char dx[4]={0,1,0,-1};
    static const signed char dy[4]={1,0,-1,0};
    u16 i,next;
    u8 dir=GucMouseDir,code,ok=0;
    int nx,ny;

    
    for(i=0;i<200;i++) {actionsequence[i]=0;action_correct_sequence[i]=0;}
    spurt_exec_index=200;
    if(FastwayCount_90==0 || dir>3 || sleepmode || isemergency) goto finish;
    if(FastwayAction_90[0].cx!=GmcMouse.cX ||
       FastwayAction_90[0].cy!=GmcMouse.cY) goto finish;
    for(i=0;i+1<FastwayCount_90;i++)
    {
        code=FastwayAction_90[i].action;
        if(code>3) goto finish;
        dir=(dir+code)%4;
        nx=FastwayAction_90[i].cx;ny=FastwayAction_90[i].cy;
        if(nx<0 || ny<0 || nx>=MAZETYPE || ny>=MAZETYPE ||
           !(GucMapBlock[nx][ny]&(1<<dir))) goto finish;
        nx+=dx[dir];ny+=dy[dir];
        if(nx<0 || ny<0 || nx>=MAZETYPE || ny>=MAZETYPE ||
           FastwayAction_90[i+1].cx!=nx || FastwayAction_90[i+1].cy!=ny) goto finish;
    }
    if(FastwayAction_90[FastwayCount_90-1].cx!=GoalX ||
       FastwayAction_90[FastwayCount_90-1].cy!=GoalY ||
       FastwayAction_90[FastwayCount_90-1].action!=0) goto finish;

    spurt_search_active=1;
    spurt_action_num=0;
    ObjectGoTo_Flag=0;GoOrBackFlag=0;
    spurt_45_end_flag=0;
    sidewall_threshold=160;
    goaheadbugflag=0;
    LastSearchAction=Stright;
    gridSearchParameters();
    i=0;
    while(i+1<FastwayCount_90)
    {
        code=FastwayAction_90[i].action;
        if(code==0)
        {
            next=i+1;
            while(next+1<FastwayCount_90 && FastwayAction_90[next].action==0) next++;
            if(!gridStraight90(next-i)) goto finish;
            spurt_action_num+=(next-i);
            i=next;
        }
        else
        {
            gridSearchParameters();
            if(code==1) mouseTurnright();
            else if(code==3) mouseTurnleft();
            else mouseTurnback();
            if(!spurtSearchRunning()) goto finish;
            
            if(code!=2 && !spurtSearchWaitTrack(180,SMARTSTRAIGHT)) goto finish;
            settrack(0);
            i++;spurt_action_num++;
        }
        GmcMouse.cX=FastwayAction_90[i].cx;
        GmcMouse.cY=FastwayAction_90[i].cy;
        
    }
    decdistance=30;
    taskstop();
    ok=1;
finish:
    spurt_search_active=0;
    spurt_search_failed=(ok?0:1);
    task=NULL;setvl=0;setvr=0;
    sleepmode=1;
    settrack(0);
    return ok;
}

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

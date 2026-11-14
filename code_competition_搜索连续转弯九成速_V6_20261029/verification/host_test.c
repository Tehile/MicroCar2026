
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
void settrack(int t) 
{
	basetrack=absolutetrack-t;
	track=t;
}

void before90turn(u8 Direction)				 
{
 
 
 
		e=0;eh=0;
		task=BEFORE90TURN;
		while(1)
		{
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

static u8 spurtIsSearchUturn(u8 pattern)
{
    return (pattern==L180L || pattern==R180L || pattern==L180M ||
            pattern==R180M || pattern==L180H || pattern==R180H);
}

static void spurtSearchPose(u16 source,u8 direction)
{
    GmcMouse.cX=FastwayAction[source].cx;
    GmcMouse.cY=FastwayAction[source].cy;
    GucMouseDir=direction;
}

static u16 spurtSourceForAction(u16 index)
{
    u16 n,source=1;
    u8 code;
    if(index>=200) return 256;
    for(n=0;n<index;n++)
    {
        code=actionsequence[n];
        if(code>=41 && code<=56) code-=40;
        else if(code>=21 && code<=36) code-=20;
        switch(code)
        {
        case STRAIGHT90L:case STRAIGHT45L:case LV45L:case RV45L:
            source++;break;
        case LT90L:case RT90L:case LV90L:case RV90L:
        case LT45L:case RT45L:case LV135L:case RV135L:
            source+=2;break;
        case LT135L:case RT135L:case L180L:case R180L:
            source+=3;break;
        default:return 256;
        }
    }
    return source;
}

static void spurtSearchParameters(u8 save)
{
#define SPURT_SEARCH_COPY(field,offset) \
    if(save) memcpy(action_correct_sequence+offset,&field,sizeof(field)); \
    else memcpy(&field,action_correct_sequence+offset,sizeof(field))
    SPURT_SEARCH_COPY(kpl,0);
    SPURT_SEARCH_COPY(kil,8);
    SPURT_SEARCH_COPY(kdl,16);
    SPURT_SEARCH_COPY(kpr,24);
    SPURT_SEARCH_COPY(kir,32);
    SPURT_SEARCH_COPY(kdr,40);
    SPURT_SEARCH_COPY(turn_kp,48);
    SPURT_SEARCH_COPY(turn_kd,56);
    SPURT_SEARCH_COPY(speed_ss,64);
    SPURT_SEARCH_COPY(speed_turn,66);
    SPURT_SEARCH_COPY(speed_fastss,68);
    SPURT_SEARCH_COPY(turn_delay,70);
    SPURT_SEARCH_COPY(keep_dis,71);
    SPURT_SEARCH_COPY(sidewall_threshold,72);
    SPURT_SEARCH_COPY(LastSearchAction,73);
    SPURT_SEARCH_COPY(goaheadbugflag,74);
#undef SPURT_SEARCH_COPY
}

static void spurtSearchUturn(u8 pattern)
{
    static const signed char dx[4]={0,1,0,-1};
    static const signed char dy[4]={1,0,-1,0};
    u16 source,n;
    u8 right,entrydir,middir,exitdir,ok=0;
    int heading=0;
    long begintrack,exitdistance;

    spurtSearchParameters(1);
    
    if(spurt_exec_index>=200) goto restore;
    source=spurtSourceForAction(spurt_exec_index);
    if(source<1 || source+2>=FastwayCount) goto restore;
    right=(pattern==R180L || pattern==R180M || pattern==R180H);
    if(FastwayAction[source].action!=(right?1:7) ||
       FastwayAction[source+1].action!=(right?2:6) ||
       FastwayAction[source+2].action!=(right?1:7)) goto restore;
    for(n=0;n<source;n++) heading+=FastwayAction[n].action;
    if(heading%2!=0) goto restore;
    entrydir=(spurt_plan_start_dir+heading/2)%4;
    middir=(entrydir+(right?1:3))%4;
    exitdir=(entrydir+2)%4;
    if(FastwayAction[source+1].cx!=FastwayAction[source].cx+dx[middir] ||
       FastwayAction[source+1].cy!=FastwayAction[source].cy+dy[middir] ||
       FastwayAction[source+2].cx!=FastwayAction[source+1].cx+dx[exitdir] ||
       FastwayAction[source+2].cy!=FastwayAction[source+1].cy+dy[exitdir]) goto restore;
    if(!spurtSearchRunning()) goto restore;

    spurt_search_active=1;
    parameter_search();
    
    speed_turn=30;
    turn_kp=4;
    turn_kd=4;
    sidewall_threshold=160;
    LastSearchAction=Stright;
    goaheadbugflag=0;
    e=0;eh=0;esum=0;

    
    exitdistance=180-(long)trackcoor+(long)turn_delay-(right?3:2);
    if(exitdistance<0) goto restore;
    begintrack=absolutetrack;
    task=SMARTSTRAIGHTFAST;
    while(!(right?(righthaswallh==1 && righthaswall==0):
                   (lefthaswallh==1 && lefthaswall==0)))
    {
        if(!spurtSearchRunning() || disf<10 ||
           absolutetrack-begintrack>360) goto restore;
    }
    if(!spurtSearchRunning()) goto restore;
    settrack(trackcoor);
    if(!spurtSearchWaitTrack(180,STRAIGHTEYECLOSECOORADJ)) goto restore;
    settrack(0);
    spurtSearchPose(source,entrydir);

    if(right) mouseTurnright(); else mouseTurnleft();
    if(!spurtSearchRunning()) goto restore;
    
    if(!spurtSearchWaitTrack(180,SMARTSTRAIGHT)) goto restore;
    settrack(0);
    spurtSearchPose(source+1,middir);

    if(right) mouseTurnright(); else mouseTurnleft();
    if(!spurtSearchRunning()) goto restore;
    if(!spurtSearchWaitTrack(180,SMARTSTRAIGHT)) goto restore;
    settrack(0);
    spurtSearchPose(source+2,exitdir);
    if(!spurtSearchWaitTrack(exitdistance,SMARTSTRAIGHTFAST)) goto restore;
    settrack(0);
    ok=1;

restore:
    
    spurtSearchParameters(0);
    spurt_search_active=0;
    e=0;eh=0;esum=0;
    if(ok) task=SMARTSTRAIGHTSPURTL;
    else
    {
        spurt_search_failed=1;
        task=NULL;setvl=0;setvr=0;
#ifdef MOUSE_REMOTE
        LP_AbortRequest();
#endif
        sleepmode=1;
    }
}

void mouseturn(u8 pattern)
{
	if(spurtIsSearchUturn(pattern))
	{
		spurtSearchUturn(pattern);
		ledoff(0);
		return;
	}
	beforemouseturn(pattern);
	taskturn(pattern);
	ledoff(0);
	speed_ss=70; speed_fastss=100;
}

int getsum(int num)
{
	int ret=0,i=0;
	for(i=0;i<=num;i++) ret+=FastwayAction[i].action;
	return ret;
}

void actiongenerate(u8 mode)	 
{
	int sum=0,current=0;
	u16 i=0,j=0;
 
	uchar k=0,dirtemp=0,leftdir=0,rightdir=0;
	for(i=0;i<200;i++)
	{
		actionsequence[i]=0;
		action_correct_sequence[i]=0;
	}
 

	if(FastwayCount<2) return;
	spurt_plan_start_dir=GucMouseDir;
	spurt_exec_index=200;
	spurt_search_active=0;
	spurt_search_failed=0;
	if(mode==LOW)
	{
		for(i=0;i+1<FastwayCount && j<199;i++) 
		{	
			if(i<current)continue;
	
			if(i+3<FastwayCount && FastwayAction[i+1].action==1  && FastwayAction[i+2].action==2 && FastwayAction[i+3].action==1 && getsum(i)%2==0){actionsequence[j++]=R180L;current+=3;}
			else if(i+3<FastwayCount && FastwayAction[i+1].action==7 && FastwayAction[i+2].action==6 && FastwayAction[i+3].action==7 && getsum(i)%2==0){actionsequence[j++]=L180L;current+=3;}
			else if(FastwayAction[i+1].action==2 && FastwayAction[i+2].action==1 && getsum(i)%2==1){actionsequence[j++]=RV135L;current+=2;} 
			else if(FastwayAction[i+1].action==6 && FastwayAction[i+2].action==7 && getsum(i)%2==1){actionsequence[j++]=LV135L;current+=2;}
			else if(FastwayAction[i+1].action==1 && FastwayAction[i+2].action==2 && getsum(i)%2==0){actionsequence[j++]=RT135L;current+=3;} 
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==6 && getsum(i)%2==0){actionsequence[j++]=LT135L;current+=3;}
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==7 && getsum(i)%2==0){actionsequence[j++]=LT90L;current+=2;}
			else if(FastwayAction[i+1].action==1 && FastwayAction[i+2].action==1 && getsum(i)%2==0){actionsequence[j++]=RT90L;current+=2;}
			else if(FastwayAction[i+1].action==2 && getsum(i)%2==1){actionsequence[j++]=RV90L;current+=2;}
			else if(FastwayAction[i+1].action==6 && getsum(i)%2==1){actionsequence[j++]=LV90L;current+=2;}
			else if(FastwayAction[i+1].action==1 && getsum(i)%2==0){actionsequence[j++]=RT45L;current+=2;}
			else if(FastwayAction[i+1].action==7 && getsum(i)%2==0){actionsequence[j++]=LT45L;current+=2;}
			else if(FastwayAction[i+1].action==1 && getsum(i)%2==1){actionsequence[j++]=RV45L;current+=1;}
			else if(FastwayAction[i+1].action==7 && getsum(i)%2==1){actionsequence[j++]=LV45L;current+=1;}
			else if(FastwayAction[i+1].action==0 && getsum(i)%2==0)
			{
				actionsequence[j++]=STRAIGHT90L;
 
		
 
				current+=1; 
			}
			else if(FastwayAction[i+1].action==0 && getsum(i)%2==1)
				{
					actionsequence[j++]=STRAIGHT45L;current+=1;
				}
		}
	}
	else if(mode==MEDIUM)
	{	 
		for(i=0;i+1<FastwayCount && j<199;i++) 
		{	
			if(i<current)continue;
	
			if(i+3<FastwayCount && FastwayAction[i+1].action==1  && FastwayAction[i+2].action==2 && FastwayAction[i+3].action==1 && getsum(i)%2==0){actionsequence[j++]=R180M;current+=3;}
			else if(i+3<FastwayCount && FastwayAction[i+1].action==7 && FastwayAction[i+2].action==6 && FastwayAction[i+3].action==7 && getsum(i)%2==0){actionsequence[j++]=L180M;current+=3;}
			else if(FastwayAction[i+1].action==2 && FastwayAction[i+2].action==1 && getsum(i)%2==1){actionsequence[j++]=RV135M;current+=2;} 
			else if(FastwayAction[i+1].action==6 && FastwayAction[i+2].action==7 && getsum(i)%2==1){actionsequence[j++]=LV135M;current+=2;}
			else if(FastwayAction[i+1].action==1 && FastwayAction[i+2].action==2 && getsum(i)%2==0){actionsequence[j++]=RT135M;current+=3;} 
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==6 && getsum(i)%2==0){actionsequence[j++]=LT135M;current+=3;}
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==7 && getsum(i)%2==0){actionsequence[j++]=LT90M;current+=2;}
			else if(FastwayAction[i+1].action==1 && FastwayAction[i+2].action==1 && getsum(i)%2==0){actionsequence[j++]=RT90M;current+=2;}
			else if(FastwayAction[i+1].action==2 && getsum(i)%2==1){actionsequence[j++]=RV90M;current+=2;}
			else if(FastwayAction[i+1].action==6 && getsum(i)%2==1){actionsequence[j++]=LV90M;current+=2;}
			else if(FastwayAction[i+1].action==1 && getsum(i)%2==0){actionsequence[j++]=RT45M;current+=2;}
			else if(FastwayAction[i+1].action==7 && getsum(i)%2==0){actionsequence[j++]=LT45M;current+=2;}
			else if(FastwayAction[i+1].action==1 && getsum(i)%2==1){actionsequence[j++]=RV45M;current+=1;}
			else if(FastwayAction[i+1].action==7 && getsum(i)%2==1){actionsequence[j++]=LV45M;current+=1;}
			else if(FastwayAction[i+1].action==0 && getsum(i)%2==0){actionsequence[j++]=STRAIGHT90M;current+=1;}
			else if(FastwayAction[i+1].action==0 && getsum(i)%2==1){actionsequence[j++]=STRAIGHT45M;current+=1;}
		}
	}
	else if(mode==HIGH)
	{
		for(i=0;i+1<FastwayCount && j<199;i++)
		{	
			if(i<current)continue;
	
			if(i+3<FastwayCount && FastwayAction[i+1].action==1  && FastwayAction[i+2].action==2 && FastwayAction[i+3].action==1 && getsum(i)%2==0){actionsequence[j++]=R180H;current+=3;}
			else if(i+3<FastwayCount && FastwayAction[i+1].action==7 && FastwayAction[i+2].action==6 && FastwayAction[i+3].action==7 && getsum(i)%2==0){actionsequence[j++]=L180H;current+=3;}
			else if(FastwayAction[i+1].action==2 && FastwayAction[i+2].action==1 && getsum(i)%2==1){actionsequence[j++]=RV135H;current+=2;} 
			else if(FastwayAction[i+1].action==6 && FastwayAction[i+2].action==7 && getsum(i)%2==1){actionsequence[j++]=LV135H;current+=2;}
			else if(FastwayAction[i+1].action==1 && FastwayAction[i+2].action==2 && getsum(i)%2==0){actionsequence[j++]=RT135H;current+=3;} 
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==6 && getsum(i)%2==0){actionsequence[j++]=LT135H;current+=3;}
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==7 && getsum(i)%2==0){actionsequence[j++]=LT90H;current+=2;}
			else if(FastwayAction[i+1].action==1 && FastwayAction[i+2].action==1 && getsum(i)%2==0){actionsequence[j++]=RT90H;current+=2;}
			else if(FastwayAction[i+1].action==2 && getsum(i)%2==1){actionsequence[j++]=RV90H;current+=2;}
			else if(FastwayAction[i+1].action==6 && getsum(i)%2==1){actionsequence[j++]=LV90H;current+=2;}
			else if(FastwayAction[i+1].action==1 && getsum(i)%2==0){actionsequence[j++]=RT45H;current+=2;}
			else if(FastwayAction[i+1].action==7 && getsum(i)%2==0){actionsequence[j++]=LT45H;current+=2;}
			else if(FastwayAction[i+1].action==1 && getsum(i)%2==1){actionsequence[j++]=RV45H;current+=1;}
			else if(FastwayAction[i+1].action==7 && getsum(i)%2==1){actionsequence[j++]=LV45H;current+=1;}
			else if(FastwayAction[i+1].action==0 && getsum(i)%2==0){actionsequence[j++]=STRAIGHT90H;current+=1;}
			else if(FastwayAction[i+1].action==0 && getsum(i)%2==1){actionsequence[j++]=STRAIGHT45H;current+=1;}
		}
	}
}
void baseline_actiongenerate(u8 mode)	 
{
	int sum=0,current=0;
	u16 i=0,j=0;
 
	uchar k=0,dirtemp=0,leftdir=0,rightdir=0;
	for(i=0;i<200;i++)
	{
		actionsequence[i]=0;
		action_correct_sequence[i]=0;
	}
 

	if(mode==LOW)
	{  	
		for(i=0;i<FastwayCount-1;i++) 
		{	
			if(i<current)continue;
	
			if(FastwayAction[i+1].action==1  && FastwayAction[i+2].action==2 && FastwayAction[i+3].action==1 && getsum(i)%2==0){actionsequence[j++]=R180L;current+=3;}
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==6 && FastwayAction[i+3].action==7 && getsum(i)%2==0){actionsequence[j++]=L180L;current+=3;}
			else if(FastwayAction[i+1].action==2 && FastwayAction[i+2].action==1 && getsum(i)%2==1){actionsequence[j++]=RV135L;current+=2;} 
			else if(FastwayAction[i+1].action==6 && FastwayAction[i+2].action==7 && getsum(i)%2==1){actionsequence[j++]=LV135L;current+=2;}
			else if(FastwayAction[i+1].action==1 && FastwayAction[i+2].action==2 && getsum(i)%2==0){actionsequence[j++]=RT135L;current+=3;} 
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==6 && getsum(i)%2==0){actionsequence[j++]=LT135L;current+=3;}
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==7 && getsum(i)%2==0){actionsequence[j++]=LT90L;current+=2;}
			else if(FastwayAction[i+1].action==1 && FastwayAction[i+2].action==1 && getsum(i)%2==0){actionsequence[j++]=RT90L;current+=2;}
			else if(FastwayAction[i+1].action==2 && getsum(i)%2==1){actionsequence[j++]=RV90L;current+=2;}
			else if(FastwayAction[i+1].action==6 && getsum(i)%2==1){actionsequence[j++]=LV90L;current+=2;}
			else if(FastwayAction[i+1].action==1 && getsum(i)%2==0){actionsequence[j++]=RT45L;current+=2;}
			else if(FastwayAction[i+1].action==7 && getsum(i)%2==0){actionsequence[j++]=LT45L;current+=2;}
			else if(FastwayAction[i+1].action==1 && getsum(i)%2==1){actionsequence[j++]=RV45L;current+=1;}
			else if(FastwayAction[i+1].action==7 && getsum(i)%2==1){actionsequence[j++]=LV45L;current+=1;}
			else if(FastwayAction[i+1].action==0 && getsum(i)%2==0)
			{
				actionsequence[j++]=STRAIGHT90L;
 
		
 
				current+=1; 
			}
			else if(FastwayAction[i+1].action==0 && getsum(i)%2==1)
				{
					actionsequence[j++]=STRAIGHT45L;current+=1;
				}
		}
	}
	else if(mode==MEDIUM)
	{	 
		for(i=0;i<FastwayCount-1;i++) 
		{	
			if(i<current)continue;
	
			if(FastwayAction[i+1].action==1  && FastwayAction[i+2].action==2 && FastwayAction[i+3].action==1 && getsum(i)%2==0){actionsequence[j++]=R180M;current+=3;}
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==6 && FastwayAction[i+3].action==7 && getsum(i)%2==0){actionsequence[j++]=L180M;current+=3;}
			else if(FastwayAction[i+1].action==2 && FastwayAction[i+2].action==1 && getsum(i)%2==1){actionsequence[j++]=RV135M;current+=2;} 
			else if(FastwayAction[i+1].action==6 && FastwayAction[i+2].action==7 && getsum(i)%2==1){actionsequence[j++]=LV135M;current+=2;}
			else if(FastwayAction[i+1].action==1 && FastwayAction[i+2].action==2 && getsum(i)%2==0){actionsequence[j++]=RT135M;current+=3;} 
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==6 && getsum(i)%2==0){actionsequence[j++]=LT135M;current+=3;}
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==7 && getsum(i)%2==0){actionsequence[j++]=LT90M;current+=2;}
			else if(FastwayAction[i+1].action==1 && FastwayAction[i+2].action==1 && getsum(i)%2==0){actionsequence[j++]=RT90M;current+=2;}
			else if(FastwayAction[i+1].action==2 && getsum(i)%2==1){actionsequence[j++]=RV90M;current+=2;}
			else if(FastwayAction[i+1].action==6 && getsum(i)%2==1){actionsequence[j++]=LV90M;current+=2;}
			else if(FastwayAction[i+1].action==1 && getsum(i)%2==0){actionsequence[j++]=RT45M;current+=2;}
			else if(FastwayAction[i+1].action==7 && getsum(i)%2==0){actionsequence[j++]=LT45M;current+=2;}
			else if(FastwayAction[i+1].action==1 && getsum(i)%2==1){actionsequence[j++]=RV45M;current+=1;}
			else if(FastwayAction[i+1].action==7 && getsum(i)%2==1){actionsequence[j++]=LV45M;current+=1;}
			else if(FastwayAction[i+1].action==0 && getsum(i)%2==0){actionsequence[j++]=STRAIGHT90M;current+=1;}
			else if(FastwayAction[i+1].action==0 && getsum(i)%2==1){actionsequence[j++]=STRAIGHT45M;current+=1;}
		}
	}
	else if(mode==HIGH)
	{
		for(i=0;i<FastwayCount-1;i++)
		{	
			if(i<current)continue;
	
			if(FastwayAction[i+1].action==1  && FastwayAction[i+2].action==2 && FastwayAction[i+3].action==1 && getsum(i)%2==0){actionsequence[j++]=R180H;current+=3;}
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==6 && FastwayAction[i+3].action==7 && getsum(i)%2==0){actionsequence[j++]=L180H;current+=3;}
			else if(FastwayAction[i+1].action==2 && FastwayAction[i+2].action==1 && getsum(i)%2==1){actionsequence[j++]=RV135H;current+=2;} 
			else if(FastwayAction[i+1].action==6 && FastwayAction[i+2].action==7 && getsum(i)%2==1){actionsequence[j++]=LV135H;current+=2;}
			else if(FastwayAction[i+1].action==1 && FastwayAction[i+2].action==2 && getsum(i)%2==0){actionsequence[j++]=RT135H;current+=3;} 
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==6 && getsum(i)%2==0){actionsequence[j++]=LT135H;current+=3;}
			else if(FastwayAction[i+1].action==7 && FastwayAction[i+2].action==7 && getsum(i)%2==0){actionsequence[j++]=LT90H;current+=2;}
			else if(FastwayAction[i+1].action==1 && FastwayAction[i+2].action==1 && getsum(i)%2==0){actionsequence[j++]=RT90H;current+=2;}
			else if(FastwayAction[i+1].action==2 && getsum(i)%2==1){actionsequence[j++]=RV90H;current+=2;}
			else if(FastwayAction[i+1].action==6 && getsum(i)%2==1){actionsequence[j++]=LV90H;current+=2;}
			else if(FastwayAction[i+1].action==1 && getsum(i)%2==0){actionsequence[j++]=RT45H;current+=2;}
			else if(FastwayAction[i+1].action==7 && getsum(i)%2==0){actionsequence[j++]=LT45H;current+=2;}
			else if(FastwayAction[i+1].action==1 && getsum(i)%2==1){actionsequence[j++]=RV45H;current+=1;}
			else if(FastwayAction[i+1].action==7 && getsum(i)%2==1){actionsequence[j++]=LV45H;current+=1;}
			else if(FastwayAction[i+1].action==0 && getsum(i)%2==0){actionsequence[j++]=STRAIGHT90H;current+=1;}
			else if(FastwayAction[i+1].action==0 && getsum(i)%2==1){actionsequence[j++]=STRAIGHT45H;current+=1;}
		}
	}
}

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

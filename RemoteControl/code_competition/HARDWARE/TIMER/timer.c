#include "timer.h"
#include "sys.h"
#include "stmflash.h"
#include "usart.h"		
#include "delay.h"	
#include "adc.h"
#include "utility.h"
#include "stratagy.h"
#include "led.h"
#include "stm32f10x_gpio.h"
#include "stm32f10x_rcc.h"
#include <math.h>
#include "user.h"

u16 led_test=0; 
int 	TIM2CNT=0; 
 
extern  uchar GucMouseTask;
u8 save_data_temp=0;
u16 save_data_num=0;
u8 save_data_time=1;
extern const u16 save_num;
extern const u8 per_timer;

 
#define COORADJUST				 
u8 EMERGENCYON=1;				 
#define SHOWCPU				 
 
#define FLASHSAVE				 

 
u8 gangleSee = 0;
u8 isSpeedTest = 0;
int speedListL[1] = {0}; 
int speedListR[1] = {0}; 
int speedListi = 0;
 int stable_count = 0;
int adjust = 0;
u8 keep_dis = 14;
 
 
 
 
u8 sidewall_threshold=130;		 

const u8 trackcoor=127;		 

u16 speed_ss=40;				 
u16 speed_turn=30;				 
u16 speed_fastss=30;			 
double GOSTRAIGHT_kp=0.4;
double SMARTSTRAIGHTFAST_bigerror_kp=0.5;		 
double SMARTSTRAIGHTFAST_smallerror_kp=0.35;	 
double STRAIGHT45_kp=0.12,STRAIGHT45_kd=0.1;
double turn_kp=4,turn_kd=4; 
float SPURT90IR_kp1,SPURT90IR_kp2,SPURT90IR_kd,SPURT90GYRO_kp,SPURT90GYRO_kd;
double kiturn;

 
 
int lw,rw;						 
int lwh=0,rwh=0;				 
int gdspeedl,gdspeedr;			 
int speedl,speedr,speed; 		 
long setvl=0,setvr=0;			 
double gdvl=0,gdvr=0;
int errl=0,errlh=0,errr=0,errrh=0,errls=0,errrs=0;
double pwml,pwmr;
double kpl=3,kil=2,kdl=0.1,kpr=3,kir=2,kdr=0;
 
long leftb,rightb,leftbh,rightbh,loopahead=0,absoluteangle=0,baseangle=0,angle,angleskip=0,anglehis[50]={0},newindex=0,anglehismax=-100,anglehismin=100;
long gangleh,ganglehh,peakgangle,peakgangleh;
 
int gyroinit=0,gyroswitch=0;
int gyro=0,gyro0;
int gdiff=0,gdiffh=0,gangle=0,ganglet=0,ggangleh=0,setgangle=0;
u16 angle_counter=0;
u8 angleterm=0,gyro_adj=0;						    
double g=1888.0,covg=1.0,gtmp,covgtmp,kg,Q=0.000912,P=0.368; 
 
long leftd,rightd,leftdh,rightdh,leftcycle=0,rightcycle=0,absolutetrack,absolutetrackl,absolutetrackr,basetrack=0,basetrackl=0,basetrackr=0,trackl,trackr,track;
 
long disf1,disf2,disf,disl,disr,disl45,disr45;
long dislh,disrh,dislhh,disrhh,disl45h,disr45h,disl45hh,disr45hh;
u8 lefthaswall=1,righthaswall=1,forwardhaswall,lefthaswallh=1,righthaswallh=1;
u8 lefthaswall45=0,righthaswall45=0,lefthaswallh45=0,righthaswallh45=0;
u8 leftwallcritical=0,rightwallcritical=0,leftwallcriticalh=0,rightwallcriticalh=0,leftwallcriticalhh=0,rightwallcriticalhh=0,leftwallcriticalhhh=0,rightwallcriticalhhh=0;;
u16 dislpeak=100,disrpeak=100,leftpeaktrack=0,rightpeaktrack=0;
u8 lastside=LEFT;
u8 irswitch=1;
u8 infrskipround=0;
 
long e,eh=0,esum=0;
 
u16 tick=0;
u8 step;
 
u8 task=0;
u16 turnaround_tick=0;
u8 blocknum=1,blocknum45=0;	 
double battary,battaryh=0;
u8 battarypercent=0;
u16 isemergency=0,emergency_count=0,emergency_delay=0;
int cpu0,cpu;
u8 freemode=0;
u8 sleepmode=0;
extern u8 lpeakfindenable,rpeakfindenable;
extern u16 flashaddr,flashaddrmax;
extern u8 flashwrite,flashenable;
const short *turn_gyro,*turn_vl,*turn_vr;
double decratio,accratio;
long queue[50]={0};
u8 queuenum=0,queuei,anglei;
int wtf;
int forwardirdis;
int stoptick;
extern int showbattery;

 
 
void Timer2_Init()			 
{
	RCC->APB2ENR|=3<<2;		 
	GPIOA->CRH&=0x0FFFFFFF;
	GPIOA->CRH|=0x80000000;
	GPIOB->CRL&=0xFFFF0FFF;
	GPIOB->CRL|=0x00008000;	 
	
	RCC->APB2ENR|=1<<0;		 
	RCC->APB1ENR|=1<<0;		 
	AFIO->MAPR&=0xF8FFFCFF;
	AFIO->MAPR|=0x04000000;	 
	AFIO->MAPR&=0xFFFFFCFF;
	AFIO->MAPR|=1<<8;		 
		
	TIM2->ARR=10000-1;		 
	TIM2->PSC=0;			 
	TIM2->CCER|=0<<1;		 
	TIM2->CCMR1|=1<<9;		 
	TIM2->CCMR1|=1<<1;		 
	TIM2->SMCR|=0x0003;		 
	TIM2->SR=0x0000;		 
	TIM2->CNT=0x0000;
	TIM2->CR1|=1<<0;		 
	
  
}

void Timer3_Init()			 
{
	RCC->APB2ENR|=1<<3;		 
	GPIOB->CRL&=0xFF00FFFF;
	GPIOB->CRL|=0x00880000;	 
	
	RCC->APB1ENR|=1<<1;		 
	AFIO->MAPR&=0XFFFFF3FF;
	AFIO->MAPR|=1<<11;		 
	
	TIM3->ARR=10000-1;		 
	TIM3->PSC=0;			 
	TIM3->CCER|=1<<1;		 
	TIM3->CCMR1|=1<<8;
	TIM3->CCMR1|=1<<0;
	TIM3->SMCR|=0x0003;		 
	TIM3->SR=0x0000;		 
	TIM3->CNT=0x0000;
	TIM3->CR1|=1<<0;		 
}

void Timer1_Init()			 
{
	RCC->APB2ENR|=1<<11;	 
 	TIM1->ARR=10000;		 
	TIM1->PSC=71;			 
	TIM1->CR1|=1<<0;		 
}

void delay_us(u16 us) 
{
	u16 start;
	u8 cross=0; 

	start=TIM1->CNT;
	while(1)
	{
		if(cross==0)
		{	
			if(TIM1->CNT < start)cross=1;
			if(TIM1->CNT-start >= us)break;
		}
		else if(cross==1)
		{
		 	if(TIM1->CNT+(10000-start) >= us)break;
		}
	}
}

void delay_ms(u16 ms)
{
	u16 start,t=0;
	u8 cross=0; 

	while(t<ms)
	{
		t++;
		cross=0;
		start=TIM1->CNT;
		while(1)
		{
			if(cross==0)
			{
				if(TIM1->CNT < start)cross=1;
				if(TIM1->CNT-start >= 1000)break;
			}
			else if(cross==1)
			{
			 	if(TIM1->CNT+(10000-start) >= 1000)break;
			}
		}
	}		
}


void PWM_motor_Init()		 
{		 					 
	 
	RCC->APB1ENR|=1<<2;		 
	RCC->APB2ENR|=1<<3;		 
	  	
	GPIOB->CRL&=0X00FFFFFF;
	GPIOB->CRL|=0XBB000000;	 
	GPIOB->CRH&=0XFFFFFF00;
	GPIOB->CRH|=0X000000BB;	 
	
	TIM4->ARR=MAXPWM;		 
	TIM4->PSC=1;			 
	
	TIM4->CCMR1&=0x0C0C;	 
	TIM4->CCMR1|=6<<4;		 
	TIM4->CCMR1|=6<<12;		 
	
	TIM4->CCMR2&=0x0C0C;	 
	TIM4->CCMR2|=6<<4;		 
	TIM4->CCMR2|=6<<12;		 
	
	TIM4->CCMR1|=1<<3;		 
	TIM4->CCMR1|=1<<11;		 
	TIM4->CCMR2|=1<<3;		 
	TIM4->CCMR2|=1<<11;		 

	TIM4->CCER|=1<<0;		 
	TIM4->CCER|=1<<4;		 
	TIM4->CCER|=1<<8;		 
	TIM4->CCER|=1<<12;		 

	TIM4->CR1|=1<<7;		 
	TIM4->CR1|=1<<0;		 
}


int alimit(int e)  
{
 	int maxa=10000;
	if(e>maxa)return maxa;
	else if(e<-maxa)return -maxa;
	else return e;
}


int elimit(int e)  
{
 	u8 maxe=130;
	if(e>maxe)return maxe;
	else if(e<-maxe)return -maxe;
	else return e;
}

int abs(int t)
{
	if(t>=0)return t;
	else return -1*t;
}

void send(long s)
{
	queue[queuenum]=s;
	queuenum++;
}

//限制函数
int limit(int value, int min_val, int max_val) {
    if(value < min_val) return min_val;
    if(value > max_val) return max_val;
    return value;
}

void SysTick_Handler()			 
{
	cpu0=TIM1->CNT;
	if(led_test==1000) {led_test=0;}
	led_test++;
	
	if(sleepmode==1)			 
	{
		LPWMH=0; LPWML=0;
		RPWMH=0; RPWML=0;
	}
	else
	{
		cpu0=TIM1->CNT;
 
 
 
 
 
 
 
 
		gyro=Get_Adc(ADC_GYRO);
		 
 
 
 
 
 
		g=gyro;
	
		gdiffh=gdiff;
		gdiff=(int)(g-gyro0)/2;
		if(gdiff<0) gdiff--;
		ganglet+=(gdiff+gdiffh)/2; 
		ggangleh=gangle;
		gangle=ganglet/gangleratio*(-1);
	
		if(gangle>ggangleh) angleterm=1;
		else if(gangle<ggangleh) angleterm=2;

		 
		if(gyro_adj==1)
		{
			angle_counter++;
			if(gangle>ggangleh)
			{
				gyro0--;
				angle_counter=0;
			}
			else if(gangle<ggangleh)
				{
					gyro0++;
					angle_counter=0;
				}
		}
 
 
 
 
 
		leftbh=leftb;
		rightbh=rightb;
		leftb= TIM2->CNT;
		rightb=TIM3->CNT;
		if((leftb-leftbh)<-9000)loopahead++;		 
		if((rightb-rightbh)<-9000)loopahead--;		 
		if((leftb-leftbh)>9000)loopahead--;			 
		if((rightb-rightbh)>9000)loopahead++;		 
		absoluteangle=(leftb-rightb+loopahead*10000)/angleratio;	 
		angle=absoluteangle-baseangle;				 
	
		 
		
		if(gangleSee == 0)
		{
			
					angleskip++;
		if(angleskip==5)
		{
			angleskip=0;
			anglehis[newindex]=angle;				 
			newindex++;
			if(newindex==40) newindex=0;
			
			anglehismin=100;anglehismax=-100;
			for(anglei=0;anglei<40;anglei++)
			{
				if(anglehis[anglei]<anglehismin) anglehismin=anglehis[anglei];
				if(anglehis[anglei]>anglehismax) anglehismax=anglehis[anglei];
			}
			if(gyro_adj!=1)							 
			{										 
				if(abs(anglehismax-anglehismin)<2 && abs(angle)<20) ganglet=0;	 
			}
		}
		
		}
		

 
 
 
 
 
 
 
		if((ganglehh-gangleh)>0 && (gangleh-gangle)<0) {peakgangleh=peakgangle; peakgangle=gangle;}
		if((ganglehh-gangleh)<0 && (gangleh-gangle)>0) {peakgangleh=peakgangle; peakgangle=gangle;}
		ganglehh=gangleh;
		gangleh=gangle;
 
 
 
 
 
 
		leftdh=leftd;
		rightdh=rightd;
		leftd=TIM2->CNT;
		rightd=TIM3->CNT;
		if((leftd-leftdh)<-9000) leftcycle++;		 
		if((rightd-rightdh)<-9000) rightcycle++;	 
		if((leftd-leftdh)>9000) leftcycle--;		 
		if((rightd-rightdh)>9000) rightcycle--;		 
		absolutetrackl=(float)(leftd+leftcycle*10000)/TrackRatioL;
		absolutetrackr=(float)(rightd+rightcycle*10000)/TrackRatioR;
		absolutetrack=(absolutetrackl+absolutetrackr)/2;
		trackl=absolutetrackl-basetrackl;
		trackr=absolutetrackr-basetrackr;				   
		track=absolutetrack-basetrack;
 
 
 
 
 
		switch(irswitch) 
		{
			case 1:
				dislhh=dislh;
				disrhh=disrh;
				dislh=disl;
				disrh=disr;
				disl45hh=disl45h;
				disr45hh=disr45h;			
				disl45h=disl45;
				disr45h=disr45;			
				getdistance();
				disf=(disf1+disf2)/2;
				break;
			case 0:
				infrskipround++;
				break;
		}
		 
		lefthaswallh=lefthaswall;
		righthaswallh=righthaswall;
		if(disl<sidewall_threshold) lefthaswall=1;
		else lefthaswall=0;
		if(disr<sidewall_threshold) righthaswall=1;
		else righthaswall=0;
		if((disf1<230&&disf2<245)||(disf2<240&&disf1<245)||(disf1<65)||(disf2<65)) forwardhaswall=1;  //(disf1<45)||(disf2<45)
		else forwardhaswall=0;

		 
		lefthaswallh45=lefthaswall45;
		righthaswallh45=righthaswall45;
		if(disl<150) lefthaswall45=1;
		else lefthaswall45=0;
		if(disr<150) righthaswall45=1;
		else righthaswall45=0;

		 
		leftwallcriticalhhh=leftwallcriticalhh;
		leftwallcriticalhh=leftwallcriticalh;
		leftwallcriticalh=leftwallcritical;
		rightwallcriticalhhh=rightwallcriticalhh;
		rightwallcriticalhh=rightwallcriticalh;
		rightwallcriticalh=rightwallcritical;
		if(disl<60) leftwallcritical=1; 
		else leftwallcritical=0;
		if(disr<60) rightwallcritical=1;
		else rightwallcritical=0;
 
 
 
 
 
		
 
 
 
	switch(task)
	{
 
	case NULL:
 
		break;
	
 
	case ACC:				 
 
		eh=e;
		e=gangle;
		 
		setvl=accratio*track+ACCV0;
		setvr=setvl;
		 
		setvl-=e*2.5+(e-eh)*1.6;
		setvr+=e*2.5+(e-eh)*1.6;
		break;
	
 
	case STOP:				 
 
		 
		eh=e;
		e=gangle;	   
		 
		setvl=-decratio*track;
		setvr=setvl;
		 
		setvl-=e*2.5+(e-eh)*1.6;
		setvr+=e*2.5+(e-eh)*1.6;
		if(track<2&&track>-2&&speedl<2&&speedr<2&&speedl>-2&&speedr>-2) tick++;
		else tick=0;
		stoptick++;
		break;
	
 
	case SLOWDOWN:			 
 
		 
		eh=e;
		e=gangle;
		tick++;
		 
		{
			setvl-=4; 
			setvr=setvl;
			 
			 
			 
		}
		break;
		
 
	case SPEEDUP:			 
 
		 
		eh=e;
		e=gangle;
		tick++;
		 
		{
			setvl+=1;
			setvr=setvl;
			 
			 
			 
		}
		break;
		
 
	case TURN:
 
		setgangle=*(turn_gyro+tick);	 	 
		eh=e;
		e=gangle-setgangle;
		tick++;
		setvl=*(turn_vl+tick)-e*turn_kp-(e-eh)*turn_kd; 
		setvr=*(turn_vr+tick)+e*turn_kp+(e-eh)*turn_kd;             
		break;
	
 
	case TURNAROUND:		 
 
		if(step==0)
		{
			tick++;	 		
			setgangle=*(turn_gyro+tick);	 	 
			eh=e;
			e=gangle-setgangle;
 
			esum+=e;		
			setvl=*(turn_vl+tick)-e*2.5-esum*0.01-(e-eh)*0.1;
			setvr=*(turn_vr+tick)+e*2.5+esum*0.01+(e-eh)*0.1;
			if(tick>200){step=1;tick=0;}                
			turnaround_tick = 0;
		}
		else if(step==1)
		{
			eh=e;
			e=gangle;
			setvl=-e*2.5-(e-eh)*0.1;	 
			setvr=e*2.5+(e-eh)*0.1;
			if(speedl<2&&speedr<2&&speedl>-2&&speedr>-2)tick++; 
			else tick=0;
 
 
		}
		break;
		
 
	case POINTTURN90:		 
 
		if(step==0)
		{
			tick++;	 		
			setgangle=*(turn_gyro+tick);	 	 
			eh=e;
			e=gangle-setgangle;	   
			esum+=e;		
			setvl=*(turn_vl+tick)-e*2.5-esum*0.01-(e-eh)*0.1; 
			setvr=*(turn_vr+tick)+e*2.5+esum*0.01+(e-eh)*0.1;
			if(tick>30){step=1;tick=0;}             //230
			turnaround_tick = 0;
		}
		else if(step==1)
		{	 	 
			e=gangle;
			setvl=e*(-1);	 
			setvr=e*1;
			if(speedl<10&&speedr<10&&speedl>-10&&speedr>-10)tick++;    //15     
			else tick=0;
		}
		break;
		
 
	case BEFORE90TURN:
 
		if(forwardhaswall==1) 
		{			 
			e=disf2-disf1;
			setvl=speed_turn-e*0.1;
			setvr=speed_turn+e*0.1;
		}
		else 
		{
			e=gangle;
			setvl=speed_turn-e*0.6; 
			setvr=speed_turn+e*0.6;
		}
		 
		{
 
 
		}
		break;
		
 
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
		break;
		
 
	case FORWARDIRTEST:		 
 
		setvl=(disf-forwardirdis)*0.5+(disf1-forwardirdis)*1;	 
		setvr=(disf-forwardirdis)*0.5+(disf2-forwardirdis)*1;
		if(setvl>50) setvl=50;
		if(setvl<-50) setvl=-50;
		if(setvr>50) setvr=50;
		if(setvr<-50) setvr=-50;
		if(setvl<2 && setvl>-2) setvl=0;
		if(setvr<2 && setvr>-2) setvr=0;
		if( (disf-forwardirdis)<-20 && abs(disf1-disf2)<100 )	 
		{
			setvl=-50;
			setvr=-50;
		}
		if( (disf-forwardirdis)>20 && abs(disf1-disf2)<100 )
		{
			setvl=50;
			setvr=50;
		}
		break;
		
 
	case KEEPWALLDISTANCE:


		setvl=(disf-keep_dis)*0.5+(disf1-keep_dis)*1;				 
		setvr=(disf-keep_dis)*0.5+(disf2-keep_dis)*1;
		if(setvl>50) setvl=50;
		if(setvl<-50) setvl=-50;
		if(setvr>50) setvr=50;
		if(setvr<-50) setvr=-50;
		if( (disf-keep_dis)<-20 && abs(disf1-disf2)<100 )
		{
			setvl=-50;
			setvr=-50;
		}
		if( (disf-keep_dis)>20 && abs(disf1-disf2)<100 )
		{
			setvl=50;
			setvr=50;
		}
		if(speedl<2&&speedr<2&&speedl>-2&&speedr>-2)tick++;
		else tick=0;
		turnaround_tick++;
		if(turnaround_tick>500)tick=100;		
		break;
		
 
 
 
 
 
		
		
 
	case GOSTRAIGHT:			 
 
		eh=e;
		e=gangle;
		setvl=speed_ss-e*GOSTRAIGHT_kp; 
		setvr=speed_ss+e*GOSTRAIGHT_kp; 
		break;
	
 
	case SMARTSTRAIGHT:			 
 
		if(lefthaswall==0 && righthaswall==0) 
		{
			eh=e;
			e=gangle;
			setvl=speed_turn-e*1;		 
			setvr=speed_turn+e*1;
		}
		else if(lefthaswall==1)
		{
			eh=e;
			e=disl-dis2wall;
			setvl=speed_turn-e*0.1;		
			setvr=speed_turn+e*0.1;
		}
		else if(righthaswall==1)
		{
			eh=e;
			e=dis2wall-disr;
			setvl=speed_turn-e*0.1;		
			setvr=speed_turn+e*0.1;
		}
		break;
		
 
	case SMARTSTRAIGHTFAST:		 
 
		if(lefthaswall==0 && righthaswall==0) 
		{
			eh=e;
			e=gangle;
			setvl=speed_ss-e*1;		
			setvr=speed_ss+e*1;
		}
		else
		{
			if((lastside==LEFT && lefthaswall==1) || (lastside==RIGHT && righthaswall==0)) 
			{
				lastside=LEFT;
				eh=e;				  
				e=disl-dis2wall;
				if(abs(e)>10)
				{
					setvl=speed_ss-e*SMARTSTRAIGHTFAST_bigerror_kp-(e-eh)*0.16; 
					setvr=speed_ss+e*SMARTSTRAIGHTFAST_bigerror_kp+(e-eh)*0.16; 
				}
				else
				{
					setvl=speed_ss-e*SMARTSTRAIGHTFAST_smallerror_kp-(e-eh)*0.1; 
					setvr=speed_ss+e*SMARTSTRAIGHTFAST_smallerror_kp+(e-eh)*0.1; 
				}
			}
			else
			{
				lastside=RIGHT;			  
				eh=e;
				e=dis2wall-disr;
				if(abs(e)>10)
				{
					setvl=speed_ss-e*SMARTSTRAIGHTFAST_bigerror_kp-(e-eh)*0.16; 
					setvr=speed_ss+e*SMARTSTRAIGHTFAST_bigerror_kp+(e-eh)*0.16; 
				}
				else
				{
					setvl=speed_ss-e*SMARTSTRAIGHTFAST_smallerror_kp-(e-eh)*0.1; 
					setvr=speed_ss+e*SMARTSTRAIGHTFAST_smallerror_kp+(e-eh)*0.1; 
				}
			}		
		}
		 
		break;
		
 
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

 
 
 
 
 
 
 
	case SMARTSTRAIGHTSPURT: 
 
		if(lefthaswall==0 && righthaswall==0) 
		{
			eh=e;
			e=gangle;
			setvl=speed_ss-e*1;		
			setvr=speed_ss+e*1;
		}
		else if(lefthaswall==1)
		{
			eh=e;
			e=disl-dis2wall;
			setvl=speed_ss-e*0.03;		
			setvr=speed_ss+e*0.03;
		}
		else if(righthaswall==1)
		{
			eh=e;
			e=dis2wall-disr;
			setvl=speed_ss-e*0.03;		
			setvr=speed_ss+e*0.03;
		}
		break;
 
	case SMARTSTRAIGHTSPURTL: 
 
		if(lefthaswall==0 && righthaswall==0) 
		{
			eh=e;
			e=gangle;
			setvl=speed_ss-e*1;		
			setvr=speed_ss+e*1;
		}
		else if(lefthaswall==1)
		{
			eh=e;
			e=disl-dis2wall;
			setvl=speed_ss-e*0.01;		
			setvr=speed_ss+e*0.01;
		}
		else if(righthaswall==1)
		{
			eh=e;
			e=dis2wall-disr;
			setvl=speed_ss-e*0.01;		
			setvr=speed_ss+e*0.01;
		}
		break;
		
 
	case SPURT90IR:	 

    
 /*   
   if(lefthaswallh==1 && righthaswall==1) {
        stable_count++;
        if(stable_count > 4) {  // ??3???????????
            eh = e;
            e = disl - disr;
            
            if(abs(e) < 25) e = 0;  // ????  //25
            
            // ???????
            
            if(abs(e) > 30) {
                adjust = e * SPURT90IR_kp1;
            } else {
                adjust = e * SPURT90IR_kp2 + (e-eh)*SPURT90IR_kd;
            }
            
            adjust = limit(adjust, -15, 15);  // ??????
            setvl = speed_ss - adjust;
            setvr = speed_ss + adjust;
        }
    } else {
        stable_count = 0;
       
        setvl = speed_ss;
        setvr = speed_ss;
    }
   */
				//左右都有墙		

 /*   if( lefthaswallh==1&&righthaswall==1&&lefthaswall==1 && righthaswall==1)
{
    // ????
   // ??:????
    
    eh = e;
    e = disl - disr;     // ????

    if(abs(e) < 30 ) e = 0;

    if(abs(e) > 30 )
    {
        setvl = speed_ss - e * SPURT90IR_kp1;
        setvr = speed_ss + e * SPURT90IR_kp1;
    }
    else
    {
        setvl = speed_ss - e * SPURT90IR_kp2 - (e-eh)*SPURT90IR_kd;
        setvr = speed_ss + e * SPURT90IR_kp2 + (e-eh)*SPURT90IR_kd;
    }
}
*/

	if(lefthaswall==0 && righthaswall==0) 
		{
			eh=e;
			e=gangle;
			setvl=speed_ss-e*1;		
			setvr=speed_ss+e*1;
		}


		else
		{

			if((lastside==LEFT && lefthaswall==1) || (lastside==RIGHT && righthaswall==0)) 
			{
				lastside=LEFT;
				eh=e;				  
				e=disl-dis2wall;
				if(abs(e)>65)
				{
					e=0;
				}
				if(abs(e)>35)
				{
					setvl=speed_ss-e*SPURT90IR_kp1; 
					setvr=speed_ss+e*SPURT90IR_kp1;
				}
				else
				{
					setvl=speed_ss-e*SPURT90IR_kp2-(e-eh)*SPURT90IR_kd;
					setvr=speed_ss+e*SPURT90IR_kp2+(e-eh)*SPURT90IR_kd;
				}
			}
			else
			{			lastside=RIGHT;			  
				eh=e;
				e=dis2wall-disr;
					if(abs(e)>65)
				{
					e=0;
				}
				if(abs(e)>35)
				{
					setvl=speed_ss-e*SPURT90IR_kp1;
					setvr=speed_ss+e*SPURT90IR_kp1;
				}
				else
				{
					setvl=speed_ss-e*SPURT90IR_kp2-(e-eh)*SPURT90IR_kd;
					setvr=speed_ss+e*SPURT90IR_kp2+(e-eh)*SPURT90IR_kd;
				}

			}		
		}
		
		break;
		
 
	case SPURT90BLIND: 
 
		eh=e;
		e=gangle;
		setvl=speed_ss-e*SPURT90GYRO_kp-(e-eh)*SPURT90GYRO_kd;
		setvr=speed_ss+e*SPURT90GYRO_kp+(e-eh)*SPURT90GYRO_kd;
		
	 
	 
	 
	 
		break;
	
 
	case STRAIGHT45: 
 
 
 
		if(disl<150) //150
		{
			e=abovezero(disl+50-disf1)+abovezero(180-disf1); 
			setvl=speed_fastss+e*STRAIGHT45_kp+(e-eh)*STRAIGHT45_kd;
			setvr=speed_fastss-e*STRAIGHT45_kp-(e-eh)*STRAIGHT45_kd;
		}
		else if(disr<150) //150
		{
			e=abovezero(disr+50-disf2)+abovezero(180-disf2); 
			setvl=speed_fastss-e*STRAIGHT45_kp-(e-eh)*STRAIGHT45_kd;
			setvr=speed_fastss+e*STRAIGHT45_kp+(e-eh)*STRAIGHT45_kd;
		}
	
		if(disr>150 && disl>150) 
		{
			e= abovezero(220-disf2)-abovezero(220-disf1);
			setvl=speed_fastss-e*STRAIGHT45_kp;
			setvr=speed_fastss+e*STRAIGHT45_kp;
		}
		break;

	default:
		ledon(0);
	break;
	} 
 
 
 
 
 
 

	if(isemergency==1)
	{
		freemode=1;
	 
		ledon(0);
		task=NULL;
		setvl=0;
		setvr=0;
		emergency_delay++;
		if(emergency_delay==200) sleepmode=1;
	 
	}
 
 
	 
	gdvl=setvl*SpeedRatioL;						 
	lw=TIM2->CNT;		  
	gdspeedl=lw-lwh;									  
	if(gdspeedl<-5000) gdspeedl+=10000;			 
	if(gdspeedl>5000) gdspeedl-=10000;			 
	lwh=lw;
	errlh=errl;
	errl=(int)gdvl-gdspeedl;
	errls+=errl;
	if(errls>1200) errls=1200;
	if(errls<-1200) errls=-1200;				 
	
	 
	gdvr=setvr*SpeedRatioR;						 
	rw=TIM3->CNT;		  
	gdspeedr=rw-rwh;
	if(gdspeedr<-5000) gdspeedr+=10000;
	if(gdspeedr>5000) gdspeedr-=10000;
	rwh=rw;
	errrh=errr;
	errr=(int)gdvr-gdspeedr;
	errrs+=errr;
	if(errrs>1200) errrs=1200;
	if(errrs<-1200) errrs=-1200;				 

	speedl=gdspeedl/SpeedRatioL;
	speedr=gdspeedr/SpeedRatioR;
	speed=(speedl+speedr)/2;
	
 
 

	
	if(isSpeedTest == 1)
	{
		if(speedListi > 5000)
		{
			speedListi = 5000;
		}
		
		if(speedListi % 100 == 0)
		{
			speedListL[speedListi/100] = gdspeedl;
			speedListR[speedListi/100] = gdspeedr;
		}	
		speedListi++;
	}

	

	if(freemode==0)
	{ 
		
		motor_PID_init();
		
 
		pwml=errl*kpl+errls*kil+(errl-errlh)*kdl;
 
		pwmr=errr*kpr+errrs*kir+(errr-errrh)*kdr;
	}
	else
	{
		pwml=0;pwmr=0;
	}									   

	if(pwml>SPEED_HAHA) pwml=SPEED_HAHA;
	if(pwml<-SPEED_HAHA) pwml=-SPEED_HAHA;
	if(pwml>=1) 
	{
		LPWML=0;LPWMH=pwml;
	}
	else if(pwml<=-1) 
	{
		LPWML=-pwml;LPWMH=0;
	}
	else
	{
		LPWML = 0;
		LPWMH = 0;
	}
	
	
	if(pwmr>SPEED_HAHA) pwmr=SPEED_HAHA;
	if(pwmr<-SPEED_HAHA) pwmr=-SPEED_HAHA;
	if(pwmr>=35)
	{
		RPWML=0;RPWMH=pwmr;
	}
	else if(pwmr<=-35)
	{
		RPWML=-pwmr;RPWMH=0;
	}
	else 
	{
		RPWML = 0;
		RPWMH = 0;
	}
 
 
 
 
	if(EMERGENCYON==1)
	{
		 
		if(pwml>SPEED_HAHA || pwmr>SPEED_HAHA || pwml<-SPEED_HAHA || pwmr<-SPEED_HAHA) emergency_count++;
		else emergency_count=0;
		if(emergency_count>200 || setvl>2000 || setvr>2000 || setvl<-2000 || setvr<-2000) isemergency=1; 
	}
 
 
 
	if(showbattery==1)
	{
		battaryh=battary;
		battary=Get_Adc(ADC_BATTARY)*3.3*3/4096*100; 
		battary=(battary*1+battaryh*9)/10;
		battarypercent=(battary-700)/12;
		if(battarypercent==0) battarypercent=1;
		if(battarypercent>8) battarypercent=8;
		#ifdef SHOWVOLTAGE
		ledoff(0);
		ledon(battarypercent);
		#endif
	}
 
 
 
 
	#ifdef FLASHSAVE
 
 
 
 
 
 
 
 
 
 
	if(flashenable==1)						
	{
		if(flashwrite==1) 
		{
			save_data_temp++;
			if(save_data_temp>(per_timer-1))
			{write_data(save_data_num);save_data_num++;save_data_temp=0;
			if(save_data_num>=save_num-1) {save_data_num=0;save_data_time++;}
			}
		}
	}	
	#endif
 
		 
 
	#ifdef SHOWCPU
	cpu=TIM1->CNT-cpu0;
	if(cpu<0) cpu=cpu+10000;
	cpu=cpu/10;			 
	if(flashenable==1)						
	{
		if(cpu>95)		 
		{
			ledon(1);ledon(7);
		}
		else ledoff(0);
	}
	#endif
 
	if(isemergency==1) ledon(0);
}
}



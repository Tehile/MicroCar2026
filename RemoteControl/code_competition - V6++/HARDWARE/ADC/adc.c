#include <stm32f10x.h>
#include "sys.h"
#include "usart.h"		
#include "delay.h"	
#include "timer.h"
#include "adc.h"
#if !defined(MOUSE_REMOTE) || defined(REMOTE_FULL)
#include "search_guard_v5.h"
#endif
#include "led.h"
#include <math.h>

 
 
 
 
 
	   
 
 
 
 
 
void  Adc_Init(void)
{    
	 
	RCC->APB2ENR|=1<<2;    	 
	GPIOA->CRL&=0XFFFFFF00;	 
	
 	RCC->APB2ENR|=1<<4;    	 
	GPIOC->CRL&=0XF0FF0000;	 
	GPIOC->CRL|=0X03000000;	 
	
	RCC->APB2ENR|=1<<3;    	 
	GPIOB->CRH&=0X000FFFFF;  
	GPIOB->CRH|=0X33300000;	 

	 
	RCC->APB2ENR|=1<<9;    	 
	RCC->APB2RSTR|=1<<9;   	 
	RCC->APB2RSTR&=~(1<<9);	 
	RCC->CFGR&=~(3<<14);   	 
	 
	 
	RCC->CFGR|=2<<14;		 

	ADC1->CR1&=0X00F0FFFF;	 
	ADC1->CR1|=0<<16;		 
	ADC1->CR1&=~(1<<8);		 
	ADC1->CR2&=~(1<<1);		 
	ADC1->CR2|=7<<17;		 
	ADC1->CR2|=1<<20;		 
	ADC1->CR2&=~(1<<11);	 

	ADC1->SQR1&=~(0XF<<20);	 

	 
	ADC1->SMPR1&=0XFFFFF000; 
	ADC1->SMPR2&=0XFFFFFFC0;
	ADC1->SMPR1|=7<<9;       
	ADC1->SMPR1|=7<<6;       
	ADC1->SMPR1|=7<<3;       
	ADC1->SMPR1|=7<<0;       
	ADC1->SMPR2|=7<<3;		 
	ADC1->SMPR2|=7<<0;		 

	ADC1->CR2|=1<<0;	     
	ADC1->CR2|=1<<3;         
	while(ADC1->CR2&1<<3);   
   
	ADC1->CR2|=1<<2;         
	while(ADC1->CR2&1<<2);   
	 
}	
		  
 
 
 
 
 
u16 Get_Adc(u8 ch)   
{
	 
	ADC1->SQR3&=0XFFFFFFE0; 
	ADC1->SQR3|=ch;		  			    
	ADC1->CR2|=1<<22;        
	while(!(ADC1->SR&1<<1)); 
	return ADC1->DR;		 
}

u16 dataf1[250]={2990,2770,2576,2404,2250,2112,1987,1874,1772,1679,1593,1515,1443,1376,1315,1257,1204,1155,1109,1065,1025,987,952,918,886,857,828,802,776,752,730,708,688,668,649,631,614,598,583,568,553,540,527,514,502,490,479,468,458,448,438,429,420,411,403,395,387,380,372,365,358,352,345,339,333,327,321,316,310,305,300,295,290,285,281,276,272,268,264,260,256,252,248,245,241,238,234,231,228,225,222,219,216,213,210,207,205,202,199,197,195,192,190,187,185,183,181,179,177,174,172,170,169,167,165,163,161,159,158,156,154,153,151,149,148,146,145,143,142,140,139,138,136,135,134,132,131,130,129,127,126,125,124,123,122,120,119,118,117,116,115,114,113,112,111,110,109,108,108,107,106,105,104,103,102,101,101,100,99,98,97,97,96,95,94,94,93,92,92,91,90,90,89,88,88,87,86,86,85,84,84,83,83,82,81,81,80,80,79,79,78,78,77,77,76,76,75,75,74,74,73,73,72,72,71,71,70,70,69,69,69,68,68,67,67,66,66,66,65,65,64,64,64,63,63,63,62,62,62,61,61,60,60,60,59,59,59,58,58,0};
u16 dataf2[250]={4333,3982,3675,3404,3163,2949,2757,2585,2429,2288,2159,2042,1935,1836,1745,1662,1584,1512,1446,1384,1326,1271,1221,1173,1129,1087,1047,1010,975,942,910,880,852,825,800,775,752,730,709,689,670,652,634,618,602,586,571,557,544,530,518,506,494,483,472,461,451,442,432,423,414,406,398,390,382,374,367,360,353,347,340,334,328,322,317,311,306,300,295,290,286,281,276,272,267,263,259,255,251,247,244,240,236,233,230,226,223,220,217,214,211,208,205,202,200,197,194,192,189,187,184,182,180,178,175,173,171,169,167,165,163,161,159,157,156,154,152,150,149,147,145,144,142,141,139,138,136,135,133,132,131,129,128,127,125,124,123,122,120,119,118,117,116,115,114,113,111,110,109,108,107,106,105,104,104,103,102,101,100,99,98,97,96,96,95,94,93,92,92,91,90,89,89,88,87,86,86,85,84,84,83,82,82,81,81,80,79,79,78,77,77,76,76,75,75,74,74,73,72,72,71,71,70,70,69,69,68,68,67,67,67,66,66,65,65,64,64,63,63,63,62,62,61,61,61,60,60,59,59,59,58,58,57,57,57,56,56,56,55,0};
u16 datal[250]={2827,2635,2464,2313,2177,2054,1943,1843,1751,1667,1590,1518,1453,1392,1336,1283,1234,1188,1146,1105,1068,1032,999,967,937,909,882,856,832,809,787,766,746,727,709,692,675,659,644,629,615,602,589,576,564,552,541,530,520,510,500,490,481,473,464,456,448,440,432,425,418,411,404,398,392,385,379,374,368,362,357,352,347,342,337,332,328,323,319,315,310,306,302,298,295,291,287,284,280,277,273,270,267,264,261,258,255,252,249,246,244,241,238,236,233,231,228,226,224,221,219,217,215,212,210,208,206,204,202,200,198,197,195,193,191,189,188,186,184,183,181,180,178,176,175,173,172,171,169,168,166,165,164,162,161,160,158,157,156,155,153,152,151,150,149,148,147,146,144,143,142,141,140,139,138,137,136,135,134,133,133,132,131,130,129,128,127,126,125,125,124,123,122,121,121,120,119,118,118,117,116,115,115,114,113,113,112,111,110,110,109,109,108,107,107,106,105,105,104,104,103,102,102,101,101,100,100,99,98,98,97,97,96,96,95,95,94,94,93,93,92,92,91,91,90,90,89,89,88,88,88,87,87,86,86,85,85,85,84,0};
u16 datar[250]={2241,2106,1986,1877,1779,1690,1609,1535,1467,1404,1345,1291,1241,1195,1151,1111,1072,1037,1003,971,941,913,886,861,837,814,792,771,751,733,715,697,681,665,650,635,622,608,595,583,571,560,549,538,528,518,508,499,490,481,473,465,457,449,442,434,427,421,414,408,401,395,390,384,378,373,368,362,357,352,348,343,338,334,330,325,321,317,313,309,306,302,298,295,291,288,285,281,278,275,272,269,266,263,261,258,255,252,250,247,245,242,240,238,235,233,231,228,226,224,222,220,218,216,214,212,210,208,206,205,203,201,199,198,196,194,193,191,190,188,187,185,184,182,181,179,178,176,175,174,172,171,170,169,167,166,165,164,163,161,160,159,158,157,156,155,154,153,151,150,149,148,147,146,145,145,144,143,142,141,140,139,138,137,136,136,135,134,133,132,131,131,130,129,128,128,127,126,125,125,124,123,122,122,121,120,120,119,118,118,117,116,116,115,115,114,113,113,112,111,111,110,110,109,109,108,107,107,106,106,105,105,104,104,103,103,102,102,101,101,100,100,99,99,98,98,97,97,96,96,96,95,95,94,94,93,93,92,92,0};	 

 
 
 
 
 
 
u16 FindDis(u16 ADC,u16 *data,u16 Length)
{
 	int tmp=0;
	if(ADC>data[0]) return 0;
	if(ADC<data[Length-2]) return Length-1;
	tmp=(Length-1)/2;
	Length/=2;
	while(1)
	{
		if(data[tmp]>ADC)
		{
			tmp+=Length/2; 
			if(Length%2==1)Length+=1; 
			Length/=2;
		}
		else if(data[tmp]<ADC)
		{
			tmp-=Length/2; 
			if(Length%2==1)Length+=1; 
			Length/=2;
		}
		else break;
		
		if(Length==1)break;
	}
	return tmp;
}



static long abovezero(long t)
{
	if(t>=0)return t;
	else return 0;
}


 
 
 
 
 
 
 
 
double f1=2000.0,covf1=1.0,f1tmp,covf1tmp,kf1,Qf1=0.01,Pf1=0.2; 
double f2=2000.0,covf2=1.0,f2tmp,covf2tmp,kf2,Qf2=0.01,Pf2=0.2;
double l=2000.0,covl=1.0,ltmp,covltmp,kl,Ql=0.01,Pl=0.2;
double r=2000.0,covr=1.0,rtmp,covrtmp,kr,Qr=0.01,Pr=0.2;
u16 TempL,TempR;
extern long disf1,disf2,disf,disl,disr,disl45,disr45;
int rawf1,rawf2,rawl,rawr,rawf1h,rawf2h,rawlh,rawrh;
u8 kalmanswitch=1;
u8 biaodingmode=0;

void getdistance()
{
	delay_us(5);
	TempL=Get_Adc(ADC_LF);
	TempR=Get_Adc(ADC_RF);
	INFR_LF=1;INFR_RF=1;
	
	delay_us(60);
	TempL=abovezero(Get_Adc(ADC_LF)-TempL);
	TempR=abovezero(Get_Adc(ADC_RF)-TempR);
#if !defined(MOUSE_REMOTE) || defined(REMOTE_FULL)
    v5_diag.front_adc1=TempL;
    v5_diag.front_adc2=TempR;
#endif
	INFR_LF=0;INFR_RF=0;
	
	if(kalmanswitch==1)
	{
		f1tmp=f1;
		covf1tmp=covf1+Qf1;
		kf1=covf1tmp/(covf1tmp+Pf1);
		f1=f1tmp+kf1*((double)TempL-f1tmp);
		covf1=(1-kf1)*covf1tmp;
		
		f2tmp=f2;
		covf2tmp=covf2+Qf2;
		kf2=covf2tmp/(covf2tmp+Pf2);
		f2=f2tmp+kf2*((double)TempR-f2tmp);
		covf2=(1-kf2)*covf2tmp;
	}
	else
	{
		f1=TempL;
		f2=TempR;
	}
 
	TempL=Get_Adc(ADC_L);
	TempR=Get_Adc(ADC_R);
	INFR_L=1;INFR_R=1;
 
	if(biaodingmode==0)
	{
		rawf1=f1;
		disf1=FindDis(abovezero((u16)f1),dataf1,250);
		
		rawf2=f2;
		disf2=FindDis(abovezero((u16)f2),dataf2,250);
	}
	else 
	{
		rawf1h=rawf1;
		rawf1=f1;
		rawf1=(rawf1*3+rawf1h*7)/10;
		
		rawf2h=rawf2;
		rawf2=f2;
		rawf2=(rawf2*3+rawf2h*7)/10;
	}
 
	delay_us(55);
	TempL=abovezero(Get_Adc(ADC_L)-TempL);
	TempR=abovezero(Get_Adc(ADC_R)-TempR);
	INFR_L=0;INFR_R=0;

	if(kalmanswitch==1)
	{
		ltmp=l;
		covltmp=covl+Ql;
		kl=covltmp/(covltmp+Pl);
		l=ltmp+kl*((double)TempL-ltmp);
		covl=(1-kl)*covltmp;
		
		rtmp=r;
		covrtmp=covr+Qr;
		kr=covrtmp/(covrtmp+Pr);
		r=rtmp+kr*((double)TempR-rtmp);
		covr=(1-kr)*covrtmp;
	}
	else
	{
		l=TempL;
		r=TempR;
	}
	
	if(biaodingmode==0)
	{
		rawl=l;
		disl=FindDis(abovezero((u16)l),datal,250);
		
		rawr=r;
		disr=FindDis(abovezero((u16)r),datar,250);
	}
	else
	{
		rawlh=rawl;
		rawl=l;
		rawl=(rawl*3+rawlh*7)/10;
		
		rawrh=rawr;
		rawr=r;
		rawr=(rawr*3+rawrh*7)/10;
	}
}

double k,d;  

void ifRight(double logV1,double logV2,double logD1,double logD2)
{
	if(logD1==logD2||logV1==0||logV2==0||((logV1*logD2-logV2*logD1)==0))
	{
		LED1=1;LED2=1;LED3=1;LED4=1;LED5=1;LED6=1;LED7=1;LED8=1;
		while(1);      
	}
}


void CurveFit(double Y1,double X1,double Y2,double X2)
{
	d=(Y1*X2-Y2*X1)/(X2-X1);
	k=(Y1-d)/X1;
}


void CheckValue(u16 num,u16 infr[],u8 Name)
{
	u16 Vol,Dist; 
	u16 Dec_dist=0;
	double temp;
	switch(Name)
	{
		case F1:Dec_dist=20;break;
		case F2:Dec_dist=20;break;
		case L:Dec_dist=21;break;
		case R:Dec_dist=20;break;
		default:break;
	}
	for(Dist=0;Dist<(num-1);Dist++)
	{
		temp=k*log((double)(Dist+Dec_dist))+d;  
		Vol=exp(temp);
		infr[Dist]=(unsigned int)Vol;
	}
	infr[Dist]=0;
}


 
void InfCorrect(double vol1,double dist1,double vol2,double dist2,u16 num,u16 infr[],u8 Name)
{
	double logV1,logV2,logD1,logD2;
	logV1=log(vol1);
	logV2=log(vol2);
	logD1=log(dist1);
	logD2=log(dist2);   
	CurveFit(logV1,logD1,logV2,logD2);    
	CheckValue(num,infr,Name);
}





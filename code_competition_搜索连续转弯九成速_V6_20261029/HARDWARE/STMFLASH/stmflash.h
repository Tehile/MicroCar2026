#ifndef __STMFLASH_H__
#define __STMFLASH_H__
#include "sys.h"  
 
 
 
 
struct run_data
{
 
 
 
	u8 task;
	s16 gangle,speedl,speedr;	
 
};
typedef struct run_data RUN_DATA;

void erase_run_data(void);
void write_data(u16 num);
void save_data(u16 num);
void print_data(void);

 
 
#define STM32_FLASH_SIZE 	128 	 		 
#define STM32_FLASH_WREN 	1              	 
 

 
#define STM32_FLASH_BASE 0x08000000 	 
 
#define FLASH_KEY1               0X45670123
#define FLASH_KEY2               0XCDEF89AB
void STMFLASH_Unlock(void);					   
void STMFLASH_Lock(void);					   
u8 STMFLASH_GetStatus(void);				   
u8 STMFLASH_WaitDone(u16 time);				   
u8 STMFLASH_ErasePage(u32 paddr);			   
u8 STMFLASH_WriteHalfWord(u32 faddr, int dat); 
int STMFLASH_ReadHalfWord(u32 faddr);		   
void STMFLASH_WriteLenByte(u32 WriteAddr,u32 DataToWrite,u16 Len);	 
u32 STMFLASH_ReadLenByte(u32 ReadAddr,u16 Len);						 
void STMFLASH_Write(u32 WriteAddr,u16 *pBuffer,u16 NumToWrite);		 
void STMFLASH_Read(u32 ReadAddr,u16 *pBuffer,u16 NumToRead);   		 

 
void Test_Write(u32 WriteAddr,u16 WriteData);
void SaveBlockData(void);
void SaveSearch_Stack(void);
void Block_Write(void);

 
 
 

#define FLASH_ADDR_DATA_NUM  	0X08016000  
#define FLASH_ADDR_DATA   		0X08016400  

#define FLASH_ADDR_BLOCK_STACK   	0X0801D800  
#define FLASH_ADDR_BLOCK_POINT   	0X0801DC00  
#define FLASH_ADDR_BLOCK   	0X0801E000  
#define FLASH_ADDR_GOPATH_90   	0X0801E400  
#define FLASH_ADDR_BACKPATH_90   	0X0801E800  

#define FLASH_ADDR_IRDATA		0X0801EC00  
#define FLASH_ADDR_GOPATH   	0X0801F000  
#define FLASH_ADDR_BACKPATH   	0X0801F800  
#define FLASH_ADDR_GYRO0  		0X0801FC00  
								   
#endif

 
















using System;
using System.Management;

namespace 注册机;

internal class SoftReg
{
	public int[] intCode = new int[127];

	public char[] charCode = new char[25];

	public int[] intNumber = new int[25];

	public string GetDiskVolumeSerialNumber()
	{
		new ManagementClass("win32_NetworkAdapterConfiguration");
		ManagementObject managementObject = new ManagementObject("win32_logicaldisk.deviceid=\"c:\"");
		managementObject.Get();
		return managementObject.GetPropertyValue("VolumeSerialNumber").ToString();
	}

	public string GetCpu()
	{
		string strCpu = null;
		foreach (ManagementObject instance in new ManagementClass("win32_Processor").GetInstances())
		{
			strCpu = instance.Properties["Processorid"].Value.ToString();
		}
		return strCpu;
	}

	public string GetMNum()
	{
		return (GetCpu() + GetDiskVolumeSerialNumber()).Substring(0, 24);
	}

	public void SetIntCode()
	{
		for (int i = 1; i < intCode.Length; i++)
		{
			intCode[i] = i % 9;
		}
	}

	public string GetRNum()
	{
		SetIntCode();
		string strMNum = GetMNum();
		for (int i = 1; i < charCode.Length; i++)
		{
			charCode[i] = Convert.ToChar(strMNum.Substring(i - 1, 1));
		}
		for (int j = 1; j < intNumber.Length; j++)
		{
			intNumber[j] = Convert.ToInt32(charCode[j]) + intCode[Convert.ToInt32(charCode[j])];
		}
		string strAsciiName = "";
		for (int k = 1; k < intNumber.Length; k++)
		{
			strAsciiName = (((intNumber[k] < 48 || intNumber[k] > 57) && (intNumber[k] < 65 || intNumber[k] > 90) && (intNumber[k] < 97 || intNumber[k] > 122)) ? ((intNumber[k] <= 122) ? (strAsciiName + Convert.ToChar(intNumber[k] - 9)) : (strAsciiName + Convert.ToChar(intNumber[k] - 10))) : (strAsciiName + Convert.ToChar(intNumber[k])));
		}
		return strAsciiName;
	}
}

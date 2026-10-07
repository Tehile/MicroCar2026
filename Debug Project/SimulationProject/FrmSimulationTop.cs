using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.IO.Pipes;
using System.Linq;
using System.Net;
using System.Net.Sockets;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Formatters.Binary;
using System.Security.Principal;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using MicromouseSimulation;

namespace SimulationProject;

public class FrmSimulationTop : Form
{
	public enum UI
	{
		visitor,
		member,
		mapChecked,
		simReady,
		debugWait,
		run,
		pause,
		finish,
		waitReply
	}

	public enum EndFlag
	{
		run,
		finish,
		dllError,
		cancle,
		overTime
	}

	public delegate void invokeDelegateUI(UI ui);

	public delegate void invokeDelegateFlag(EndFlag flag);

	public delegate void invokeDelegateSend();

	private bool isEXELoad1;

	private bool isEXELoad2;

	private Map saveUserMap;

	public Draw draw;

	public Mouse mouse;

	private UI uiStatus;

	public string userID;

	public bool isUserMapCanChange;

	private bool canSend = true;

	public List<string> readBuffer;

	private double runTimeShow;

	private double mazeTimeShow;

	private double thisTimeShow;

	private double bestTimeShow;

	private bool intoEnd;

	private bool outStart;

	private int TimeStartRunNum;

	private EndFlag simulationEnd_Flag;

	public Graphics gMap;

	public bool isLoadEnd;

	public bool isOverTime;

	private NamedPipeClientStream pipeSend;

	private NamedPipeServerStream pipeRead;

	private NamedPipeClientStream pipeRead_q;

	private StreamWriter sw;

	private StreamReader sr;

	private string serverIP = "101.201.102.118";

	private int serverPost = 3389;

	private frmLogin from_login;

	private frmReg from_reg;

	private frmChangePassword from_changePassword;

	private frmRanking from_ranking;

	private Thread sendThread;

	private Thread readThread;

	private Thread createTalkThread;

	private bool canQuitSim;

	private bool sendQuitNew;

	private IContainer components;

	private GroupBox grpMethod;

	private Button btnReset;

	private Button btnOffline;

	private Button btnSaveMap;

	private Button btnLoadMap;

	private RadioButton rdoMethod4;

	private RadioButton rdoMethod2;

	private RadioButton rdoMethod1;

	private TrackBar trbSpeed;

	private Label label1;

	private GroupBox grpTimeSys;

	private Label label2;

	private GroupBox grpRealData;

	private TextBox txtPosition_real;

	private TextBox txtBestScore;

	private TextBox txtThisScore;

	private TextBox txtMazeTime;

	private Label label9;

	private Label label10;

	private Label label11;

	private Label label12;

	private TextBox txtRunTime;

	private Label label7;

	public PictureBox picMap;

	private OpenFileDialog openFileDialogUserMap;

	private System.Windows.Forms.Timer timer1;

	private GroupBox grpStartPoint;

	private RadioButton rdoMethodEx1;

	public RadioButton rdoRightDown;

	public RadioButton rdoRightUp;

	public RadioButton rdoLeftUp;

	public RadioButton rdoLeftDown;

	private GroupBox grpStartDir;

	public RadioButton rdoRight;

	public RadioButton rdoDown;

	public RadioButton rdoLeft;

	public RadioButton rdoUp;

	private GroupBox grpMouseDisplay;

	public CheckBox chkSearched_mouse;

	private RadioButton rdoMethod3;

	private OpenFileDialog openFileDialogDll;

	private SaveFileDialog saveFileDialogMap;

	private Button btnAbout;

	public CheckBox chkCrossway_mouse;

	public CheckBox chkFlood_mouse;

	public BackgroundWorker checkMapWorker;

	private Button btnChectMap;

	private Button btnDebug;

	private TextBox txtSearchP1;

	private GroupBox grpUserInfo;

	private Button btnQuit;

	private StatusStrip statusStrip1;

	private ToolStripStatusLabel toolStripStatusLabel1;

	private Label lblHello;

	private Button btnUpdateScore;

	private Button btnGetRanking;

	private Button btnLogin;

	private Button btnChangePassword;

	private Button btnReg;

	private ToolStripStatusLabel lblLogin_s;

	private ToolStripStatusLabel toolStripStatusLabel3;

	private ToolStripStatusLabel lblCheck_s;

	private ToolStripStatusLabel toolStripStatusLabel5;

	private ToolStripStatusLabel lblSim_s;

	private Button btnRunSim;

	private ToolStripStatusLabel toolStripStatusLabel7;

	private ToolStripStatusLabel lblRun_s;

	private ToolStripStatusLabel toolStripStatusLabel9;

	private ToolStripStatusLabel lblFinish_s;

	private ToolStripStatusLabel toolStripStatusLabel11;

	public BackgroundWorker loginWorker;

	public BackgroundWorker regWorker;

	public BackgroundWorker changePasswordWorker;

	public BackgroundWorker updateScoreWorker;

	public BackgroundWorker getRankingWorker;

	private OpenFileDialog openFileSendDll;

	private Label label5;

	private TextBox txtSearchP;

	private GroupBox grpMouseData;

	private TextBox txtAction;

	private TextBox txtStatus;

	private TextBox txtPosition_mouse;

	private TextBox txtUserMsg;

	private Label label18;

	private Label label19;

	private Label label20;

	private Label label21;

	private Label label3;

	private TextBox txtMapStatus;

	private RadioButton rdoMethodEx2;

	private GroupBox grpRealDisplay;

	public CheckBox chkCrossway_real;

	public CheckBox chkSearched_real;

	public CheckBox chkFlood_real;

	public UI UIStatus
	{
		get
		{
			return uiStatus;
		}
		set
		{
			uiStatus = value;
			switch (uiStatus)
			{
			case UI.visitor:
				mouse = new Mouse();
				draw = new Draw(mouse, picMap);
				sendThread = new Thread(sendTask);
				sendThread.IsBackground = true;
				readThread = new Thread(readTask);
				readThread.IsBackground = true;
				readBuffer = new List<string>();
				grpMouseDisplay.Enabled = false;
				grpRealDisplay.Enabled = false;
				grpMethod.Enabled = false;
				grpStartPoint.Enabled = false;
				grpStartDir.Enabled = false;
				grpTimeSys.Enabled = false;
				grpRealData.Enabled = false;
				grpMouseData.Enabled = false;
				grpUserInfo.Enabled = true;
				picMap.Enabled = false;
				trbSpeed.Enabled = false;
				btnLoadMap.Enabled = false;
				btnSaveMap.Enabled = false;
				btnQuit.Enabled = false;
				btnChectMap.Enabled = false;
				btnRunSim.Enabled = false;
				btnReset.Enabled = false;
				btnOffline.Enabled = false;
				btnDebug.Enabled = false;
				txtStatus.Text = "";
				txtAction.Text = "";
				txtPosition_mouse.Text = "";
				txtUserMsg.Text = "";
				txtPosition_real.Text = "";
				txtSearchP1.Text = "";
				txtSearchP.Text = "";
				txtMapStatus.Text = "";
				txtRunTime.Text = "";
				txtMazeTime.Text = "";
				txtThisScore.Text = "";
				txtBestScore.Text = "";
				lblHello.Text = "请登录账号";
				btnLogin.Enabled = true;
				btnReg.Enabled = true;
				btnChangePassword.Enabled = false;
				btnUpdateScore.Enabled = false;
				btnGetRanking.Enabled = false;
				btnRunSim.Text = "开始运行";
				btnDebug.Text = "实时调试";
				rdoLeftDown.Checked = false;
				rdoLeftDown.Checked = true;
				rdoUp.Checked = false;
				rdoUp.Checked = true;
				chkSearched_mouse.Checked = false;
				chkFlood_mouse.Checked = false;
				chkCrossway_mouse.Checked = false;
				rdoMethod1.Checked = true;
				timer1.Enabled = false;
				SimulationEnd_Flag = EndFlag.run;
				isUserMapCanChange = false;
				lblLogin_s.ForeColor = Color.DarkRed;
				lblCheck_s.ForeColor = Color.DarkRed;
				lblSim_s.ForeColor = Color.DarkRed;
				lblRun_s.ForeColor = Color.DarkRed;
				lblFinish_s.ForeColor = Color.DarkRed;
				break;
			case UI.member:
			{
				grpMouseDisplay.Enabled = true;
				grpRealDisplay.Enabled = true;
				grpMethod.Enabled = true;
				grpStartPoint.Enabled = true;
				grpStartDir.Enabled = true;
				grpTimeSys.Enabled = true;
				grpRealData.Enabled = true;
				grpMouseData.Enabled = true;
				picMap.Enabled = true;
				trbSpeed.Enabled = true;
				btnLoadMap.Enabled = true;
				btnSaveMap.Enabled = true;
				btnQuit.Enabled = false;
				btnChectMap.Enabled = true;
				btnRunSim.Enabled = false;
				btnReset.Enabled = false;
				btnOffline.Enabled = false;
				btnDebug.Enabled = false;
				grpUserInfo.Enabled = true;
				txtStatus.Text = "";
				txtAction.Text = "";
				txtPosition_mouse.Text = "";
				txtUserMsg.Text = "";
				txtPosition_real.Text = "";
				txtSearchP1.Text = "";
				txtSearchP.Text = "";
				txtMapStatus.Text = "";
				txtRunTime.Text = "";
				txtMazeTime.Text = "";
				txtThisScore.Text = "";
				txtBestScore.Text = "";
				lblHello.Text = "亲爱的 " + userID + " ，欢迎使用！";
				btnLogin.Enabled = false;
				btnReg.Enabled = false;
				btnChangePassword.Enabled = true;
				btnUpdateScore.Enabled = true;
				btnGetRanking.Enabled = true;
				btnRunSim.Text = "开始运行";
				btnDebug.Text = "实时调试";
				RadioButton oldStartPosition = (rdoLeftDown.Checked ? rdoLeftDown : (rdoLeftUp.Checked ? rdoLeftUp : ((!rdoRightUp.Checked) ? rdoRightDown : rdoRightUp)));
				RadioButton oldStartDir = (rdoUp.Checked ? rdoUp : (rdoDown.Checked ? rdoDown : ((!rdoLeft.Checked) ? rdoRight : rdoLeft)));
				oldStartPosition.Checked = false;
				oldStartPosition.Checked = true;
				oldStartDir.Checked = false;
				oldStartDir.Checked = true;
				timer1.Enabled = false;
				isUserMapCanChange = true;
				lblLogin_s.ForeColor = Color.ForestGreen;
				lblCheck_s.ForeColor = Color.DarkRed;
				lblSim_s.ForeColor = Color.DarkRed;
				lblRun_s.ForeColor = Color.DarkRed;
				lblFinish_s.ForeColor = Color.DarkRed;
				break;
			}
			case UI.mapChecked:
				grpMouseDisplay.Enabled = true;
				grpRealDisplay.Enabled = true;
				grpMethod.Enabled = true;
				grpStartPoint.Enabled = false;
				grpStartDir.Enabled = false;
				grpTimeSys.Enabled = true;
				grpRealData.Enabled = true;
				grpMouseData.Enabled = true;
				grpUserInfo.Enabled = true;
				picMap.Enabled = true;
				trbSpeed.Enabled = true;
				btnLoadMap.Enabled = false;
				btnSaveMap.Enabled = true;
				btnQuit.Enabled = false;
				btnChectMap.Enabled = false;
				btnRunSim.Enabled = false;
				btnReset.Enabled = true;
				btnOffline.Enabled = true;
				btnDebug.Enabled = true;
				btnLogin.Enabled = false;
				btnReg.Enabled = false;
				btnChangePassword.Enabled = true;
				btnUpdateScore.Enabled = true;
				btnGetRanking.Enabled = true;
				btnRunSim.Text = "开始运行";
				btnDebug.Text = "实时调试";
				timer1.Enabled = false;
				isUserMapCanChange = false;
				lblLogin_s.ForeColor = Color.ForestGreen;
				lblCheck_s.ForeColor = Color.ForestGreen;
				lblSim_s.ForeColor = Color.DarkRed;
				lblRun_s.ForeColor = Color.DarkRed;
				lblFinish_s.ForeColor = Color.DarkRed;
				break;
			case UI.simReady:
				btnLogin.Enabled = false;
				btnReg.Enabled = false;
				btnChangePassword.Enabled = false;
				btnUpdateScore.Enabled = false;
				btnGetRanking.Enabled = false;
				grpMouseDisplay.Enabled = true;
				grpMethod.Enabled = false;
				grpStartPoint.Enabled = false;
				grpStartDir.Enabled = false;
				grpTimeSys.Enabled = true;
				grpRealData.Enabled = true;
				grpMouseData.Enabled = true;
				grpUserInfo.Enabled = true;
				picMap.Enabled = true;
				trbSpeed.Enabled = true;
				btnLoadMap.Enabled = false;
				btnSaveMap.Enabled = true;
				btnChectMap.Enabled = false;
				btnRunSim.Enabled = true;
				btnQuit.Enabled = false;
				btnReset.Enabled = false;
				btnOffline.Enabled = false;
				btnDebug.Enabled = false;
				btnRunSim.Text = "开始运行";
				btnDebug.Text = "实时调试";
				timer1.Enabled = false;
				isUserMapCanChange = false;
				lblLogin_s.ForeColor = Color.ForestGreen;
				lblCheck_s.ForeColor = Color.ForestGreen;
				lblSim_s.ForeColor = Color.ForestGreen;
				lblRun_s.ForeColor = Color.DarkRed;
				lblFinish_s.ForeColor = Color.DarkRed;
				break;
			case UI.debugWait:
				btnLogin.Enabled = false;
				btnReg.Enabled = false;
				btnChangePassword.Enabled = false;
				btnUpdateScore.Enabled = false;
				btnGetRanking.Enabled = false;
				grpMouseDisplay.Enabled = false;
				grpRealDisplay.Enabled = false;
				grpMethod.Enabled = false;
				grpStartPoint.Enabled = false;
				grpStartDir.Enabled = false;
				grpTimeSys.Enabled = false;
				grpRealData.Enabled = false;
				grpMouseData.Enabled = false;
				grpUserInfo.Enabled = false;
				picMap.Enabled = false;
				trbSpeed.Enabled = false;
				btnLoadMap.Enabled = false;
				btnSaveMap.Enabled = false;
				btnQuit.Enabled = false;
				btnChectMap.Enabled = false;
				btnRunSim.Enabled = false;
				btnReset.Enabled = false;
				btnOffline.Enabled = false;
				btnDebug.Enabled = true;
				btnRunSim.Text = "开始运行";
				btnDebug.Text = "取消连接";
				timer1.Enabled = false;
				isUserMapCanChange = false;
				lblLogin_s.ForeColor = Color.ForestGreen;
				lblCheck_s.ForeColor = Color.ForestGreen;
				lblSim_s.ForeColor = Color.DarkRed;
				lblRun_s.ForeColor = Color.DarkRed;
				lblFinish_s.ForeColor = Color.DarkRed;
				break;
			case UI.run:
				btnLogin.Enabled = false;
				btnReg.Enabled = false;
				btnChangePassword.Enabled = false;
				btnUpdateScore.Enabled = false;
				btnGetRanking.Enabled = false;
				grpMouseDisplay.Enabled = true;
				grpRealDisplay.Enabled = true;
				grpMethod.Enabled = false;
				grpStartPoint.Enabled = false;
				grpStartDir.Enabled = false;
				grpTimeSys.Enabled = true;
				grpRealData.Enabled = true;
				grpMouseData.Enabled = true;
				grpUserInfo.Enabled = true;
				picMap.Enabled = true;
				trbSpeed.Enabled = true;
				btnLoadMap.Enabled = false;
				btnSaveMap.Enabled = false;
				btnQuit.Enabled = false;
				btnChectMap.Enabled = false;
				btnRunSim.Enabled = true;
				btnReset.Enabled = false;
				btnOffline.Enabled = false;
				btnDebug.Enabled = false;
				btnRunSim.Text = "暂停运行";
				btnDebug.Text = "实时调试";
				timer1.Enabled = true;
				isUserMapCanChange = false;
				lblLogin_s.ForeColor = Color.ForestGreen;
				lblCheck_s.ForeColor = Color.ForestGreen;
				lblSim_s.ForeColor = Color.ForestGreen;
				lblRun_s.ForeColor = Color.ForestGreen;
				lblFinish_s.ForeColor = Color.DarkRed;
				break;
			case UI.pause:
				btnLogin.Enabled = false;
				btnReg.Enabled = false;
				btnChangePassword.Enabled = false;
				btnUpdateScore.Enabled = false;
				btnGetRanking.Enabled = false;
				grpMouseDisplay.Enabled = true;
				grpRealDisplay.Enabled = true;
				grpMethod.Enabled = false;
				grpStartPoint.Enabled = false;
				grpStartDir.Enabled = false;
				grpTimeSys.Enabled = true;
				grpRealData.Enabled = true;
				grpMouseData.Enabled = true;
				grpUserInfo.Enabled = true;
				picMap.Enabled = true;
				trbSpeed.Enabled = true;
				btnLoadMap.Enabled = false;
				btnSaveMap.Enabled = true;
				btnQuit.Enabled = true;
				btnChectMap.Enabled = false;
				btnRunSim.Enabled = true;
				btnReset.Enabled = false;
				btnOffline.Enabled = false;
				btnDebug.Enabled = false;
				btnRunSim.Text = "继续运行";
				btnDebug.Text = "实时调试";
				timer1.Enabled = false;
				isUserMapCanChange = false;
				lblLogin_s.ForeColor = Color.ForestGreen;
				lblCheck_s.ForeColor = Color.ForestGreen;
				lblSim_s.ForeColor = Color.ForestGreen;
				lblRun_s.ForeColor = Color.ForestGreen;
				lblFinish_s.ForeColor = Color.DarkRed;
				break;
			case UI.finish:
				btnLogin.Enabled = false;
				btnReg.Enabled = false;
				btnChangePassword.Enabled = false;
				btnUpdateScore.Enabled = false;
				btnGetRanking.Enabled = false;
				grpMouseDisplay.Enabled = true;
				grpRealDisplay.Enabled = true;
				grpMethod.Enabled = false;
				grpStartPoint.Enabled = false;
				grpStartDir.Enabled = false;
				grpTimeSys.Enabled = true;
				grpRealData.Enabled = true;
				grpMouseData.Enabled = true;
				grpUserInfo.Enabled = true;
				picMap.Enabled = true;
				trbSpeed.Enabled = true;
				btnLoadMap.Enabled = false;
				btnSaveMap.Enabled = true;
				btnQuit.Enabled = false;
				btnChectMap.Enabled = false;
				btnRunSim.Enabled = false;
				btnReset.Enabled = true;
				btnOffline.Enabled = false;
				btnDebug.Enabled = false;
				btnRunSim.Text = "开始运行";
				btnDebug.Text = "实时调试";
				timer1.Enabled = false;
				isUserMapCanChange = false;
				lblLogin_s.ForeColor = Color.ForestGreen;
				lblCheck_s.ForeColor = Color.ForestGreen;
				lblSim_s.ForeColor = Color.ForestGreen;
				lblRun_s.ForeColor = Color.ForestGreen;
				lblFinish_s.ForeColor = Color.ForestGreen;
				if (sw != null)
				{
					sw.Dispose();
					sw = null;
				}
				if (sr != null)
				{
					sr.Dispose();
					sr = null;
				}
				if (pipeSend != null)
				{
					pipeSend.Dispose();
					pipeSend = null;
				}
				if (pipeRead != null)
				{
					pipeRead.Dispose();
					pipeRead = null;
				}
				break;
			case UI.waitReply:
				btnLogin.Enabled = false;
				btnReg.Enabled = false;
				btnChangePassword.Enabled = false;
				btnUpdateScore.Enabled = false;
				btnGetRanking.Enabled = false;
				grpMouseDisplay.Enabled = false;
				grpRealDisplay.Enabled = false;
				grpMethod.Enabled = false;
				grpStartPoint.Enabled = false;
				grpStartDir.Enabled = false;
				grpTimeSys.Enabled = false;
				grpRealData.Enabled = false;
				grpMouseData.Enabled = false;
				grpUserInfo.Enabled = false;
				picMap.Enabled = false;
				trbSpeed.Enabled = false;
				btnLoadMap.Enabled = false;
				btnSaveMap.Enabled = false;
				btnQuit.Enabled = false;
				btnChectMap.Enabled = false;
				btnRunSim.Enabled = false;
				btnReset.Enabled = false;
				btnOffline.Enabled = false;
				btnDebug.Enabled = false;
				break;
			}
			picMap.Invalidate();
		}
	}

	public EndFlag SimulationEnd_Flag
	{
		get
		{
			return simulationEnd_Flag;
		}
		set
		{
			if (simulationEnd_Flag == value)
			{
				return;
			}
			simulationEnd_Flag = value;
			Process[] processes;
			if (value == EndFlag.dllError)
			{
				processes = Process.GetProcesses();
				foreach (Process item in processes)
				{
					if (item.ProcessName == "1")
					{
						item.Kill();
					}
					if (item.ProcessName == "2")
					{
						item.Kill();
					}
					if (item.ProcessName == "3")
					{
						item.Kill();
					}
					if (item.ProcessName == "4")
					{
						item.Kill();
					}
					if (item.ProcessName == "5")
					{
						item.Kill();
					}
					if (item.ProcessName == "6")
					{
						item.Kill();
					}
				}
				btnLogin.Invoke(new invokeDelegateUI(invokeUI), UI.finish);
				MessageBox.Show("算法运行错误！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			}
			if (value != EndFlag.overTime)
			{
				return;
			}
			processes = Process.GetProcesses();
			foreach (Process item2 in processes)
			{
				if (item2.ProcessName == "1")
				{
					item2.Kill();
				}
				if (item2.ProcessName == "2")
				{
					item2.Kill();
				}
				if (item2.ProcessName == "3")
				{
					item2.Kill();
				}
				if (item2.ProcessName == "4")
				{
					item2.Kill();
				}
				if (item2.ProcessName == "5")
				{
					item2.Kill();
				}
				if (item2.ProcessName == "6")
				{
					item2.Kill();
				}
			}
			btnLogin.Invoke(new invokeDelegateUI(invokeUI), UI.finish);
			MessageBox.Show("算法超时！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
		}
	}

	private void rdoMethodEx1_CheckedChanged(object sender, EventArgs e)
	{
		if (rdoMethodEx1.Checked && !isEXELoad1)
		{
			openFileDialogDll.Filter = "用户算法(*.exe)|*.exe";
			openFileDialogDll.ShowDialog();
		}
	}

	private void rdoMethodEx3_CheckedChanged(object sender, EventArgs e)
	{
		if (rdoMethodEx2.Checked && !isEXELoad2)
		{
			openFileDialogDll.Filter = "用户算法(*.exe)|*.exe";
			openFileDialogDll.ShowDialog();
		}
	}

	private void openFileDialogDll_FileOk(object sender, CancelEventArgs e)
	{
		if (!isEXELoad1)
		{
			isEXELoad1 = true;
			rdoMethodEx1.Text = Path.GetFileNameWithoutExtension(openFileDialogDll.FileName);
			rdoMethodEx1.Enabled = true;
			rdoMethodEx1.Checked = true;
			string fullPath = Path.GetFullPath(openFileDialogDll.FileName);
			string destinationFile = AppDomain.CurrentDomain.SetupInformation.ApplicationBase + "5.exe";
			bool isrewrite = true;
			File.Copy(fullPath, destinationFile, isrewrite);
		}
		else if (!isEXELoad2)
		{
			isEXELoad2 = true;
			rdoMethodEx2.Text = Path.GetFileNameWithoutExtension(openFileDialogDll.FileName);
			rdoMethodEx2.Enabled = true;
			rdoMethodEx2.Checked = true;
			string fullPath2 = Path.GetFullPath(openFileDialogDll.FileName);
			string destinationFile2 = AppDomain.CurrentDomain.SetupInformation.ApplicationBase + "6.exe";
			bool isrewrite2 = true;
			File.Copy(fullPath2, destinationFile2, isrewrite2);
		}
	}

	private void btnAbout_Click(object sender, EventArgs e)
	{
		new Form3().ShowDialog(this);
	}

	public FrmSimulationTop()
	{
		InitializeComponent();
		gMap = picMap.CreateGraphics();
		UIStatus = UI.visitor;
		isEXELoad1 = false;
		isEXELoad2 = false;
		SetStyle(ControlStyles.ResizeRedraw | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, value: false);
	}

	private void frmSimulationTop_Load(object sender, EventArgs e)
	{
		UIStatus = UI.member;
	}

	private void rdoLeftDown_CheckedChanged(object sender, EventArgs e)
	{
		if (rdoLeftDown.Checked)
		{
			rdoUp.Enabled = true;
			rdoDown.Enabled = false;
			rdoLeft.Enabled = false;
			rdoRight.Enabled = true;
			mouse.x = 0;
			mouse.y = 0;
			mouse.xOld = 0;
			mouse.yOld = 0;
			mouse.Xcjm = 0;
			mouse.Ycjm = 0;
			mouse.xStart = 0;
			mouse.yStart = 0;
			rdoUp.Checked = false;
			rdoUp.Checked = true;
		}
	}

	private void rdoLeftUp_CheckedChanged(object sender, EventArgs e)
	{
		if (rdoLeftUp.Checked)
		{
			rdoUp.Enabled = false;
			rdoDown.Enabled = true;
			rdoLeft.Enabled = false;
			rdoRight.Enabled = true;
			mouse.x = 0;
			mouse.y = 15;
			mouse.Xcjm = 0;
			mouse.Ycjm = 15;
			mouse.xOld = 0;
			mouse.yOld = 15;
			mouse.xStart = 0;
			mouse.yStart = 15;
			rdoRight.Checked = false;
			rdoRight.Checked = true;
		}
	}

	private void rdoRightUp_CheckedChanged(object sender, EventArgs e)
	{
		if (rdoRightUp.Checked)
		{
			rdoUp.Enabled = false;
			rdoDown.Enabled = true;
			rdoLeft.Enabled = true;
			rdoRight.Enabled = false;
			mouse.x = 15;
			mouse.y = 15;
			mouse.Xcjm = 15;
			mouse.Ycjm = 15;
			mouse.xOld = 15;
			mouse.yOld = 15;
			mouse.xStart = 15;
			mouse.yStart = 15;
			rdoDown.Checked = false;
			rdoDown.Checked = true;
		}
	}

	private void rdoRightDown_CheckedChanged(object sender, EventArgs e)
	{
		if (rdoRightDown.Checked)
		{
			rdoUp.Enabled = true;
			rdoDown.Enabled = false;
			rdoLeft.Enabled = true;
			rdoRight.Enabled = false;
			mouse.x = 15;
			mouse.y = 0;
			mouse.Xcjm = 15;
			mouse.Ycjm = 0;
			mouse.xOld = 15;
			mouse.yOld = 0;
			mouse.xStart = 15;
			mouse.yStart = 0;
			rdoLeft.Checked = false;
			rdoLeft.Checked = true;
		}
	}

	private void rdoUp_CheckedChanged(object sender, EventArgs e)
	{
		if (rdoUp.Checked)
		{
			mouse.startDir = 0;
			mouse.absoluteDir = 0;
			mouse.Dircjm = 0;
			mouse.map.updateFlood(mouse.xStart, mouse.yStart);
			draw.updateMouseCenter();
			picMap.Invalidate();
		}
	}

	private void rdoDown_CheckedChanged(object sender, EventArgs e)
	{
		if (rdoDown.Checked)
		{
			mouse.startDir = 2;
			mouse.absoluteDir = 2;
			mouse.Dircjm = 2;
			mouse.map.updateFlood(mouse.xStart, mouse.yStart);
			draw.updateMouseCenter();
			picMap.Invalidate();
		}
	}

	private void rdoLeft_CheckedChanged(object sender, EventArgs e)
	{
		if (rdoLeft.Checked)
		{
			mouse.startDir = 1;
			mouse.absoluteDir = 1;
			mouse.Dircjm = 1;
			mouse.map.updateFlood(mouse.xStart, mouse.yStart);
			draw.updateMouseCenter();
			picMap.Invalidate();
		}
	}

	private void rdoRight_CheckedChanged(object sender, EventArgs e)
	{
		if (rdoRight.Checked)
		{
			mouse.startDir = 3;
			mouse.absoluteDir = 3;
			mouse.Dircjm = 3;
			mouse.map.updateFlood(mouse.xStart, mouse.yStart);
			draw.updateMouseCenter();
			picMap.Invalidate();
		}
	}

	private void btnLoadMap_Click(object sender, EventArgs e)
	{
		openFileDialogUserMap.Filter = "迷宫数据(*.bin)|*.bin";
		openFileDialogUserMap.ShowDialog();
	}

	private void openFileDialogUserMap_FileOk(object sender, CancelEventArgs e)
	{
		IFormatter formatter = new BinaryFormatter();
		Stream stream = openFileDialogUserMap.OpenFile();
		try
		{
			Map map_temp = (Map)formatter.Deserialize(stream);
			for (int i = 0; i < 16; i++)
			{
				for (int j = 0; j < 16; j++)
				{
					for (int k = 0; k < 4; k++)
					{
						mouse.map.blocks[i, j].baffle[k] = map_temp.blocks[i, j].baffle[k];
					}
				}
			}
			mouse.map.updateFlood(mouse.xStart, mouse.yStart);
		}
		catch
		{
			MessageBox.Show("迷宫文件损坏!");
		}
		stream.Close();
		picMap.Invalidate();
	}

	private void btnSaveMap_Click(object sender, EventArgs e)
	{
		saveFileDialogMap.Filter = "迷宫数据(*.bin)|*.bin";
		saveFileDialogMap.ShowDialog();
	}

	private void saveFileDialogMap_FileOk(object sender, CancelEventArgs e)
	{
		string path = saveFileDialogMap.FileName.ToString();
		IFormatter formatter = new BinaryFormatter();
		if (!string.IsNullOrEmpty(path))
		{
			Stream stream = new FileStream(path, FileMode.OpenOrCreate, FileAccess.Write, FileShare.None);
			formatter.Serialize(stream, mouse.map);
			stream.Close();
		}
		MessageBox.Show("迷宫保存成功!");
	}

	private void picMap_SizeChanged(object sender, EventArgs e)
	{
		picMap.Invalidate();
	}

	private void picMap_Paint(object sender, PaintEventArgs e)
	{
		_ = e.ClipRectangle;
		BufferedGraphics bufferedGraphics = BufferedGraphicsManager.Current.Allocate(e.Graphics, e.ClipRectangle);
		Graphics gBuffer = bufferedGraphics.Graphics;
		gBuffer.SmoothingMode = SmoothingMode.HighQuality;
		gBuffer.PixelOffsetMode = PixelOffsetMode.HighSpeed;
		gBuffer.Clear(picMap.BackColor);
		draw.Prepare(picMap);
		if (chkSearched_mouse.Checked)
		{
			draw.SearchedWay_mouse(gBuffer);
		}
		if (chkSearched_real.Checked)
		{
			draw.SearchedWay_real(gBuffer);
		}
		if (chkCrossway_mouse.Checked)
		{
			draw.CrossWay_mouse(gBuffer);
		}
		if (chkCrossway_real.Checked)
		{
			draw.CrossWay_real(gBuffer);
		}
		if (chkFlood_mouse.Checked)
		{
			draw.FloodNum_mouse(gBuffer);
		}
		if (chkFlood_real.Checked)
		{
			draw.FloodNum_real(gBuffer);
		}
		draw.EmptyMap(gBuffer);
		draw.RealMap(gBuffer);
		draw.MousePosture(gBuffer);
		bufferedGraphics.Render(e.Graphics);
		gBuffer.Dispose();
		bufferedGraphics.Dispose();
	}

	private void picMap_MouseClick(object sender, MouseEventArgs e)
	{
		if (isUserMapCanChange)
		{
			int x = picMap.PointToClient(Control.MousePosition).X;
			int y = picMap.PointToClient(Control.MousePosition).Y;
			if (draw.UpdateUserMapBlock(x, y))
			{
				picMap.Invalidate();
			}
		}
	}

	private void btnRunSim_Click(object sender, EventArgs e)
	{
		if (UIStatus == UI.run)
		{
			UIStatus = UI.pause;
		}
		else if (UIStatus == UI.pause)
		{
			UIStatus = UI.run;
		}
		else if (UIStatus == UI.simReady)
		{
			UIStatus = UI.run;
		}
	}

	private void btnDebug_Click(object sender, EventArgs e)
	{
		if (UIStatus == UI.mapChecked)
		{
			readBuffer = new List<string>();
			pipeRead = new NamedPipeServerStream("mouseDLL2Sim", PipeDirection.InOut, 1);
			pipeSend = new NamedPipeClientStream("localhost", "mouseSim2DLL", PipeDirection.InOut, PipeOptions.None, TokenImpersonationLevel.None);
			sw = new StreamWriter(pipeSend);
			sr = new StreamReader(pipeRead);
			sendQuitNew = false;
			canSend = true;
			sendThread = new Thread(sendTask);
			sendThread.IsBackground = true;
			readThread = new Thread(readTask);
			readThread.IsBackground = true;
			createTalkThread = new Thread(createTalkTask);
			createTalkThread.IsBackground = true;
			UIStatus = UI.debugWait;
			createTalkThread.Start();
		}
		else if (UIStatus == UI.debugWait)
		{
			pipeRead_q = new NamedPipeClientStream("localhost", "mouseDLL2Sim", PipeDirection.InOut, PipeOptions.None, TokenImpersonationLevel.None);
			pipeRead_q.Connect();
			createTalkThread.Abort();
			createTalkThread.Join();
			pipeRead_q.Dispose();
			pipeRead_q = null;
			UIStatus = UI.mapChecked;
		}
	}

	private void btnOffline_Click(object sender, EventArgs e)
	{
		try
		{
			readBuffer = new List<string>();
			pipeRead = new NamedPipeServerStream("mouseDLL2Sim", PipeDirection.InOut, 1);
			pipeSend = new NamedPipeClientStream("localhost", "mouseSim2DLL", PipeDirection.InOut, PipeOptions.None, TokenImpersonationLevel.None);
			sw = new StreamWriter(pipeSend);
			sr = new StreamReader(pipeRead);
			sendQuitNew = false;
			canSend = true;
			sendThread = new Thread(sendTask);
			sendThread.IsBackground = true;
			readThread = new Thread(readTask);
			readThread.IsBackground = true;
			createTalkThread = new Thread(createTalkTask);
			createTalkThread.IsBackground = true;
			createTalkThread.Start();
		}
		catch
		{
			if (sw != null)
			{
				sw.Dispose();
				sw = null;
			}
			if (sr != null)
			{
				sr.Dispose();
				sr = null;
			}
			if (pipeRead != null)
			{
				pipeRead.Dispose();
				pipeRead = null;
			}
			if (pipeSend != null)
			{
				pipeSend.Dispose();
				pipeSend = null;
			}
			MessageBox.Show("仿真通道未建立！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			return;
		}
		try
		{
			if (rdoMethod1.Checked)
			{
				Process.Start("1.exe");
			}
			else if (rdoMethod2.Checked)
			{
				Process.Start("2.exe");
			}
			else if (rdoMethod3.Checked)
			{
				Process.Start("3.exe");
			}
			else if (rdoMethod4.Checked)
			{
				Process.Start("4.exe");
			}
			else if (rdoMethodEx1.Checked)
			{
				Process.Start("5.exe");
			}
			else if (rdoMethodEx2.Checked)
			{
				Process.Start("6.exe");
			}
		}
		catch
		{
			MessageBox.Show("离线算法加载失败！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
		}
	}

	private void btnReset_Click(object sender, EventArgs e)
	{
		saveUserMap = mouse.map;
		if (!readThread.IsAlive && !sendThread.IsAlive)
		{
			readBuffer.Clear();
			mouse = new Mouse();
			for (int i = 0; i < 16; i++)
			{
				for (int j = 0; j < 16; j++)
				{
					for (int k = 0; k < 4; k++)
					{
						mouse.map.blocks[i, j].baffle[k] = saveUserMap.blocks[i, j].baffle[k];
					}
				}
			}
			draw = new Draw(mouse, picMap);
			SimulationEnd_Flag = EndFlag.run;
			UIStatus = UI.member;
		}
		else
		{
			MessageBox.Show("仿真未退出！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
		}
	}

	private void btnChectMap_Click(object sender, EventArgs e)
	{
		object[] arrObjects = new object[1] { mouse };
		checkMapWorker.RunWorkerAsync(arrObjects);
		UIStatus = UI.waitReply;
	}

	private void checkMapWorker_DoWork(object sender, DoWorkEventArgs e)
	{
		_ = (BackgroundWorker)sender;
		Mouse mouse = (Mouse)((object[])e.Argument)[0];
		int lostBlockNum;
		for (int i = 1; i < 15; i++)
		{
			for (int j = 1; j < 15; j++)
			{
				lostBlockNum = 0;
				if (mouse.map.blocks[i - 1, j - 1].baffle[0])
				{
					lostBlockNum++;
				}
				if (mouse.map.blocks[i - 1, j - 1].baffle[3])
				{
					lostBlockNum++;
				}
				if (mouse.map.blocks[i, j].baffle[1])
				{
					lostBlockNum++;
				}
				if (mouse.map.blocks[i, j].baffle[2])
				{
					lostBlockNum++;
				}
				if (i == 8 && j == 8)
				{
					if (lostBlockNum != 0)
					{
						string errorStr = "终点状态不合法！";
						e.Result = errorStr.ToString();
						return;
					}
				}
				else if (lostBlockNum == 0)
				{
					string errorStr = "立柱(" + i + "," + j + ")为孤立状态！";
					e.Result = errorStr.ToString();
					return;
				}
			}
		}
		lostBlockNum = 0;
		for (int k = 0; k < 16; k++)
		{
			if (!mouse.map.blocks[0, k].baffle[1])
			{
				lostBlockNum++;
			}
			if (!mouse.map.blocks[15, k].baffle[3])
			{
				lostBlockNum++;
			}
			if (!mouse.map.blocks[k, 0].baffle[2])
			{
				lostBlockNum++;
			}
			if (!mouse.map.blocks[k, 15].baffle[0])
			{
				lostBlockNum++;
			}
		}
		if (lostBlockNum != 0)
		{
			string errorStr = "迷宫外围不封闭！存在" + lostBlockNum + "处缺口！";
			e.Result = errorStr.ToString();
			return;
		}
		int rightHand = 0;
		int leftHand = 0;
		switch (mouse.startDir)
		{
		case 0:
			rightHand = 3;
			leftHand = 1;
			break;
		case 1:
			rightHand = 0;
			leftHand = 2;
			break;
		case 2:
			rightHand = 1;
			leftHand = 3;
			break;
		case 3:
			rightHand = 2;
			leftHand = 0;
			break;
		}
		if (!mouse.map.blocks[mouse.xStart, mouse.yStart].baffle[rightHand])
		{
			string errorStr = "发车起点右侧应有墙壁！";
			e.Result = errorStr.ToString();
			return;
		}
		if (!mouse.map.blocks[mouse.xStart, mouse.yStart].baffle[leftHand])
		{
			string errorStr = "发车起点左侧应有墙壁！";
			e.Result = errorStr.ToString();
			return;
		}
		int mapEndBlockNum = 0;
		if (mouse.map.blocks[7, 7].baffle[1])
		{
			mapEndBlockNum++;
		}
		if (mouse.map.blocks[7, 7].baffle[2])
		{
			mapEndBlockNum++;
		}
		if (mouse.map.blocks[7, 8].baffle[0])
		{
			mapEndBlockNum++;
		}
		if (mouse.map.blocks[7, 8].baffle[1])
		{
			mapEndBlockNum++;
		}
		if (mouse.map.blocks[8, 7].baffle[2])
		{
			mapEndBlockNum++;
		}
		if (mouse.map.blocks[8, 7].baffle[3])
		{
			mapEndBlockNum++;
		}
		if (mouse.map.blocks[8, 8].baffle[0])
		{
			mapEndBlockNum++;
		}
		if (mouse.map.blocks[8, 8].baffle[3])
		{
			mapEndBlockNum++;
		}
		if (mapEndBlockNum != 7)
		{
			string errorStr = "终点状态不合法！";
			e.Result = errorStr.ToString();
			return;
		}
		mouse.map.updateFlood(mouse.xStart, mouse.yStart);
		if (mouse.map.blocks[7, 7].flood == 255 || mouse.map.blocks[7, 8].flood == 255 || mouse.map.blocks[8, 7].flood == 255 || mouse.map.blocks[8, 8].flood == 255)
		{
			string errorStr = "迷宫终点不可达！";
			e.Result = errorStr.ToString();
		}
	}

	private void checkMapWorker_ProgressChanged(object sender, ProgressChangedEventArgs e)
	{
	}

	private void checkMapWorker_RunWorkerCompleted(object sender, RunWorkerCompletedEventArgs e)
	{
		if (e.Result != null)
		{
			MessageBox.Show((string)e.Result, "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			UIStatus = UI.member;
		}
		else
		{
			UIStatus = UI.mapChecked;
		}
	}

	private int translateInfo()
	{
		if (int.Parse(readBuffer[0]) == 4)
		{
			return 0;
		}
		if (int.Parse(readBuffer[0]) == 5)
		{
			return 0;
		}
		try
		{
			mouse.actionCjm.Add(int.Parse(readBuffer[0]));
			mouse.checkCjm(mouse.map);
			List<string> list_position = readBuffer[1].Split(' ').ToList();
			list_position.Remove("");
			list_position.Remove(" ");
			mouse.userX = int.Parse(list_position[0]);
			mouse.userY = int.Parse(list_position[1]);
			if (mouse.actionError)
			{
				if (SimulationEnd_Flag == EndFlag.run)
				{
					btnLogin.Invoke(new invokeDelegateFlag(invokeEndFlag), EndFlag.dllError);
					return 2;
				}
			}
			else
			{
				mouse.actionCjm2.Add(mouse.actionCjm[0]);
				mouse.actionCjm.Clear();
				mouse.stateCjm = int.Parse(readBuffer[2]);
				List<string> list_searched = readBuffer[3].Split(' ').ToList();
				list_searched.Remove("");
				list_searched.Remove(" ");
				for (int i = 0; i < list_searched.Count; i++)
				{
					mouse.searchedBuffer.Add(int.Parse(list_searched[i]));
				}
				List<string> list_crossway = readBuffer[4].Split(' ').ToList();
				list_crossway.Remove("");
				list_crossway.Remove(" ");
				int i2;
				for (i2 = 0; i2 < list_crossway.Count; i2++)
				{
					mouse.crosswayXBuffer.Add(int.Parse(list_crossway[i2]));
					i2++;
					mouse.crosswayYBuffer.Add(int.Parse(list_crossway[i2]));
				}
				List<string> list_flood = readBuffer[5].Split(' ').ToList();
				list_flood.Remove("");
				list_flood.Remove(" ");
				for (int j = 0; j < list_flood.Count; j++)
				{
					mouse.floodBuffer.Add(list_flood[j]);
				}
			}
			return 1;
		}
		catch
		{
			return 2;
		}
	}

	private string getWallState()
	{
		string wallState = "";
		int left = ((mouse.Dircjm != 3) ? (mouse.Dircjm + 1) : 0);
		int front = mouse.Dircjm;
		int right = ((mouse.Dircjm != 0) ? (mouse.Dircjm - 1) : 3);
		wallState = ((!mouse.map.blocks[mouse.Xcjm, mouse.Ycjm].baffle[left]) ? (wallState + "0") : (wallState + "1"));
		wallState = ((!mouse.map.blocks[mouse.Xcjm, mouse.Ycjm].baffle[front]) ? (wallState + "0") : (wallState + "1"));
		if (mouse.map.blocks[mouse.Xcjm, mouse.Ycjm].baffle[right])
		{
			return wallState + "1";
		}
		return wallState + "0";
	}

	private void timer1_Tick(object sender, EventArgs e)
	{
		if (draw.actionX.Count == 1 && mouse.actionCjm2.Count != 0)
		{
			mouse.map.blocks[mouse.x, mouse.y].isSearched = true;
			mouse.xOld = mouse.x;
			mouse.yOld = mouse.y;
			if (mouse.actionCjm2[0] == 0)
			{
				draw.MoveOneBlockAction();
				mouse.MoveOneBlockAction(draw.length);
				txtAction.Text = "正向直行1格";
			}
			else if (mouse.actionCjm2[0] == 1)
			{
				draw.StepTurnLeftAction();
				mouse.StepTurnLeftAction(draw.length);
				txtAction.Text = "原地左转90度";
			}
			else if (mouse.actionCjm2[0] == 2)
			{
				draw.StepTurnRightAction();
				mouse.StepTurnRightAction(draw.length);
				txtAction.Text = "原地右转90度";
			}
			else if (mouse.actionCjm2[0] == 3)
			{
				draw.TurnBackAction();
				mouse.TurnBackAction(draw.length);
				txtAction.Text = "后转180度";
			}
			else if (SimulationEnd_Flag == EndFlag.run)
			{
				SimulationEnd_Flag = EndFlag.dllError;
			}
			txtPosition_mouse.Text = "(" + mouse.userX + "," + mouse.userY + ")";
			mouse.searchedList = new List<int>(mouse.searchedBuffer);
			mouse.crosswayX = new List<int>(mouse.crosswayXBuffer);
			mouse.crosswayY = new List<int>(mouse.crosswayYBuffer);
			mouse.updateCrossway();
			mouse.floodDataList = new List<string>(mouse.floodBuffer);
			mouse.stateTimerCjm = mouse.stateCjm;
			switch (mouse.stateTimerCjm)
			{
			case 0:
				txtStatus.Text = "WAIT";
				break;
			case 1:
				txtStatus.Text = "START";
				break;
			case 2:
				txtStatus.Text = "MAZESEARCH";
				break;
			case 3:
				txtStatus.Text = "SPURT";
				break;
			case 4:
				txtStatus.Text = "END";
				break;
			}
			txtPosition_real.Text = "(" + mouse.xOld + "," + mouse.yOld + ")-->(" + mouse.x + "," + mouse.y + ")";
			int searchNum = 0;
			for (int i = 0; i < 16; i++)
			{
				for (int j = 0; j < 16; j++)
				{
					if (mouse.map.blocks[i, j].isSearched)
					{
						searchNum++;
					}
				}
			}
			double searchP = (double)searchNum * 100.0 / 256.0;
			txtSearchP.Text = searchP.ToString("0.00") + "%";
			double searchP2 = (double)searchNum * 100.0 / (double)(256 - mouse.map.deadArea);
			txtSearchP1.Text = searchP2.ToString("0.00") + "%";
			txtMapStatus.Text = mouse.map.deadArea + " 格无效";
			if ((mouse.xOld != mouse.x || mouse.yOld != mouse.y) && (mouse.x != mouse.xStart || mouse.y != mouse.yStart))
			{
				if (((mouse.x == 7 && mouse.y == 7) || (mouse.x == 7 && mouse.y == 8) || (mouse.x == 8 && mouse.y == 7) || (mouse.x == 8 && mouse.y == 8)) && (mouse.xOld != 7 || mouse.yOld != 7) && (mouse.xOld != 7 || mouse.yOld != 8) && (mouse.xOld != 8 || mouse.yOld != 7) && (mouse.xOld != 8 || mouse.yOld != 8))
				{
					intoEnd = true;
				}
				else if (mouse.xOld == mouse.xStart && mouse.yOld == mouse.yStart)
				{
					outStart = true;
				}
				else if (((mouse.xOld == 7 && mouse.yOld == 7) || (mouse.xOld == 7 && mouse.yOld == 8) || (mouse.xOld == 8 && mouse.yOld == 7) || (mouse.xOld == 8 && mouse.yOld == 8)) && (mouse.x != 7 || mouse.y != 7) && (mouse.x != 7 || mouse.y != 8) && (mouse.x != 8 || mouse.y != 7) && mouse.x == 8)
				{
					_ = mouse.y;
					_ = 8;
				}
			}
			mouse.actionCjm2.RemoveAt(0);
			mouse.crosswayXBuffer.Clear();
			mouse.crosswayYBuffer.Clear();
			mouse.floodBuffer.Clear();
			mouse.searchedBuffer.Clear();
		}
		if (draw.actionX.Count > 1)
		{
			mouse.runTime++;
			mouse.mazeTime++;
			if (outStart)
			{
				if (TimeStartRunNum == 0)
				{
					mouse.mazeTime = 0.0;
				}
				mouse.runTime = 0.0;
			}
			if (intoEnd)
			{
				mouse.thisTime = mouse.mazeTime / 30.0 + mouse.runTime;
				if (mouse.thisTime < mouse.bestTime || mouse.bestTime < 0.01)
				{
					mouse.bestTime = mouse.thisTime;
				}
			}
			runTimeShow = mouse.runTime / (double)mouse.speed / 2.0;
			mazeTimeShow = mouse.mazeTime / (double)mouse.speed / 2.0;
			thisTimeShow = mouse.thisTime / (double)mouse.speed / 2.0;
			bestTimeShow = mouse.bestTime / (double)mouse.speed / 2.0;
			if (mazeTimeShow >= 900.0)
			{
				SimulationEnd_Flag = EndFlag.overTime;
			}
			txtRunTime.Text = runTimeShow.ToString("0.00") + " 秒";
			txtMazeTime.Text = mazeTimeShow.ToString("0.00") + " 秒";
			txtThisScore.Text = thisTimeShow.ToString("0.00") + " 秒";
			txtBestScore.Text = bestTimeShow.ToString("0.00") + " 秒";
			if (outStart)
			{
				outStart = false;
				TimeStartRunNum++;
			}
			if (intoEnd)
			{
				intoEnd = false;
			}
			draw.actionX.RemoveAt(0);
			draw.actionY.RemoveAt(0);
			draw.actionD.RemoveAt(0);
		}
		if (draw.actionX.Count == 1 && SimulationEnd_Flag == EndFlag.finish)
		{
			timer1.Enabled = false;
			UIStatus = UI.finish;
			MessageBox.Show("仿真完成！", "信息", MessageBoxButtons.OK, MessageBoxIcon.Asterisk);
		}
		picMap.Invalidate();
	}

	private void FrmSimulationTop_Paint(object sender, PaintEventArgs e)
	{
	}

	private void textBox11_TextChanged(object sender, EventArgs e)
	{
	}

	private void trackBar1_Scroll(object sender, EventArgs e)
	{
		timer1.Interval = 101 - trbSpeed.Value;
	}

	private void groupBox3_Enter(object sender, EventArgs e)
	{
	}

	private void grpMethod_Enter(object sender, EventArgs e)
	{
	}

	private void picMap_Click(object sender, EventArgs e)
	{
	}

	private void rdoRealMap_CheckedChanged(object sender, EventArgs e)
	{
		picMap.Invalidate();
	}

	private void rdoOpenMap_CheckedChanged(object sender, EventArgs e)
	{
		picMap.Invalidate();
	}

	private void rdoCloseMap_CheckedChanged(object sender, EventArgs e)
	{
		picMap.Invalidate();
	}

	private void chkSearched_CheckedChanged(object sender, EventArgs e)
	{
		if (chkSearched_mouse.Checked)
		{
			chkSearched_real.Checked = false;
		}
		picMap.Invalidate();
	}

	private void chkFlood_CheckedChanged(object sender, EventArgs e)
	{
		if (chkFlood_mouse.Checked)
		{
			chkFlood_real.Checked = false;
		}
		picMap.Invalidate();
	}

	private void chkCrossway_CheckedChanged(object sender, EventArgs e)
	{
		if (chkCrossway_mouse.Checked)
		{
			chkCrossway_real.Checked = false;
		}
		picMap.Invalidate();
	}

	private void toolStripStatusLabel6_Click(object sender, EventArgs e)
	{
	}

	private bool send_message(string message, Socket socket)
	{
		try
		{
			socket.Send(Encoding.UTF8.GetBytes(message + "#"));
		}
		catch
		{
			return false;
		}
		return true;
	}

	private bool send_file(string file_name, Socket socket)
	{
		try
		{
			long file_size = new FileInfo(file_name).Length;
			socket.Send(Encoding.UTF8.GetBytes(file_size + "#"));
			FileStream reader = new FileStream(file_name, FileMode.Open, FileAccess.Read, FileShare.None);
			byte[] fileBuffer = new byte[1];
			for (long i = 0L; i < file_size; i++)
			{
				reader.Read(fileBuffer, 0, 1);
				socket.Send(fileBuffer, 0, 1, SocketFlags.None);
			}
			reader.Close();
		}
		catch
		{
			return false;
		}
		return true;
	}

	private bool get_message(ref string get_buffer, Socket socket)
	{
		try
		{
			byte[] recvBytes = new byte[1024];
			while (true)
			{
				socket.Receive(recvBytes, 1, SocketFlags.None);
				string temp_str = Encoding.UTF8.GetString(recvBytes, 0, 1);
				if (!(temp_str == "#"))
				{
					get_buffer += temp_str;
					continue;
				}
				break;
			}
		}
		catch
		{
			return false;
		}
		return true;
	}

	private bool get_file(string file_name, Socket socket)
	{
		try
		{
			string file_size = "";
			byte[] fileBuffer = new byte[1];
			FileStream writer = new FileStream(Path.Combine(file_name), FileMode.Create, FileAccess.Write, FileShare.None);
			for (long i = 0L; i < long.Parse(file_size); i++)
			{
				socket.Receive(fileBuffer, 1, SocketFlags.None);
				writer.Write(fileBuffer, 0, 1);
				writer.Flush();
			}
			writer.Close();
		}
		catch
		{
			return false;
		}
		return true;
	}

	private void btnLogin_Click(object sender, EventArgs e)
	{
		from_login = new frmLogin(this);
		from_login.ShowDialog(this);
	}

	private void loginWorker_DoWork(object sender, DoWorkEventArgs e)
	{
		BackgroundWorker loginWorker = (BackgroundWorker)sender;
		object[] obj = (object[])e.Argument;
		string command = (string)obj[0];
		string userID = (string)obj[1];
		string password = (string)obj[2];
		IPAddress ip = IPAddress.Parse(serverIP);
		Socket clientSocket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
		try
		{
			clientSocket.Connect(new IPEndPoint(ip, serverPost));
			if (!send_message(command, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			if (!send_message(userID, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			if (!send_message(password, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			string answer = "";
			if (!get_message(ref answer, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			e.Result = new object[1] { answer };
			clientSocket.Close();
		}
		catch
		{
			e.Result = new object[1] { "0" };
			clientSocket.Close();
			return;
		}
		loginWorker.ReportProgress(100);
	}

	private void loginWorker_RunWorkerCompleted(object sender, RunWorkerCompletedEventArgs e)
	{
		userID = from_login.userID;
		try
		{
			FileStream fileStream = new FileStream("info.txt", FileMode.Create);
			StreamWriter streamWriter = new StreamWriter(fileStream);
			streamWriter.WriteLine(userID);
			streamWriter.Flush();
			streamWriter.Close();
			fileStream.Close();
		}
		catch
		{
			MessageBox.Show("记住密码失败！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
		}
		from_login.Close();
		UIStatus = UI.member;

		from_login.btnOK.Enabled = true;
	}

	private void loginWorker_ProgressChanged(object sender, ProgressChangedEventArgs e)
	{
	}

	private void btnReg_Click(object sender, EventArgs e)
	{
		from_reg = new frmReg(this);
		from_reg.ShowDialog(this);
	}

	private void regWorker_DoWork(object sender, DoWorkEventArgs e)
	{
		BackgroundWorker regWorker = (BackgroundWorker)sender;
		object[] obj = (object[])e.Argument;
		string command = (string)obj[0];
		string userID = (string)obj[1];
		string password = (string)obj[2];
		IPAddress ip = IPAddress.Parse(serverIP);
		Socket clientSocket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
		try
		{
			clientSocket.Connect(new IPEndPoint(ip, serverPost));
			if (!send_message(command, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			if (!send_message(userID, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			if (!send_message(password, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			string answer = "";
			if (!get_message(ref answer, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			e.Result = new object[1] { answer };
			clientSocket.Close();
		}
		catch
		{
			e.Result = new object[1] { "0" };
			clientSocket.Close();
			return;
		}
		regWorker.ReportProgress(100);
	}

	private void regWorker_ProgressChanged(object sender, ProgressChangedEventArgs e)
	{
	}

	private void regWorker_RunWorkerCompleted(object sender, RunWorkerCompletedEventArgs e)
	{
		switch ((string)((object[])e.Result)[0])
		{
		case "0":
			MessageBox.Show("无法连接服务器！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			break;
		case "1":
			MessageBox.Show("注册成功，请联系管理员激活！", "通知", MessageBoxButtons.OK, MessageBoxIcon.Asterisk);
			break;
		case "2":
			MessageBox.Show("注册失败，用户已注册，已激活！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			break;
		case "3":
			MessageBox.Show("注册失败，用户已注册，未激活！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			break;
		}
		from_reg.btnOK.Enabled = true;
	}

	private void btnChangePassword_Click(object sender, EventArgs e)
	{
		from_changePassword = new frmChangePassword(this);
		from_changePassword.ShowDialog(this);
	}

	private void changePasswordWorker_DoWork(object sender, DoWorkEventArgs e)
	{
		BackgroundWorker changePasswordWorker = (BackgroundWorker)sender;
		object[] obj = (object[])e.Argument;
		string command = (string)obj[0];
		string userID = (string)obj[1];
		string password_old = (string)obj[2];
		string password_new = (string)obj[3];
		IPAddress ip = IPAddress.Parse(serverIP);
		Socket clientSocket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
		try
		{
			clientSocket.Connect(new IPEndPoint(ip, serverPost));
			if (!send_message(command, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			if (!send_message(userID, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			if (!send_message(password_old, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			if (!send_message(password_new, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			string answer = "";
			if (!get_message(ref answer, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			e.Result = new object[1] { answer };
			clientSocket.Close();
		}
		catch
		{
			e.Result = new object[1] { "0" };
			clientSocket.Close();
			return;
		}
		changePasswordWorker.ReportProgress(100);
	}

	private void changePasswordWorker_ProgressChanged(object sender, ProgressChangedEventArgs e)
	{
	}

	private void changePasswordWorker_RunWorkerCompleted(object sender, RunWorkerCompletedEventArgs e)
	{
		switch ((string)((object[])e.Result)[0])
		{
		case "0":
			MessageBox.Show("无法连接服务器！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			break;
		case "1":
			MessageBox.Show("密码修改成功！", "通知", MessageBoxButtons.OK, MessageBoxIcon.Asterisk);
			from_changePassword.Close();
			break;
		case "2":
			MessageBox.Show("旧密码错误！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			break;
		case "3":
			MessageBox.Show("用户不存在！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			break;
		}
	}

	private void openFileSendDll_FileOk(object sender, CancelEventArgs e)
	{
		string command = "4";
		string dllFileName = Path.GetFullPath(openFileSendDll.FileName);
		if (new FileInfo(dllFileName).Length > 500000)
		{
			MessageBox.Show("算法文件过大，应在500KB以内！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			return;
		}
		object[] arrObjects = new object[3] { command, userID, dllFileName };
		updateScoreWorker.RunWorkerAsync(arrObjects);
	}

	private void btnUpdateScore_Click(object sender, EventArgs e)
	{
		openFileSendDll.Filter = "用户算法(*.exe)|*.exe";
		openFileSendDll.ShowDialog();
	}

	private void updateScoreWorker_DoWork(object sender, DoWorkEventArgs e)
	{
		BackgroundWorker updateScoreWorker = (BackgroundWorker)sender;
		object[] obj = (object[])e.Argument;
		string command = (string)obj[0];
		string userID = (string)obj[1];
		string dllFileName = (string)obj[2];
		IPAddress ip = IPAddress.Parse(serverIP);
		Socket clientSocket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
		try
		{
			clientSocket.Connect(new IPEndPoint(ip, serverPost));
			if (!send_message(command, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			if (!send_message(userID, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			if (!send_file(dllFileName, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			string answer = "";
			if (!get_message(ref answer, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			e.Result = new object[1] { answer };
			clientSocket.Close();
		}
		catch
		{
			e.Result = new object[1] { "0" };
			clientSocket.Close();
			return;
		}
		updateScoreWorker.ReportProgress(100);
	}

	private void updateScoreWorker_ProgressChanged(object sender, ProgressChangedEventArgs e)
	{
	}

	private void updateScoreWorker_RunWorkerCompleted(object sender, RunWorkerCompletedEventArgs e)
	{
		switch ((string)((object[])e.Result)[0])
		{
		case "0":
			MessageBox.Show("无法连接服务器！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			break;
		case "1":
			MessageBox.Show("最好成绩已更新！", "信息", MessageBoxButtons.OK, MessageBoxIcon.Asterisk);
			break;
		case "2":
			MessageBox.Show("最好成绩未更新！", "信息", MessageBoxButtons.OK, MessageBoxIcon.Asterisk);
			break;
		case "3":
			MessageBox.Show("用户不存在！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			break;
		}
	}

	private void btnGetRanking_Click(object sender, EventArgs e)
	{
		string command = "5";
		object[] arrObjects = new object[2] { command, userID };
		getRankingWorker.RunWorkerAsync(arrObjects);
		from_ranking = new frmRanking(this);
		from_ranking.ShowDialog(this);
	}

	private void getRankingWorker_DoWork(object sender, DoWorkEventArgs e)
	{
		BackgroundWorker getRankingWorker = (BackgroundWorker)sender;
		object[] obj = (object[])e.Argument;
		string command = (string)obj[0];
		string userID = (string)obj[1];
		List<string> answer_rankList = new List<string>();
		IPAddress ip = IPAddress.Parse(serverIP);
		Socket clientSocket = new Socket(AddressFamily.InterNetwork, SocketType.Stream, ProtocolType.Tcp);
		try
		{
			clientSocket.Connect(new IPEndPoint(ip, serverPost));
			if (!send_message(command, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			if (!send_message(userID, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			string answer = "";
			if (!get_message(ref answer, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			if (answer == "2")
			{
				e.Result = new object[1] { answer };
				clientSocket.Close();
				return;
			}
			string rankListNumStr = "";
			if (!get_message(ref rankListNumStr, clientSocket))
			{
				e.Result = new object[1] { "0" };
				clientSocket.Close();
				return;
			}
			int rankListNum = int.Parse(rankListNumStr);
			for (int i = 0; i < rankListNum; i++)
			{
				string tempMessage = "";
				if (!get_message(ref tempMessage, clientSocket))
				{
					e.Result = new object[1] { "0" };
					clientSocket.Close();
					return;
				}
				answer_rankList.Add(tempMessage);
			}
			e.Result = new object[2] { answer, answer_rankList };
			clientSocket.Close();
		}
		catch
		{
			e.Result = new object[1] { "0" };
			clientSocket.Close();
			return;
		}
		getRankingWorker.ReportProgress(100);
	}

	private void getRankingWorker_ProgressChanged(object sender, ProgressChangedEventArgs e)
	{
	}

	private void getRankingWorker_RunWorkerCompleted(object sender, RunWorkerCompletedEventArgs e)
	{
		object[] arrObjects = (object[])e.Result;
		switch ((string)arrObjects[0])
		{
		case "0":
			MessageBox.Show("无法连接服务器！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			break;
		case "2":
			MessageBox.Show("用户不存在！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			break;
		case "1":
			from_ranking.rankingList = (List<string>)arrObjects[1];
			from_ranking.update_data();
			break;
		}
		from_ranking.btnUpdate.Enabled = true;
	}

	private void rdoMethod1_CheckedChanged(object sender, EventArgs e)
	{
	}

	public void sendTask()
	{
		while (true)
		{
			Thread.Sleep(1);
			if (!canSend || mouse.actionCjm2.Count != 0)
			{
				continue;
			}
			try
			{
				string sendStr = getWallState();
				sw.WriteLine(sendStr + "#");
				sw.Flush();
				canSend = false;
			}
			catch
			{
				if (SimulationEnd_Flag == EndFlag.run)
				{
					btnLogin.Invoke(new invokeDelegateFlag(invokeEndFlag), EndFlag.dllError);
				}
				break;
			}
		}
	}

	public void readTask()
	{
		int count = 0;
		try
		{
			while (true)
			{
				Thread.Sleep(1);
				string temp = sr.ReadLine();
				if (temp == null)
				{
					break;
				}
				readBuffer.Add(temp);
				count++;
				if (count != 6)
				{
					continue;
				}
				switch (translateInfo())
				{
				case 0:
					if (SimulationEnd_Flag == EndFlag.run)
					{
						btnLogin.Invoke(new invokeDelegateFlag(invokeEndFlag), EndFlag.finish);
					}
					sendQuitNew = true;
					btnLogin.Invoke(new invokeDelegateSend(keilSend));
					return;
				case 2:
					if (SimulationEnd_Flag == EndFlag.run)
					{
						btnLogin.Invoke(new invokeDelegateFlag(invokeEndFlag), EndFlag.dllError);
					}
					sendQuitNew = true;
					btnLogin.Invoke(new invokeDelegateSend(keilSend));
					return;
				default:
					readBuffer.Clear();
					count = 0;
					canSend = true;
					break;
				}
			}
			if (SimulationEnd_Flag == EndFlag.run)
			{
				btnLogin.Invoke(new invokeDelegateFlag(invokeEndFlag), EndFlag.dllError);
			}
			sendQuitNew = true;
			btnLogin.Invoke(new invokeDelegateSend(keilSend));
		}
		catch
		{
			if (SimulationEnd_Flag == EndFlag.run)
			{
				btnLogin.Invoke(new invokeDelegateFlag(invokeEndFlag), EndFlag.dllError);
			}
			sendQuitNew = true;
			btnLogin.Invoke(new invokeDelegateSend(keilSend));
		}
	}

	public void createTalkTask()
	{
		try
		{
			pipeRead.WaitForConnection();
			pipeRead.ReadMode = PipeTransmissionMode.Byte;
			if (pipeRead_q != null)
			{
				pipeRead.Dispose();
				pipeRead = null;
				pipeSend.Dispose();
				pipeSend = null;
			}
			else
			{
				pipeSend.Connect();
				sendThread.Start();
				readThread.Start();
				btnLogin.Invoke(new invokeDelegateUI(invokeUI), UI.simReady);
			}
		}
		catch
		{
			if (pipeRead != null)
			{
				pipeRead.Dispose();
				pipeRead = null;
			}
			if (pipeRead != null)
			{
				pipeSend.Dispose();
				pipeSend = null;
			}
			btnLogin.Invoke(new invokeDelegateUI(invokeUI), UI.mapChecked);
		}
	}

	private void button1_Click(object sender, EventArgs e)
	{
		Process[] processes = Process.GetProcesses();
		foreach (Process item in processes)
		{
			if (item.ProcessName == "1")
			{
				item.Kill();
			}
			if (item.ProcessName == "2")
			{
				item.Kill();
			}
			if (item.ProcessName == "3")
			{
				item.Kill();
			}
			if (item.ProcessName == "4")
			{
				item.Kill();
			}
			if (item.ProcessName == "5")
			{
				item.Kill();
			}
			if (item.ProcessName == "6")
			{
				item.Kill();
			}
		}
	}

	private void timer2_Tick(object sender, EventArgs e)
	{
		if (sendQuitNew && sendThread.IsAlive)
		{
			sendThread.Abort();
			sendQuitNew = false;
		}
	}

	public void keilSend()
	{
		if (sendQuitNew && sendThread.IsAlive)
		{
			sendThread.Abort();
			sendQuitNew = false;
		}
	}

	public void invokeUI(UI ui)
	{
		UIStatus = ui;
	}

	public void invokeEndFlag(EndFlag flag)
	{
		SimulationEnd_Flag = flag;
	}

	private void button2_Click(object sender, EventArgs e)
	{
	}

	private void button3_Click(object sender, EventArgs e)
	{
		Process.Start("1.exe");
	}

	private void btnQuit_Click(object sender, EventArgs e)
	{
		Process[] processes = Process.GetProcesses();
		foreach (Process item in processes)
		{
			if (item.ProcessName == "1")
			{
				item.Kill();
			}
			if (item.ProcessName == "2")
			{
				item.Kill();
			}
			if (item.ProcessName == "3")
			{
				item.Kill();
			}
			if (item.ProcessName == "4")
			{
				item.Kill();
			}
			if (item.ProcessName == "5")
			{
				item.Kill();
			}
			if (item.ProcessName == "6")
			{
				item.Kill();
			}
		}
		UIStatus = UI.finish;
	}

	private void checkBox1_CheckedChanged(object sender, EventArgs e)
	{
		picMap.Invalidate();
	}

	private void chkSearched_real_CheckedChanged(object sender, EventArgs e)
	{
		if (chkSearched_real.Checked)
		{
			chkSearched_mouse.Checked = false;
		}
		picMap.Invalidate();
	}

	private void chkFlood_real_CheckedChanged(object sender, EventArgs e)
	{
		if (chkFlood_real.Checked)
		{
			mouse.map.updateFlood(mouse.xStart, mouse.yStart);
			chkFlood_mouse.Checked = false;
		}
		picMap.Invalidate();
	}

	private void chkCrossway_real_CheckedChanged(object sender, EventArgs e)
	{
		if (chkCrossway_real.Checked)
		{
			chkCrossway_mouse.Checked = false;
		}
		picMap.Invalidate();
	}

	private void rdoMethod4_CheckedChanged(object sender, EventArgs e)
	{
	}

	protected override void Dispose(bool disposing)
	{
		if (disposing && components != null)
		{
			components.Dispose();
		}
		base.Dispose(disposing);
	}

	private void InitializeComponent()
	{
		this.components = new System.ComponentModel.Container();
		this.picMap = new System.Windows.Forms.PictureBox();
		this.txtPosition_real = new System.Windows.Forms.TextBox();
		this.grpRealData = new System.Windows.Forms.GroupBox();
		this.label3 = new System.Windows.Forms.Label();
		this.label2 = new System.Windows.Forms.Label();
		this.label5 = new System.Windows.Forms.Label();
		this.txtSearchP1 = new System.Windows.Forms.TextBox();
		this.txtMapStatus = new System.Windows.Forms.TextBox();
		this.txtSearchP = new System.Windows.Forms.TextBox();
		this.label7 = new System.Windows.Forms.Label();
		this.btnChectMap = new System.Windows.Forms.Button();
		this.btnDebug = new System.Windows.Forms.Button();
		this.btnAbout = new System.Windows.Forms.Button();
		this.grpMouseDisplay = new System.Windows.Forms.GroupBox();
		this.chkCrossway_mouse = new System.Windows.Forms.CheckBox();
		this.chkSearched_mouse = new System.Windows.Forms.CheckBox();
		this.chkFlood_mouse = new System.Windows.Forms.CheckBox();
		this.grpStartDir = new System.Windows.Forms.GroupBox();
		this.rdoRight = new System.Windows.Forms.RadioButton();
		this.rdoDown = new System.Windows.Forms.RadioButton();
		this.rdoLeft = new System.Windows.Forms.RadioButton();
		this.rdoUp = new System.Windows.Forms.RadioButton();
		this.label1 = new System.Windows.Forms.Label();
		this.grpStartPoint = new System.Windows.Forms.GroupBox();
		this.rdoRightDown = new System.Windows.Forms.RadioButton();
		this.rdoRightUp = new System.Windows.Forms.RadioButton();
		this.rdoLeftUp = new System.Windows.Forms.RadioButton();
		this.rdoLeftDown = new System.Windows.Forms.RadioButton();
		this.trbSpeed = new System.Windows.Forms.TrackBar();
		this.btnReset = new System.Windows.Forms.Button();
		this.grpMethod = new System.Windows.Forms.GroupBox();
		this.rdoMethodEx2 = new System.Windows.Forms.RadioButton();
		this.rdoMethod3 = new System.Windows.Forms.RadioButton();
		this.rdoMethodEx1 = new System.Windows.Forms.RadioButton();
		this.rdoMethod4 = new System.Windows.Forms.RadioButton();
		this.rdoMethod2 = new System.Windows.Forms.RadioButton();
		this.rdoMethod1 = new System.Windows.Forms.RadioButton();
		this.btnOffline = new System.Windows.Forms.Button();
		this.btnSaveMap = new System.Windows.Forms.Button();
		this.btnLoadMap = new System.Windows.Forms.Button();
		this.grpTimeSys = new System.Windows.Forms.GroupBox();
		this.txtBestScore = new System.Windows.Forms.TextBox();
		this.txtThisScore = new System.Windows.Forms.TextBox();
		this.txtMazeTime = new System.Windows.Forms.TextBox();
		this.label9 = new System.Windows.Forms.Label();
		this.label10 = new System.Windows.Forms.Label();
		this.label11 = new System.Windows.Forms.Label();
		this.label12 = new System.Windows.Forms.Label();
		this.txtRunTime = new System.Windows.Forms.TextBox();
		this.openFileDialogUserMap = new System.Windows.Forms.OpenFileDialog();
		this.timer1 = new System.Windows.Forms.Timer(this.components);
		this.openFileDialogDll = new System.Windows.Forms.OpenFileDialog();
		this.saveFileDialogMap = new System.Windows.Forms.SaveFileDialog();
		this.checkMapWorker = new System.ComponentModel.BackgroundWorker();
		this.grpUserInfo = new System.Windows.Forms.GroupBox();
		this.btnChangePassword = new System.Windows.Forms.Button();
		this.btnReg = new System.Windows.Forms.Button();
		this.lblHello = new System.Windows.Forms.Label();
		this.btnUpdateScore = new System.Windows.Forms.Button();
		this.btnGetRanking = new System.Windows.Forms.Button();
		this.btnLogin = new System.Windows.Forms.Button();
		this.btnQuit = new System.Windows.Forms.Button();
		this.statusStrip1 = new System.Windows.Forms.StatusStrip();
		this.toolStripStatusLabel1 = new System.Windows.Forms.ToolStripStatusLabel();
		this.lblLogin_s = new System.Windows.Forms.ToolStripStatusLabel();
		this.toolStripStatusLabel3 = new System.Windows.Forms.ToolStripStatusLabel();
		this.lblCheck_s = new System.Windows.Forms.ToolStripStatusLabel();
		this.toolStripStatusLabel5 = new System.Windows.Forms.ToolStripStatusLabel();
		this.lblSim_s = new System.Windows.Forms.ToolStripStatusLabel();
		this.toolStripStatusLabel7 = new System.Windows.Forms.ToolStripStatusLabel();
		this.lblRun_s = new System.Windows.Forms.ToolStripStatusLabel();
		this.toolStripStatusLabel9 = new System.Windows.Forms.ToolStripStatusLabel();
		this.lblFinish_s = new System.Windows.Forms.ToolStripStatusLabel();
		this.toolStripStatusLabel11 = new System.Windows.Forms.ToolStripStatusLabel();
		this.btnRunSim = new System.Windows.Forms.Button();
		this.loginWorker = new System.ComponentModel.BackgroundWorker();
		this.regWorker = new System.ComponentModel.BackgroundWorker();
		this.changePasswordWorker = new System.ComponentModel.BackgroundWorker();
		this.updateScoreWorker = new System.ComponentModel.BackgroundWorker();
		this.getRankingWorker = new System.ComponentModel.BackgroundWorker();
		this.openFileSendDll = new System.Windows.Forms.OpenFileDialog();
		this.grpMouseData = new System.Windows.Forms.GroupBox();
		this.txtAction = new System.Windows.Forms.TextBox();
		this.txtStatus = new System.Windows.Forms.TextBox();
		this.txtPosition_mouse = new System.Windows.Forms.TextBox();
		this.txtUserMsg = new System.Windows.Forms.TextBox();
		this.label18 = new System.Windows.Forms.Label();
		this.label19 = new System.Windows.Forms.Label();
		this.label20 = new System.Windows.Forms.Label();
		this.label21 = new System.Windows.Forms.Label();
		this.grpRealDisplay = new System.Windows.Forms.GroupBox();
		this.chkCrossway_real = new System.Windows.Forms.CheckBox();
		this.chkSearched_real = new System.Windows.Forms.CheckBox();
		this.chkFlood_real = new System.Windows.Forms.CheckBox();
		((System.ComponentModel.ISupportInitialize)this.picMap).BeginInit();
		this.grpRealData.SuspendLayout();
		this.grpMouseDisplay.SuspendLayout();
		this.grpStartDir.SuspendLayout();
		this.grpStartPoint.SuspendLayout();
		((System.ComponentModel.ISupportInitialize)this.trbSpeed).BeginInit();
		this.grpMethod.SuspendLayout();
		this.grpTimeSys.SuspendLayout();
		this.grpUserInfo.SuspendLayout();
		this.statusStrip1.SuspendLayout();
		this.grpMouseData.SuspendLayout();
		this.grpRealDisplay.SuspendLayout();
		base.SuspendLayout();
		this.picMap.Location = new System.Drawing.Point(12, 12);
		this.picMap.Name = "picMap";
		this.picMap.Size = new System.Drawing.Size(685, 688);
		this.picMap.TabIndex = 0;
		this.picMap.TabStop = false;
		this.picMap.SizeChanged += new System.EventHandler(picMap_SizeChanged);
		this.picMap.Click += new System.EventHandler(picMap_Click);
		this.picMap.Paint += new System.Windows.Forms.PaintEventHandler(picMap_Paint);
		this.picMap.MouseClick += new System.Windows.Forms.MouseEventHandler(picMap_MouseClick);
		this.txtPosition_real.Anchor = System.Windows.Forms.AnchorStyles.Bottom | System.Windows.Forms.AnchorStyles.Left;
		this.txtPosition_real.Location = new System.Drawing.Point(111, 30);
		this.txtPosition_real.Name = "txtPosition_real";
		this.txtPosition_real.ReadOnly = true;
		this.txtPosition_real.Size = new System.Drawing.Size(117, 21);
		this.txtPosition_real.TabIndex = 20;
		this.txtPosition_real.TabStop = false;
		this.txtPosition_real.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.txtPosition_real.TextChanged += new System.EventHandler(textBox11_TextChanged);
		this.grpRealData.Controls.Add(this.txtPosition_real);
		this.grpRealData.Controls.Add(this.label3);
		this.grpRealData.Controls.Add(this.label2);
		this.grpRealData.Controls.Add(this.label5);
		this.grpRealData.Controls.Add(this.txtSearchP1);
		this.grpRealData.Controls.Add(this.txtMapStatus);
		this.grpRealData.Controls.Add(this.txtSearchP);
		this.grpRealData.Controls.Add(this.label7);
		this.grpRealData.Location = new System.Drawing.Point(703, 200);
		this.grpRealData.Name = "grpRealData";
		this.grpRealData.Size = new System.Drawing.Size(268, 191);
		this.grpRealData.TabIndex = 1;
		this.grpRealData.TabStop = false;
		this.grpRealData.Text = "真实数据";
		this.label3.Anchor = System.Windows.Forms.AnchorStyles.Bottom | System.Windows.Forms.AnchorStyles.Left;
		this.label3.AutoSize = true;
		this.label3.Location = new System.Drawing.Point(44, 154);
		this.label3.Name = "label3";
		this.label3.Size = new System.Drawing.Size(53, 12);
		this.label3.TabIndex = 10;
		this.label3.Text = "地图状态";
		this.label2.Anchor = System.Windows.Forms.AnchorStyles.Bottom | System.Windows.Forms.AnchorStyles.Left;
		this.label2.AutoSize = true;
		this.label2.Location = new System.Drawing.Point(44, 74);
		this.label2.Margin = new System.Windows.Forms.Padding(3);
		this.label2.Name = "label2";
		this.label2.Size = new System.Drawing.Size(53, 12);
		this.label2.TabIndex = 9;
		this.label2.Text = "有效搜索";
		this.label5.Anchor = System.Windows.Forms.AnchorStyles.Bottom | System.Windows.Forms.AnchorStyles.Left;
		this.label5.AutoSize = true;
		this.label5.Location = new System.Drawing.Point(44, 114);
		this.label5.Name = "label5";
		this.label5.Size = new System.Drawing.Size(53, 12);
		this.label5.TabIndex = 12;
		this.label5.Text = "全图搜索";
		this.txtSearchP1.Anchor = System.Windows.Forms.AnchorStyles.Bottom | System.Windows.Forms.AnchorStyles.Left;
		this.txtSearchP1.Location = new System.Drawing.Point(111, 70);
		this.txtSearchP1.Name = "txtSearchP1";
		this.txtSearchP1.ReadOnly = true;
		this.txtSearchP1.Size = new System.Drawing.Size(117, 21);
		this.txtSearchP1.TabIndex = 30;
		this.txtSearchP1.TabStop = false;
		this.txtSearchP1.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.txtMapStatus.Anchor = System.Windows.Forms.AnchorStyles.Bottom | System.Windows.Forms.AnchorStyles.Left;
		this.txtMapStatus.Location = new System.Drawing.Point(111, 150);
		this.txtMapStatus.Name = "txtMapStatus";
		this.txtMapStatus.ReadOnly = true;
		this.txtMapStatus.Size = new System.Drawing.Size(117, 21);
		this.txtMapStatus.TabIndex = 25;
		this.txtMapStatus.TabStop = false;
		this.txtMapStatus.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.txtSearchP.Anchor = System.Windows.Forms.AnchorStyles.Bottom | System.Windows.Forms.AnchorStyles.Left;
		this.txtSearchP.Location = new System.Drawing.Point(111, 110);
		this.txtSearchP.Name = "txtSearchP";
		this.txtSearchP.ReadOnly = true;
		this.txtSearchP.Size = new System.Drawing.Size(117, 21);
		this.txtSearchP.TabIndex = 29;
		this.txtSearchP.TabStop = false;
		this.txtSearchP.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.label7.Anchor = System.Windows.Forms.AnchorStyles.Bottom | System.Windows.Forms.AnchorStyles.Left;
		this.label7.AutoSize = true;
		this.label7.Location = new System.Drawing.Point(44, 34);
		this.label7.Name = "label7";
		this.label7.Size = new System.Drawing.Size(53, 12);
		this.label7.TabIndex = 20;
		this.label7.Text = "坐标更新";
		this.btnChectMap.Location = new System.Drawing.Point(1009, 668);
		this.btnChectMap.Name = "btnChectMap";
		this.btnChectMap.Size = new System.Drawing.Size(89, 23);
		this.btnChectMap.TabIndex = 14;
		this.btnChectMap.TabStop = false;
		this.btnChectMap.Text = "验证迷宫";
		this.btnChectMap.UseVisualStyleBackColor = true;
		this.btnChectMap.Click += new System.EventHandler(btnChectMap_Click);
		this.btnDebug.Location = new System.Drawing.Point(1124, 668);
		this.btnDebug.Name = "btnDebug";
		this.btnDebug.Size = new System.Drawing.Size(89, 23);
		this.btnDebug.TabIndex = 13;
		this.btnDebug.TabStop = false;
		this.btnDebug.Text = "实时调试";
		this.btnDebug.UseVisualStyleBackColor = true;
		this.btnDebug.Click += new System.EventHandler(btnDebug_Click);
		this.btnAbout.Location = new System.Drawing.Point(32, 143);
		this.btnAbout.Name = "btnAbout";
		this.btnAbout.Size = new System.Drawing.Size(89, 23);
		this.btnAbout.TabIndex = 6;
		this.btnAbout.TabStop = false;
		this.btnAbout.Text = "关于我们";
		this.btnAbout.UseVisualStyleBackColor = true;
		this.btnAbout.Click += new System.EventHandler(btnAbout_Click);
		this.grpMouseDisplay.Controls.Add(this.chkCrossway_mouse);
		this.grpMouseDisplay.Controls.Add(this.chkSearched_mouse);
		this.grpMouseDisplay.Controls.Add(this.chkFlood_mouse);
		this.grpMouseDisplay.Location = new System.Drawing.Point(703, 397);
		this.grpMouseDisplay.Name = "grpMouseDisplay";
		this.grpMouseDisplay.Size = new System.Drawing.Size(115, 148);
		this.grpMouseDisplay.TabIndex = 6;
		this.grpMouseDisplay.TabStop = false;
		this.grpMouseDisplay.Text = "内部数据显示";
		this.chkCrossway_mouse.AutoSize = true;
		this.chkCrossway_mouse.Location = new System.Drawing.Point(17, 71);
		this.chkCrossway_mouse.Name = "chkCrossway_mouse";
		this.chkCrossway_mouse.Size = new System.Drawing.Size(84, 16);
		this.chkCrossway_mouse.TabIndex = 5;
		this.chkCrossway_mouse.TabStop = false;
		this.chkCrossway_mouse.Text = "岔路口信息";
		this.chkCrossway_mouse.UseVisualStyleBackColor = true;
		this.chkCrossway_mouse.CheckedChanged += new System.EventHandler(chkCrossway_CheckedChanged);
		this.chkSearched_mouse.AutoSize = true;
		this.chkSearched_mouse.Location = new System.Drawing.Point(17, 33);
		this.chkSearched_mouse.Name = "chkSearched_mouse";
		this.chkSearched_mouse.Size = new System.Drawing.Size(84, 16);
		this.chkSearched_mouse.TabIndex = 1;
		this.chkSearched_mouse.TabStop = false;
		this.chkSearched_mouse.Text = "已搜索区域";
		this.chkSearched_mouse.UseVisualStyleBackColor = true;
		this.chkSearched_mouse.CheckedChanged += new System.EventHandler(chkSearched_CheckedChanged);
		this.chkFlood_mouse.AutoSize = true;
		this.chkFlood_mouse.Location = new System.Drawing.Point(17, 109);
		this.chkFlood_mouse.Name = "chkFlood_mouse";
		this.chkFlood_mouse.Size = new System.Drawing.Size(84, 16);
		this.chkFlood_mouse.TabIndex = 4;
		this.chkFlood_mouse.TabStop = false;
		this.chkFlood_mouse.Text = "坐标洪水值";
		this.chkFlood_mouse.UseVisualStyleBackColor = true;
		this.chkFlood_mouse.CheckedChanged += new System.EventHandler(chkFlood_CheckedChanged);
		this.grpStartDir.Controls.Add(this.rdoRight);
		this.grpStartDir.Controls.Add(this.rdoDown);
		this.grpStartDir.Controls.Add(this.rdoLeft);
		this.grpStartDir.Controls.Add(this.rdoUp);
		this.grpStartDir.Location = new System.Drawing.Point(977, 460);
		this.grpStartDir.Name = "grpStartDir";
		this.grpStartDir.Size = new System.Drawing.Size(268, 57);
		this.grpStartDir.TabIndex = 12;
		this.grpStartDir.TabStop = false;
		this.grpStartDir.Text = "发车方向";
		this.rdoRight.AutoSize = true;
		this.rdoRight.Location = new System.Drawing.Point(202, 26);
		this.rdoRight.Name = "rdoRight";
		this.rdoRight.Size = new System.Drawing.Size(47, 16);
		this.rdoRight.TabIndex = 8;
		this.rdoRight.Text = "向右";
		this.rdoRight.UseVisualStyleBackColor = true;
		this.rdoRight.CheckedChanged += new System.EventHandler(rdoRight_CheckedChanged);
		this.rdoDown.AutoSize = true;
		this.rdoDown.Location = new System.Drawing.Point(82, 26);
		this.rdoDown.Name = "rdoDown";
		this.rdoDown.Size = new System.Drawing.Size(47, 16);
		this.rdoDown.TabIndex = 6;
		this.rdoDown.Text = "向下";
		this.rdoDown.UseVisualStyleBackColor = true;
		this.rdoDown.CheckedChanged += new System.EventHandler(rdoDown_CheckedChanged);
		this.rdoLeft.AutoSize = true;
		this.rdoLeft.Location = new System.Drawing.Point(142, 26);
		this.rdoLeft.Name = "rdoLeft";
		this.rdoLeft.Size = new System.Drawing.Size(47, 16);
		this.rdoLeft.TabIndex = 7;
		this.rdoLeft.Text = "向左";
		this.rdoLeft.UseVisualStyleBackColor = true;
		this.rdoLeft.CheckedChanged += new System.EventHandler(rdoLeft_CheckedChanged);
		this.rdoUp.AutoSize = true;
		this.rdoUp.Location = new System.Drawing.Point(22, 26);
		this.rdoUp.Name = "rdoUp";
		this.rdoUp.Size = new System.Drawing.Size(47, 16);
		this.rdoUp.TabIndex = 5;
		this.rdoUp.Text = "向上";
		this.rdoUp.UseVisualStyleBackColor = true;
		this.rdoUp.CheckedChanged += new System.EventHandler(rdoUp_CheckedChanged);
		this.label1.AutoSize = true;
		this.label1.Location = new System.Drawing.Point(984, 528);
		this.label1.Name = "label1";
		this.label1.Size = new System.Drawing.Size(53, 12);
		this.label1.TabIndex = 7;
		this.label1.Text = "仿真速度";
		this.grpStartPoint.Controls.Add(this.rdoRightDown);
		this.grpStartPoint.Controls.Add(this.rdoRightUp);
		this.grpStartPoint.Controls.Add(this.rdoLeftUp);
		this.grpStartPoint.Controls.Add(this.rdoLeftDown);
		this.grpStartPoint.Location = new System.Drawing.Point(977, 397);
		this.grpStartPoint.Name = "grpStartPoint";
		this.grpStartPoint.Size = new System.Drawing.Size(268, 57);
		this.grpStartPoint.TabIndex = 11;
		this.grpStartPoint.TabStop = false;
		this.grpStartPoint.Text = "起点位置";
		this.rdoRightDown.AutoSize = true;
		this.rdoRightDown.Location = new System.Drawing.Point(202, 26);
		this.rdoRightDown.Name = "rdoRightDown";
		this.rdoRightDown.Size = new System.Drawing.Size(47, 16);
		this.rdoRightDown.TabIndex = 4;
		this.rdoRightDown.Text = "右下";
		this.rdoRightDown.UseVisualStyleBackColor = true;
		this.rdoRightDown.CheckedChanged += new System.EventHandler(rdoRightDown_CheckedChanged);
		this.rdoRightUp.AutoSize = true;
		this.rdoRightUp.Location = new System.Drawing.Point(142, 26);
		this.rdoRightUp.Name = "rdoRightUp";
		this.rdoRightUp.Size = new System.Drawing.Size(47, 16);
		this.rdoRightUp.TabIndex = 3;
		this.rdoRightUp.Text = "右上";
		this.rdoRightUp.UseVisualStyleBackColor = true;
		this.rdoRightUp.CheckedChanged += new System.EventHandler(rdoRightUp_CheckedChanged);
		this.rdoLeftUp.AutoSize = true;
		this.rdoLeftUp.Location = new System.Drawing.Point(82, 26);
		this.rdoLeftUp.Name = "rdoLeftUp";
		this.rdoLeftUp.Size = new System.Drawing.Size(47, 16);
		this.rdoLeftUp.TabIndex = 2;
		this.rdoLeftUp.Text = "左上";
		this.rdoLeftUp.UseVisualStyleBackColor = true;
		this.rdoLeftUp.CheckedChanged += new System.EventHandler(rdoLeftUp_CheckedChanged);
		this.rdoLeftDown.AutoSize = true;
		this.rdoLeftDown.Location = new System.Drawing.Point(22, 26);
		this.rdoLeftDown.Name = "rdoLeftDown";
		this.rdoLeftDown.Size = new System.Drawing.Size(47, 16);
		this.rdoLeftDown.TabIndex = 1;
		this.rdoLeftDown.Text = "左下";
		this.rdoLeftDown.UseVisualStyleBackColor = true;
		this.rdoLeftDown.CheckedChanged += new System.EventHandler(rdoLeftDown_CheckedChanged);
		this.trbSpeed.AutoSize = false;
		this.trbSpeed.Location = new System.Drawing.Point(1043, 523);
		this.trbSpeed.Maximum = 100;
		this.trbSpeed.Minimum = 1;
		this.trbSpeed.Name = "trbSpeed";
		this.trbSpeed.Size = new System.Drawing.Size(202, 23);
		this.trbSpeed.TabIndex = 6;
		this.trbSpeed.TabStop = false;
		this.trbSpeed.Value = 1;
		this.trbSpeed.Scroll += new System.EventHandler(trackBar1_Scroll);
		this.btnReset.Location = new System.Drawing.Point(1124, 596);
		this.btnReset.Name = "btnReset";
		this.btnReset.Size = new System.Drawing.Size(89, 23);
		this.btnReset.TabIndex = 5;
		this.btnReset.TabStop = false;
		this.btnReset.Text = "清空数据";
		this.btnReset.UseVisualStyleBackColor = true;
		this.btnReset.Click += new System.EventHandler(btnReset_Click);
		this.grpMethod.Controls.Add(this.rdoMethodEx2);
		this.grpMethod.Controls.Add(this.rdoMethod3);
		this.grpMethod.Controls.Add(this.rdoMethodEx1);
		this.grpMethod.Controls.Add(this.rdoMethod4);
		this.grpMethod.Controls.Add(this.rdoMethod2);
		this.grpMethod.Controls.Add(this.rdoMethod1);
		this.grpMethod.Location = new System.Drawing.Point(824, 397);
		this.grpMethod.Name = "grpMethod";
		this.grpMethod.Size = new System.Drawing.Size(147, 303);
		this.grpMethod.TabIndex = 0;
		this.grpMethod.TabStop = false;
		this.grpMethod.Text = "离线算法";
		this.grpMethod.Enter += new System.EventHandler(grpMethod_Enter);
		this.rdoMethodEx2.AutoSize = true;
		this.rdoMethodEx2.Location = new System.Drawing.Point(16, 259);
		this.rdoMethodEx2.Name = "rdoMethodEx2";
		this.rdoMethodEx2.Size = new System.Drawing.Size(119, 16);
		this.rdoMethodEx2.TabIndex = 6;
		this.rdoMethodEx2.Text = "外部算法(未加载)";
		this.rdoMethodEx2.UseVisualStyleBackColor = true;
		this.rdoMethodEx2.CheckedChanged += new System.EventHandler(rdoMethodEx3_CheckedChanged);
		this.rdoMethod3.AutoSize = true;
		this.rdoMethod3.Location = new System.Drawing.Point(16, 124);
		this.rdoMethod3.Name = "rdoMethod3";
		this.rdoMethod3.Size = new System.Drawing.Size(119, 16);
		this.rdoMethod3.TabIndex = 4;
		this.rdoMethod3.Text = "中右法则(搜全图)";
		this.rdoMethod3.UseVisualStyleBackColor = true;
		this.rdoMethodEx1.AutoSize = true;
		this.rdoMethodEx1.Location = new System.Drawing.Point(16, 214);
		this.rdoMethodEx1.Name = "rdoMethodEx1";
		this.rdoMethodEx1.Size = new System.Drawing.Size(119, 16);
		this.rdoMethodEx1.TabIndex = 3;
		this.rdoMethodEx1.Text = "外部算法(未加载)";
		this.rdoMethodEx1.UseVisualStyleBackColor = true;
		this.rdoMethodEx1.CheckedChanged += new System.EventHandler(rdoMethodEx1_CheckedChanged);
		this.rdoMethod4.AutoSize = true;
		this.rdoMethod4.Location = new System.Drawing.Point(16, 169);
		this.rdoMethod4.Name = "rdoMethod4";
		this.rdoMethod4.Size = new System.Drawing.Size(119, 16);
		this.rdoMethod4.TabIndex = 2;
		this.rdoMethod4.Text = "中心法则(搜全图)";
		this.rdoMethod4.UseVisualStyleBackColor = true;
		this.rdoMethod4.CheckedChanged += new System.EventHandler(rdoMethod4_CheckedChanged);
		this.rdoMethod2.AutoSize = true;
		this.rdoMethod2.Location = new System.Drawing.Point(16, 79);
		this.rdoMethod2.Name = "rdoMethod2";
		this.rdoMethod2.Size = new System.Drawing.Size(119, 16);
		this.rdoMethod2.TabIndex = 1;
		this.rdoMethod2.Text = "左手法则(搜全图)";
		this.rdoMethod2.UseVisualStyleBackColor = true;
		this.rdoMethod1.AutoSize = true;
		this.rdoMethod1.Location = new System.Drawing.Point(16, 34);
		this.rdoMethod1.Name = "rdoMethod1";
		this.rdoMethod1.Size = new System.Drawing.Size(119, 16);
		this.rdoMethod1.TabIndex = 0;
		this.rdoMethod1.Text = "右手法则(搜全图)";
		this.rdoMethod1.UseVisualStyleBackColor = true;
		this.rdoMethod1.CheckedChanged += new System.EventHandler(rdoMethod1_CheckedChanged);
		this.btnOffline.Location = new System.Drawing.Point(1124, 632);
		this.btnOffline.Name = "btnOffline";
		this.btnOffline.Size = new System.Drawing.Size(89, 23);
		this.btnOffline.TabIndex = 3;
		this.btnOffline.TabStop = false;
		this.btnOffline.Text = "离线仿真";
		this.btnOffline.UseVisualStyleBackColor = true;
		this.btnOffline.Click += new System.EventHandler(btnOffline_Click);
		this.btnSaveMap.Location = new System.Drawing.Point(1009, 632);
		this.btnSaveMap.Name = "btnSaveMap";
		this.btnSaveMap.Size = new System.Drawing.Size(89, 23);
		this.btnSaveMap.TabIndex = 2;
		this.btnSaveMap.TabStop = false;
		this.btnSaveMap.Text = "保存迷宫";
		this.btnSaveMap.UseVisualStyleBackColor = true;
		this.btnSaveMap.Click += new System.EventHandler(btnSaveMap_Click);
		this.btnLoadMap.Location = new System.Drawing.Point(1009, 596);
		this.btnLoadMap.Name = "btnLoadMap";
		this.btnLoadMap.Size = new System.Drawing.Size(89, 23);
		this.btnLoadMap.TabIndex = 1;
		this.btnLoadMap.TabStop = false;
		this.btnLoadMap.Text = "载入迷宫";
		this.btnLoadMap.UseVisualStyleBackColor = true;
		this.btnLoadMap.Click += new System.EventHandler(btnLoadMap_Click);
		this.grpTimeSys.Controls.Add(this.txtBestScore);
		this.grpTimeSys.Controls.Add(this.txtThisScore);
		this.grpTimeSys.Controls.Add(this.txtMazeTime);
		this.grpTimeSys.Controls.Add(this.label9);
		this.grpTimeSys.Controls.Add(this.label10);
		this.grpTimeSys.Controls.Add(this.label11);
		this.grpTimeSys.Controls.Add(this.label12);
		this.grpTimeSys.Controls.Add(this.txtRunTime);
		this.grpTimeSys.Location = new System.Drawing.Point(977, 200);
		this.grpTimeSys.Name = "grpTimeSys";
		this.grpTimeSys.Size = new System.Drawing.Size(268, 191);
		this.grpTimeSys.TabIndex = 0;
		this.grpTimeSys.TabStop = false;
		this.grpTimeSys.Text = "计分系统";
		this.grpTimeSys.Enter += new System.EventHandler(groupBox3_Enter);
		this.txtBestScore.Location = new System.Drawing.Point(113, 150);
		this.txtBestScore.Name = "txtBestScore";
		this.txtBestScore.ReadOnly = true;
		this.txtBestScore.Size = new System.Drawing.Size(117, 21);
		this.txtBestScore.TabIndex = 27;
		this.txtBestScore.TabStop = false;
		this.txtBestScore.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.txtThisScore.Location = new System.Drawing.Point(113, 110);
		this.txtThisScore.Name = "txtThisScore";
		this.txtThisScore.ReadOnly = true;
		this.txtThisScore.Size = new System.Drawing.Size(117, 21);
		this.txtThisScore.TabIndex = 26;
		this.txtThisScore.TabStop = false;
		this.txtThisScore.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.txtMazeTime.Location = new System.Drawing.Point(113, 70);
		this.txtMazeTime.Name = "txtMazeTime";
		this.txtMazeTime.ReadOnly = true;
		this.txtMazeTime.Size = new System.Drawing.Size(117, 21);
		this.txtMazeTime.TabIndex = 25;
		this.txtMazeTime.TabStop = false;
		this.txtMazeTime.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.label9.AutoSize = true;
		this.label9.Location = new System.Drawing.Point(44, 114);
		this.label9.Name = "label9";
		this.label9.Size = new System.Drawing.Size(53, 12);
		this.label9.TabIndex = 21;
		this.label9.Text = "本程成绩";
		this.label10.AutoSize = true;
		this.label10.Location = new System.Drawing.Point(44, 74);
		this.label10.Name = "label10";
		this.label10.Size = new System.Drawing.Size(53, 12);
		this.label10.TabIndex = 23;
		this.label10.Text = "迷宫时间";
		this.label11.AutoSize = true;
		this.label11.Location = new System.Drawing.Point(44, 154);
		this.label11.Name = "label11";
		this.label11.Size = new System.Drawing.Size(53, 12);
		this.label11.TabIndex = 22;
		this.label11.Text = "最好成绩";
		this.label12.AutoSize = true;
		this.label12.Location = new System.Drawing.Point(44, 34);
		this.label12.Name = "label12";
		this.label12.Size = new System.Drawing.Size(53, 12);
		this.label12.TabIndex = 0;
		this.label12.Text = "运行时间";
		this.txtRunTime.Location = new System.Drawing.Point(113, 30);
		this.txtRunTime.Name = "txtRunTime";
		this.txtRunTime.ReadOnly = true;
		this.txtRunTime.Size = new System.Drawing.Size(117, 21);
		this.txtRunTime.TabIndex = 24;
		this.txtRunTime.TabStop = false;
		this.txtRunTime.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.openFileDialogUserMap.FileName = "openFileDialog1";
		this.openFileDialogUserMap.Title = "打开迷宫数据";
		this.openFileDialogUserMap.FileOk += new System.ComponentModel.CancelEventHandler(openFileDialogUserMap_FileOk);
		this.timer1.Tick += new System.EventHandler(timer1_Tick);
		this.openFileDialogDll.FileName = "openFileDialog1";
		this.openFileDialogDll.FileOk += new System.ComponentModel.CancelEventHandler(openFileDialogDll_FileOk);
		this.saveFileDialogMap.Title = "保存迷宫数据";
		this.saveFileDialogMap.FileOk += new System.ComponentModel.CancelEventHandler(saveFileDialogMap_FileOk);
		this.checkMapWorker.WorkerReportsProgress = true;
		this.checkMapWorker.WorkerSupportsCancellation = true;
		this.checkMapWorker.DoWork += new System.ComponentModel.DoWorkEventHandler(checkMapWorker_DoWork);
		this.checkMapWorker.ProgressChanged += new System.ComponentModel.ProgressChangedEventHandler(checkMapWorker_ProgressChanged);
		this.checkMapWorker.RunWorkerCompleted += new System.ComponentModel.RunWorkerCompletedEventHandler(checkMapWorker_RunWorkerCompleted);
		this.grpUserInfo.Controls.Add(this.btnChangePassword);
		this.grpUserInfo.Controls.Add(this.btnReg);
		this.grpUserInfo.Controls.Add(this.btnAbout);
		this.grpUserInfo.Controls.Add(this.lblHello);
		this.grpUserInfo.Controls.Add(this.btnUpdateScore);
		this.grpUserInfo.Controls.Add(this.btnGetRanking);
		this.grpUserInfo.Controls.Add(this.btnLogin);
		this.grpUserInfo.Location = new System.Drawing.Point(977, 12);
		this.grpUserInfo.Name = "grpUserInfo";
		this.grpUserInfo.Size = new System.Drawing.Size(268, 182);
		this.grpUserInfo.TabIndex = 0;
		this.grpUserInfo.TabStop = false;
		this.grpUserInfo.Text = "用户信息";
		this.btnChangePassword.Location = new System.Drawing.Point(147, 143);
		this.btnChangePassword.Name = "btnChangePassword";
		this.btnChangePassword.Size = new System.Drawing.Size(89, 23);
		this.btnChangePassword.TabIndex = 3;
		this.btnChangePassword.TabStop = false;
		this.btnChangePassword.Text = "修改密码";
		this.btnChangePassword.UseVisualStyleBackColor = true;
		this.btnChangePassword.Click += new System.EventHandler(btnChangePassword_Click);
		this.btnReg.Location = new System.Drawing.Point(147, 105);
		this.btnReg.Name = "btnReg";
		this.btnReg.Size = new System.Drawing.Size(89, 23);
		this.btnReg.TabIndex = 2;
		this.btnReg.TabStop = false;
		this.btnReg.Text = "用户注册";
		this.btnReg.UseVisualStyleBackColor = true;
		this.btnReg.Click += new System.EventHandler(btnReg_Click);
		this.lblHello.AutoSize = true;
		this.lblHello.Location = new System.Drawing.Point(9, 31);
		this.lblHello.MaximumSize = new System.Drawing.Size(250, 12);
		this.lblHello.MinimumSize = new System.Drawing.Size(250, 12);
		this.lblHello.Name = "lblHello";
		this.lblHello.Size = new System.Drawing.Size(250, 12);
		this.lblHello.TabIndex = 25;
		this.lblHello.Text = "亲爱的 18120194 ，欢迎使用";
		this.lblHello.TextAlign = System.Drawing.ContentAlignment.MiddleCenter;
		this.btnUpdateScore.Location = new System.Drawing.Point(32, 67);
		this.btnUpdateScore.Name = "btnUpdateScore";
		this.btnUpdateScore.Size = new System.Drawing.Size(89, 23);
		this.btnUpdateScore.TabIndex = 4;
		this.btnUpdateScore.TabStop = false;
		this.btnUpdateScore.Text = "提交算法";
		this.btnUpdateScore.UseVisualStyleBackColor = true;
		this.btnUpdateScore.Click += new System.EventHandler(btnUpdateScore_Click);
		this.btnGetRanking.Location = new System.Drawing.Point(32, 105);
		this.btnGetRanking.Name = "btnGetRanking";
		this.btnGetRanking.Size = new System.Drawing.Size(89, 23);
		this.btnGetRanking.TabIndex = 5;
		this.btnGetRanking.TabStop = false;
		this.btnGetRanking.Text = "算法排行";
		this.btnGetRanking.UseVisualStyleBackColor = true;
		this.btnGetRanking.Click += new System.EventHandler(btnGetRanking_Click);
		this.btnLogin.Location = new System.Drawing.Point(147, 67);
		this.btnLogin.Name = "btnLogin";
		this.btnLogin.Size = new System.Drawing.Size(89, 23);
		this.btnLogin.TabIndex = 0;
		this.btnLogin.TabStop = false;
		this.btnLogin.Text = "用户登录";
		this.btnLogin.UseVisualStyleBackColor = true;
		this.btnLogin.Click += new System.EventHandler(btnLogin_Click);
		this.btnQuit.Location = new System.Drawing.Point(1124, 560);
		this.btnQuit.Name = "btnQuit";
		this.btnQuit.Size = new System.Drawing.Size(89, 23);
		this.btnQuit.TabIndex = 32;
		this.btnQuit.TabStop = false;
		this.btnQuit.Text = "退出仿真";
		this.btnQuit.UseVisualStyleBackColor = true;
		this.btnQuit.Click += new System.EventHandler(btnQuit_Click);
		this.statusStrip1.Items.AddRange(new System.Windows.Forms.ToolStripItem[11]
		{
			this.toolStripStatusLabel1, this.lblLogin_s, this.toolStripStatusLabel3, this.lblCheck_s, this.toolStripStatusLabel5, this.lblSim_s, this.toolStripStatusLabel7, this.lblRun_s, this.toolStripStatusLabel9, this.lblFinish_s,
			this.toolStripStatusLabel11
		});
		this.statusStrip1.Location = new System.Drawing.Point(0, 710);
		this.statusStrip1.Name = "statusStrip1";
		this.statusStrip1.RenderMode = System.Windows.Forms.ToolStripRenderMode.Professional;
		this.statusStrip1.Size = new System.Drawing.Size(1257, 22);
		this.statusStrip1.SizingGrip = false;
		this.statusStrip1.TabIndex = 33;
		this.statusStrip1.Text = "statusStrip1";
		this.toolStripStatusLabel1.Name = "toolStripStatusLabel1";
		this.toolStripStatusLabel1.Size = new System.Drawing.Size(84, 17);
		this.toolStripStatusLabel1.Text = "                   ";
		this.lblLogin_s.Font = new System.Drawing.Font("微软雅黑", 9f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.lblLogin_s.ForeColor = System.Drawing.Color.ForestGreen;
		this.lblLogin_s.Name = "lblLogin_s";
		this.lblLogin_s.Size = new System.Drawing.Size(56, 17);
		this.lblLogin_s.Text = "用户登录";
		this.toolStripStatusLabel3.Font = new System.Drawing.Font("微软雅黑", 9f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.toolStripStatusLabel3.ForeColor = System.Drawing.Color.Black;
		this.toolStripStatusLabel3.Name = "toolStripStatusLabel3";
		this.toolStripStatusLabel3.Size = new System.Drawing.Size(48, 17);
		this.toolStripStatusLabel3.Text = "  --->  ";
		this.lblCheck_s.Font = new System.Drawing.Font("微软雅黑", 9f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.lblCheck_s.ForeColor = System.Drawing.Color.DarkRed;
		this.lblCheck_s.Name = "lblCheck_s";
		this.lblCheck_s.Size = new System.Drawing.Size(56, 17);
		this.lblCheck_s.Text = "迷宫验证";
		this.toolStripStatusLabel5.Font = new System.Drawing.Font("微软雅黑", 9f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.toolStripStatusLabel5.ForeColor = System.Drawing.Color.Black;
		this.toolStripStatusLabel5.Name = "toolStripStatusLabel5";
		this.toolStripStatusLabel5.Size = new System.Drawing.Size(48, 17);
		this.toolStripStatusLabel5.Text = "  --->  ";
		this.lblSim_s.Font = new System.Drawing.Font("微软雅黑", 9f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.lblSim_s.ForeColor = System.Drawing.Color.DarkRed;
		this.lblSim_s.Name = "lblSim_s";
		this.lblSim_s.Size = new System.Drawing.Size(117, 17);
		this.lblSim_s.Text = "离线仿真 / 实时调试";
		this.lblSim_s.Click += new System.EventHandler(toolStripStatusLabel6_Click);
		this.toolStripStatusLabel7.Font = new System.Drawing.Font("微软雅黑", 9f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.toolStripStatusLabel7.ForeColor = System.Drawing.Color.Black;
		this.toolStripStatusLabel7.Name = "toolStripStatusLabel7";
		this.toolStripStatusLabel7.Size = new System.Drawing.Size(48, 17);
		this.toolStripStatusLabel7.Text = "  --->  ";
		this.lblRun_s.Font = new System.Drawing.Font("微软雅黑", 9f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.lblRun_s.ForeColor = System.Drawing.Color.DarkRed;
		this.lblRun_s.Name = "lblRun_s";
		this.lblRun_s.Size = new System.Drawing.Size(56, 17);
		this.lblRun_s.Text = "正在运行";
		this.toolStripStatusLabel9.Font = new System.Drawing.Font("微软雅黑", 9f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.toolStripStatusLabel9.ForeColor = System.Drawing.Color.Black;
		this.toolStripStatusLabel9.Name = "toolStripStatusLabel9";
		this.toolStripStatusLabel9.Size = new System.Drawing.Size(48, 17);
		this.toolStripStatusLabel9.Text = "  --->  ";
		this.lblFinish_s.Font = new System.Drawing.Font("微软雅黑", 9f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.lblFinish_s.ForeColor = System.Drawing.Color.DarkRed;
		this.lblFinish_s.Name = "lblFinish_s";
		this.lblFinish_s.Size = new System.Drawing.Size(56, 17);
		this.lblFinish_s.Text = "运行完成";
		this.toolStripStatusLabel11.Name = "toolStripStatusLabel11";
		this.toolStripStatusLabel11.Size = new System.Drawing.Size(76, 17);
		this.toolStripStatusLabel11.Text = "                 ";
		this.btnRunSim.Location = new System.Drawing.Point(1009, 560);
		this.btnRunSim.Name = "btnRunSim";
		this.btnRunSim.Size = new System.Drawing.Size(89, 23);
		this.btnRunSim.TabIndex = 8;
		this.btnRunSim.TabStop = false;
		this.btnRunSim.Text = "开始运行";
		this.btnRunSim.UseVisualStyleBackColor = true;
		this.btnRunSim.Click += new System.EventHandler(btnRunSim_Click);
		this.loginWorker.WorkerReportsProgress = true;
		this.loginWorker.WorkerSupportsCancellation = true;
		this.loginWorker.DoWork += new System.ComponentModel.DoWorkEventHandler(loginWorker_DoWork);
		this.loginWorker.ProgressChanged += new System.ComponentModel.ProgressChangedEventHandler(loginWorker_ProgressChanged);
		this.loginWorker.RunWorkerCompleted += new System.ComponentModel.RunWorkerCompletedEventHandler(loginWorker_RunWorkerCompleted);
		this.regWorker.WorkerReportsProgress = true;
		this.regWorker.WorkerSupportsCancellation = true;
		this.regWorker.DoWork += new System.ComponentModel.DoWorkEventHandler(regWorker_DoWork);
		this.regWorker.ProgressChanged += new System.ComponentModel.ProgressChangedEventHandler(regWorker_ProgressChanged);
		this.regWorker.RunWorkerCompleted += new System.ComponentModel.RunWorkerCompletedEventHandler(regWorker_RunWorkerCompleted);
		this.changePasswordWorker.WorkerReportsProgress = true;
		this.changePasswordWorker.WorkerSupportsCancellation = true;
		this.changePasswordWorker.DoWork += new System.ComponentModel.DoWorkEventHandler(changePasswordWorker_DoWork);
		this.changePasswordWorker.ProgressChanged += new System.ComponentModel.ProgressChangedEventHandler(changePasswordWorker_ProgressChanged);
		this.changePasswordWorker.RunWorkerCompleted += new System.ComponentModel.RunWorkerCompletedEventHandler(changePasswordWorker_RunWorkerCompleted);
		this.updateScoreWorker.WorkerReportsProgress = true;
		this.updateScoreWorker.WorkerSupportsCancellation = true;
		this.updateScoreWorker.DoWork += new System.ComponentModel.DoWorkEventHandler(updateScoreWorker_DoWork);
		this.updateScoreWorker.ProgressChanged += new System.ComponentModel.ProgressChangedEventHandler(updateScoreWorker_ProgressChanged);
		this.updateScoreWorker.RunWorkerCompleted += new System.ComponentModel.RunWorkerCompletedEventHandler(updateScoreWorker_RunWorkerCompleted);
		this.getRankingWorker.WorkerReportsProgress = true;
		this.getRankingWorker.WorkerSupportsCancellation = true;
		this.getRankingWorker.DoWork += new System.ComponentModel.DoWorkEventHandler(getRankingWorker_DoWork);
		this.getRankingWorker.ProgressChanged += new System.ComponentModel.ProgressChangedEventHandler(getRankingWorker_ProgressChanged);
		this.getRankingWorker.RunWorkerCompleted += new System.ComponentModel.RunWorkerCompletedEventHandler(getRankingWorker_RunWorkerCompleted);
		this.openFileSendDll.FileName = "openFileDialog1";
		this.openFileSendDll.FileOk += new System.ComponentModel.CancelEventHandler(openFileSendDll_FileOk);
		this.grpMouseData.Controls.Add(this.txtAction);
		this.grpMouseData.Controls.Add(this.txtStatus);
		this.grpMouseData.Controls.Add(this.txtPosition_mouse);
		this.grpMouseData.Controls.Add(this.txtUserMsg);
		this.grpMouseData.Controls.Add(this.label18);
		this.grpMouseData.Controls.Add(this.label19);
		this.grpMouseData.Controls.Add(this.label20);
		this.grpMouseData.Controls.Add(this.label21);
		this.grpMouseData.Location = new System.Drawing.Point(703, 12);
		this.grpMouseData.Name = "grpMouseData";
		this.grpMouseData.Size = new System.Drawing.Size(268, 182);
		this.grpMouseData.TabIndex = 32;
		this.grpMouseData.TabStop = false;
		this.grpMouseData.Text = "内部数据";
		this.txtAction.Location = new System.Drawing.Point(111, 104);
		this.txtAction.Name = "txtAction";
		this.txtAction.ReadOnly = true;
		this.txtAction.Size = new System.Drawing.Size(117, 21);
		this.txtAction.TabIndex = 17;
		this.txtAction.TabStop = false;
		this.txtAction.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.txtStatus.Location = new System.Drawing.Point(111, 28);
		this.txtStatus.Name = "txtStatus";
		this.txtStatus.ReadOnly = true;
		this.txtStatus.Size = new System.Drawing.Size(117, 21);
		this.txtStatus.TabIndex = 16;
		this.txtStatus.TabStop = false;
		this.txtStatus.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.txtPosition_mouse.Location = new System.Drawing.Point(111, 66);
		this.txtPosition_mouse.Name = "txtPosition_mouse";
		this.txtPosition_mouse.ReadOnly = true;
		this.txtPosition_mouse.Size = new System.Drawing.Size(117, 21);
		this.txtPosition_mouse.TabIndex = 18;
		this.txtPosition_mouse.TabStop = false;
		this.txtPosition_mouse.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.txtUserMsg.Location = new System.Drawing.Point(111, 142);
		this.txtUserMsg.Name = "txtUserMsg";
		this.txtUserMsg.ReadOnly = true;
		this.txtUserMsg.Size = new System.Drawing.Size(117, 21);
		this.txtUserMsg.TabIndex = 31;
		this.txtUserMsg.TabStop = false;
		this.txtUserMsg.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.label18.AutoSize = true;
		this.label18.Location = new System.Drawing.Point(44, 32);
		this.label18.Name = "label18";
		this.label18.Size = new System.Drawing.Size(53, 12);
		this.label18.TabIndex = 21;
		this.label18.Text = "运行状态";
		this.label19.AutoSize = true;
		this.label19.Location = new System.Drawing.Point(44, 70);
		this.label19.Name = "label19";
		this.label19.Size = new System.Drawing.Size(53, 12);
		this.label19.TabIndex = 26;
		this.label19.Text = "当前坐标";
		this.label20.AutoSize = true;
		this.label20.Location = new System.Drawing.Point(44, 108);
		this.label20.Name = "label20";
		this.label20.Size = new System.Drawing.Size(53, 12);
		this.label20.TabIndex = 27;
		this.label20.Text = "执行动作";
		this.label21.AutoSize = true;
		this.label21.Location = new System.Drawing.Point(44, 146);
		this.label21.Name = "label21";
		this.label21.Size = new System.Drawing.Size(53, 12);
		this.label21.TabIndex = 28;
		this.label21.Text = "用户输出";
		this.grpRealDisplay.Controls.Add(this.chkCrossway_real);
		this.grpRealDisplay.Controls.Add(this.chkSearched_real);
		this.grpRealDisplay.Controls.Add(this.chkFlood_real);
		this.grpRealDisplay.Location = new System.Drawing.Point(703, 551);
		this.grpRealDisplay.Name = "grpRealDisplay";
		this.grpRealDisplay.Size = new System.Drawing.Size(115, 149);
		this.grpRealDisplay.TabIndex = 37;
		this.grpRealDisplay.TabStop = false;
		this.grpRealDisplay.Text = "真实数据显示";
		this.chkCrossway_real.AutoSize = true;
		this.chkCrossway_real.Location = new System.Drawing.Point(17, 71);
		this.chkCrossway_real.Name = "chkCrossway_real";
		this.chkCrossway_real.Size = new System.Drawing.Size(84, 16);
		this.chkCrossway_real.TabIndex = 5;
		this.chkCrossway_real.TabStop = false;
		this.chkCrossway_real.Text = "岔路口信息";
		this.chkCrossway_real.UseVisualStyleBackColor = true;
		this.chkCrossway_real.CheckedChanged += new System.EventHandler(chkCrossway_real_CheckedChanged);
		this.chkSearched_real.AutoSize = true;
		this.chkSearched_real.Location = new System.Drawing.Point(17, 33);
		this.chkSearched_real.Name = "chkSearched_real";
		this.chkSearched_real.Size = new System.Drawing.Size(84, 16);
		this.chkSearched_real.TabIndex = 1;
		this.chkSearched_real.TabStop = false;
		this.chkSearched_real.Text = "已搜索区域";
		this.chkSearched_real.UseVisualStyleBackColor = true;
		this.chkSearched_real.CheckedChanged += new System.EventHandler(chkSearched_real_CheckedChanged);
		this.chkFlood_real.AutoSize = true;
		this.chkFlood_real.Location = new System.Drawing.Point(17, 109);
		this.chkFlood_real.Name = "chkFlood_real";
		this.chkFlood_real.Size = new System.Drawing.Size(84, 16);
		this.chkFlood_real.TabIndex = 4;
		this.chkFlood_real.TabStop = false;
		this.chkFlood_real.Text = "坐标洪水值";
		this.chkFlood_real.UseVisualStyleBackColor = true;
		this.chkFlood_real.CheckedChanged += new System.EventHandler(chkFlood_real_CheckedChanged);
		base.AutoScaleDimensions = new System.Drawing.SizeF(6f, 12f);
		base.AutoScaleMode = System.Windows.Forms.AutoScaleMode.Font;
		this.AutoScroll = true;
		base.ClientSize = new System.Drawing.Size(1257, 732);
		base.Controls.Add(this.grpRealDisplay);
		base.Controls.Add(this.grpMouseData);
		base.Controls.Add(this.statusStrip1);
		base.Controls.Add(this.btnDebug);
		base.Controls.Add(this.btnReset);
		base.Controls.Add(this.btnRunSim);
		base.Controls.Add(this.btnChectMap);
		base.Controls.Add(this.grpUserInfo);
		base.Controls.Add(this.btnOffline);
		base.Controls.Add(this.grpStartDir);
		base.Controls.Add(this.grpMouseDisplay);
		base.Controls.Add(this.btnSaveMap);
		base.Controls.Add(this.grpStartPoint);
		base.Controls.Add(this.btnLoadMap);
		base.Controls.Add(this.label1);
		base.Controls.Add(this.trbSpeed);
		base.Controls.Add(this.grpMethod);
		base.Controls.Add(this.grpRealData);
		base.Controls.Add(this.grpTimeSys);
		base.Controls.Add(this.picMap);
		base.Controls.Add(this.btnQuit);
		base.FormBorderStyle = System.Windows.Forms.FormBorderStyle.FixedSingle;
		base.MaximizeBox = false;
		base.Name = "FrmSimulationTop";
		this.Text = "电脑鼠策略仿真软件";
		base.Load += new System.EventHandler(frmSimulationTop_Load);
		base.Paint += new System.Windows.Forms.PaintEventHandler(FrmSimulationTop_Paint);
		((System.ComponentModel.ISupportInitialize)this.picMap).EndInit();
		this.grpRealData.ResumeLayout(false);
		this.grpRealData.PerformLayout();
		this.grpMouseDisplay.ResumeLayout(false);
		this.grpMouseDisplay.PerformLayout();
		this.grpStartDir.ResumeLayout(false);
		this.grpStartDir.PerformLayout();
		this.grpStartPoint.ResumeLayout(false);
		this.grpStartPoint.PerformLayout();
		((System.ComponentModel.ISupportInitialize)this.trbSpeed).EndInit();
		this.grpMethod.ResumeLayout(false);
		this.grpMethod.PerformLayout();
		this.grpTimeSys.ResumeLayout(false);
		this.grpTimeSys.PerformLayout();
		this.grpUserInfo.ResumeLayout(false);
		this.grpUserInfo.PerformLayout();
		this.statusStrip1.ResumeLayout(false);
		this.statusStrip1.PerformLayout();
		this.grpMouseData.ResumeLayout(false);
		this.grpMouseData.PerformLayout();
		this.grpRealDisplay.ResumeLayout(false);
		this.grpRealDisplay.PerformLayout();
		base.ResumeLayout(false);
		base.PerformLayout();
	}
}

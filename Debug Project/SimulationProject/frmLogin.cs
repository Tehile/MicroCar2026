using System;
using System.ComponentModel;
using System.Drawing;
using System.IO;
using System.Text;
using System.Windows.Forms;

namespace SimulationProject;

public class frmLogin : Form
{
	private FrmSimulationTop father;

	public string command;

	public string userID;

	public string password;

	private IContainer components;

	private TextBox txtUserID;

	private Label label1;

	private TextBox txtPassword;

	private Label label2;

	private Button btnCancel;

	public Button btnOK;

	public frmLogin(FrmSimulationTop form)
	{
		InitializeComponent();
		father = form;
	}

	public bool checkID(string userID_temp)
	{
		try
		{
			if (userID_temp.Length > 8)
			{
				return false;
			}
			for (int i = 0; i < userID_temp.Length; i++)
			{
				Encoding.UTF8.GetBytes(userID_temp[i].ToString());
				if (userID_temp[i].ToString() == "#")
				{
					return false;
				}
			}
		}
		catch
		{
			return false;
		}
		return true;
	}

	public bool checkPassword(string password_temp)
	{
		try
		{
			if (password_temp.Length != 8)
			{
				return false;
			}
			for (int i = 0; i < password_temp.Length; i++)
			{
				int.Parse(password_temp[i].ToString());
			}
		}
		catch
		{
			return false;
		}
		return true;
	}

	private void frmLogin_Load(object sender, EventArgs e)
	{
	}

	private void btnCancel_Click(object sender, EventArgs e)
	{
		Close();
	}

	private void btnOK_Click(object sender, EventArgs e)
	{
		//if (!checkID(txtUserID.Text))
		//{
		//	MessageBox.Show("账号不合法！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
		//	return;
		//}
		//if (!checkPassword(txtPassword.Text))
		//{
		//	MessageBox.Show("密码不合法，应为8位纯数据！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
		//	return;
		//}
		command = "1";
		userID = txtUserID.Text;
		password = txtPassword.Text;
		object[] arrObjects = new object[3] { command, userID, password };
		//if (father.loginWorker.IsBusy)
		//{
		//	MessageBox.Show("操作过于频繁！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
		//	return;
		//}
		btnOK.Enabled = false;
		father.loginWorker.RunWorkerAsync(arrObjects);
	}

	private void txtUserID_TextChanged(object sender, EventArgs e)
	{
	}

	private void frmLogin_Shown(object sender, EventArgs e)
	{
		string userID_old = "";
		try
		{
			StreamReader streamReader = new StreamReader("info.txt", Encoding.Default);
			userID_old = streamReader.ReadLine();
			streamReader.Close();
		}
		catch
		{
			return;
		}
		if (userID_old != null && checkID(userID_old))
		{
			txtUserID.Text = userID_old;
			txtPassword.Focus();
		}
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
		this.txtUserID = new System.Windows.Forms.TextBox();
		this.label1 = new System.Windows.Forms.Label();
		this.txtPassword = new System.Windows.Forms.TextBox();
		this.label2 = new System.Windows.Forms.Label();
		this.btnOK = new System.Windows.Forms.Button();
		this.btnCancel = new System.Windows.Forms.Button();
		base.SuspendLayout();
		this.txtUserID.Location = new System.Drawing.Point(89, 25);
		this.txtUserID.MaxLength = 8;
		this.txtUserID.Name = "txtUserID";
		this.txtUserID.Size = new System.Drawing.Size(100, 21);
		this.txtUserID.TabIndex = 0;
		this.txtUserID.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.txtUserID.TextChanged += new System.EventHandler(txtUserID_TextChanged);
		this.label1.AutoSize = true;
		this.label1.Location = new System.Drawing.Point(39, 29);
		this.label1.Name = "label1";
		this.label1.Size = new System.Drawing.Size(29, 12);
		this.label1.TabIndex = 28;
		this.label1.Text = "账号";
		this.txtPassword.Location = new System.Drawing.Point(89, 63);
		this.txtPassword.MaxLength = 8;
		this.txtPassword.Name = "txtPassword";
		this.txtPassword.PasswordChar = '*';
		this.txtPassword.Size = new System.Drawing.Size(100, 21);
		this.txtPassword.TabIndex = 1;
		this.txtPassword.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.label2.AutoSize = true;
		this.label2.Location = new System.Drawing.Point(39, 67);
		this.label2.Name = "label2";
		this.label2.Size = new System.Drawing.Size(29, 12);
		this.label2.TabIndex = 30;
		this.label2.Text = "密码";
		this.btnOK.Location = new System.Drawing.Point(41, 110);
		this.btnOK.Name = "btnOK";
		this.btnOK.Size = new System.Drawing.Size(66, 23);
		this.btnOK.TabIndex = 2;
		this.btnOK.Text = "确定";
		this.btnOK.UseVisualStyleBackColor = true;
		this.btnOK.Click += new System.EventHandler(btnOK_Click);
		this.btnCancel.Location = new System.Drawing.Point(123, 110);
		this.btnCancel.Name = "btnCancel";
		this.btnCancel.Size = new System.Drawing.Size(66, 23);
		this.btnCancel.TabIndex = 3;
		this.btnCancel.Text = "取消";
		this.btnCancel.UseVisualStyleBackColor = true;
		this.btnCancel.Click += new System.EventHandler(btnCancel_Click);
		base.AcceptButton = this.btnOK;
		base.AutoScaleDimensions = new System.Drawing.SizeF(6f, 12f);
		base.AutoScaleMode = System.Windows.Forms.AutoScaleMode.Font;
		base.ClientSize = new System.Drawing.Size(231, 157);
		base.Controls.Add(this.btnCancel);
		base.Controls.Add(this.btnOK);
		base.Controls.Add(this.txtPassword);
		base.Controls.Add(this.label2);
		base.Controls.Add(this.txtUserID);
		base.Controls.Add(this.label1);
		base.FormBorderStyle = System.Windows.Forms.FormBorderStyle.FixedSingle;
		base.MaximizeBox = false;
		base.Name = "frmLogin";
		base.StartPosition = System.Windows.Forms.FormStartPosition.CenterParent;
		this.Text = "用户登录";
		base.Load += new System.EventHandler(frmLogin_Load);
		base.Shown += new System.EventHandler(frmLogin_Shown);
		base.ResumeLayout(false);
		base.PerformLayout();
	}
}

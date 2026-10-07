using System;
using System.ComponentModel;
using System.Drawing;
using System.Text;
using System.Windows.Forms;

namespace SimulationProject;

public class frmChangePassword : Form
{
	private FrmSimulationTop father;

	public string command;

	public string userID;

	public string password_old;

	public string password_new;

	private IContainer components;

	private TextBox txtPassword_2;

	private Label label3;

	private Button btnCancel;

	private Button btnOK;

	private TextBox txtPassword_1;

	private Label label2;

	private TextBox txtPasswordOld;

	private Label label1;

	public frmChangePassword(FrmSimulationTop form)
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

	private void frmChangePassword_Load(object sender, EventArgs e)
	{
	}

	private void btnCancel_Click(object sender, EventArgs e)
	{
		Close();
	}

	private void btnOK_Click(object sender, EventArgs e)
	{
		command = "3";
		userID = father.userID;
		password_old = txtPasswordOld.Text;
		string password_1 = txtPassword_1.Text;
		string password_2 = txtPassword_2.Text;
		if (!checkPassword(password_old))
		{
			MessageBox.Show("旧密码不合法，应为8位纯数字！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			return;
		}
		if (password_1 != password_2)
		{
			MessageBox.Show("两次密码输入不同！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			return;
		}
		password_new = password_1;
		if (!checkPassword(password_new))
		{
			MessageBox.Show("新密码不合法，应为8位纯数字！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
			return;
		}
		object[] arrObjects = new object[4] { command, userID, password_old, password_new };
		if (father.changePasswordWorker.IsBusy)
		{
			MessageBox.Show("操作过于频繁！", "错误", MessageBoxButtons.OK, MessageBoxIcon.Hand);
		}
		else
		{
			father.changePasswordWorker.RunWorkerAsync(arrObjects);
		}
	}

	private void frmChangePassword_Shown(object sender, EventArgs e)
	{
		txtPasswordOld.Focus();
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
		this.txtPassword_2 = new System.Windows.Forms.TextBox();
		this.label3 = new System.Windows.Forms.Label();
		this.btnCancel = new System.Windows.Forms.Button();
		this.btnOK = new System.Windows.Forms.Button();
		this.txtPassword_1 = new System.Windows.Forms.TextBox();
		this.label2 = new System.Windows.Forms.Label();
		this.txtPasswordOld = new System.Windows.Forms.TextBox();
		this.label1 = new System.Windows.Forms.Label();
		base.SuspendLayout();
		this.txtPassword_2.Location = new System.Drawing.Point(89, 84);
		this.txtPassword_2.MaxLength = 8;
		this.txtPassword_2.Name = "txtPassword_2";
		this.txtPassword_2.PasswordChar = '*';
		this.txtPassword_2.Size = new System.Drawing.Size(100, 21);
		this.txtPassword_2.TabIndex = 2;
		this.txtPassword_2.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.label3.AutoSize = true;
		this.label3.Location = new System.Drawing.Point(27, 88);
		this.label3.Name = "label3";
		this.label3.Size = new System.Drawing.Size(53, 12);
		this.label3.TabIndex = 48;
		this.label3.Text = "确认密码";
		this.btnCancel.Location = new System.Drawing.Point(123, 123);
		this.btnCancel.Name = "btnCancel";
		this.btnCancel.Size = new System.Drawing.Size(66, 23);
		this.btnCancel.TabIndex = 4;
		this.btnCancel.Text = "取消";
		this.btnCancel.UseVisualStyleBackColor = true;
		this.btnCancel.Click += new System.EventHandler(btnCancel_Click);
		this.btnOK.Location = new System.Drawing.Point(41, 123);
		this.btnOK.Name = "btnOK";
		this.btnOK.Size = new System.Drawing.Size(66, 23);
		this.btnOK.TabIndex = 3;
		this.btnOK.Text = "确定";
		this.btnOK.UseVisualStyleBackColor = true;
		this.btnOK.Click += new System.EventHandler(btnOK_Click);
		this.txtPassword_1.Location = new System.Drawing.Point(89, 50);
		this.txtPassword_1.MaxLength = 8;
		this.txtPassword_1.Name = "txtPassword_1";
		this.txtPassword_1.PasswordChar = '*';
		this.txtPassword_1.Size = new System.Drawing.Size(100, 21);
		this.txtPassword_1.TabIndex = 1;
		this.txtPassword_1.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.label2.AutoSize = true;
		this.label2.Location = new System.Drawing.Point(33, 54);
		this.label2.Name = "label2";
		this.label2.Size = new System.Drawing.Size(41, 12);
		this.label2.TabIndex = 44;
		this.label2.Text = "新密码";
		this.txtPasswordOld.Location = new System.Drawing.Point(89, 16);
		this.txtPasswordOld.MaxLength = 8;
		this.txtPasswordOld.Name = "txtPasswordOld";
		this.txtPasswordOld.PasswordChar = '*';
		this.txtPasswordOld.Size = new System.Drawing.Size(100, 21);
		this.txtPasswordOld.TabIndex = 0;
		this.txtPasswordOld.TextAlign = System.Windows.Forms.HorizontalAlignment.Center;
		this.label1.AutoSize = true;
		this.label1.Location = new System.Drawing.Point(33, 20);
		this.label1.Name = "label1";
		this.label1.Size = new System.Drawing.Size(41, 12);
		this.label1.TabIndex = 42;
		this.label1.Text = "原密码";
		base.AcceptButton = this.btnOK;
		base.AutoScaleDimensions = new System.Drawing.SizeF(6f, 12f);
		base.AutoScaleMode = System.Windows.Forms.AutoScaleMode.Font;
		base.ClientSize = new System.Drawing.Size(231, 157);
		base.Controls.Add(this.txtPassword_2);
		base.Controls.Add(this.label3);
		base.Controls.Add(this.btnCancel);
		base.Controls.Add(this.btnOK);
		base.Controls.Add(this.txtPassword_1);
		base.Controls.Add(this.label2);
		base.Controls.Add(this.txtPasswordOld);
		base.Controls.Add(this.label1);
		base.FormBorderStyle = System.Windows.Forms.FormBorderStyle.FixedSingle;
		base.Name = "frmChangePassword";
		base.StartPosition = System.Windows.Forms.FormStartPosition.CenterParent;
		this.Text = "修改密码";
		base.Load += new System.EventHandler(frmChangePassword_Load);
		base.Shown += new System.EventHandler(frmChangePassword_Shown);
		base.ResumeLayout(false);
		base.PerformLayout();
	}
}

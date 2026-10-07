using System;
using System.ComponentModel;
using System.Drawing;
using System.Windows.Forms;
using Microsoft.Win32;

namespace 注册机;

public class FormRegister : Form
{
	public static bool state = true;

	private SoftReg softReg = new SoftReg();

	private IContainer components;

	private Label label1;

	private TextBox txtHardware;

	private TextBox txtLicence;

	private Label label2;

	private Button btnReg;

	private Button btnClose;

	public FormRegister()
	{
		InitializeComponent();
	}

	private void FormRegister_Load(object sender, EventArgs e)
	{
		base.StartPosition = FormStartPosition.CenterScreen;
		txtHardware.Text = softReg.GetMNum();
	}

	private void btnReg_Click(object sender, EventArgs e)
	{
		try
		{
			if (txtLicence.Text == softReg.GetRNum())
			{
				MessageBox.Show("注册成功！重启软件后生效！", "信息", MessageBoxButtons.OK, MessageBoxIcon.Asterisk);
				Registry.CurrentUser.OpenSubKey("Software", writable: true).CreateSubKey("mySoftWare").CreateSubKey("Register.INI")
					.CreateSubKey(txtLicence.Text)
					.SetValue("UserName", "Rsoft");
				Close();
			}
			else
			{
				MessageBox.Show("注册码错误！", "警告", MessageBoxButtons.OK, MessageBoxIcon.Exclamation);
				txtLicence.SelectAll();
			}
		}
		catch (Exception ex)
		{
			throw new Exception(ex.Message);
		}
	}

	private void btnClose_Click(object sender, EventArgs e)
	{
		if (state)
		{
			Close();
		}
		else
		{
			Application.Exit();
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
		this.label1 = new System.Windows.Forms.Label();
		this.txtHardware = new System.Windows.Forms.TextBox();
		this.txtLicence = new System.Windows.Forms.TextBox();
		this.label2 = new System.Windows.Forms.Label();
		this.btnReg = new System.Windows.Forms.Button();
		this.btnClose = new System.Windows.Forms.Button();
		base.SuspendLayout();
		this.label1.AutoSize = true;
		this.label1.Location = new System.Drawing.Point(16, 20);
		this.label1.Name = "label1";
		this.label1.Size = new System.Drawing.Size(53, 12);
		this.label1.TabIndex = 0;
		this.label1.Text = "机器码：";
		this.txtHardware.Location = new System.Drawing.Point(75, 17);
		this.txtHardware.Name = "txtHardware";
		this.txtHardware.Size = new System.Drawing.Size(194, 21);
		this.txtHardware.TabIndex = 1;
		this.txtLicence.Location = new System.Drawing.Point(75, 61);
		this.txtLicence.Name = "txtLicence";
		this.txtLicence.Size = new System.Drawing.Size(194, 21);
		this.txtLicence.TabIndex = 3;
		this.label2.AutoSize = true;
		this.label2.Location = new System.Drawing.Point(16, 64);
		this.label2.Name = "label2";
		this.label2.Size = new System.Drawing.Size(53, 12);
		this.label2.TabIndex = 2;
		this.label2.Text = "注册码：";
		this.btnReg.Location = new System.Drawing.Point(18, 112);
		this.btnReg.Name = "btnReg";
		this.btnReg.Size = new System.Drawing.Size(75, 23);
		this.btnReg.TabIndex = 4;
		this.btnReg.Text = "注册";
		this.btnReg.UseVisualStyleBackColor = true;
		this.btnReg.Click += new System.EventHandler(btnReg_Click);
		this.btnClose.Location = new System.Drawing.Point(194, 112);
		this.btnClose.Name = "btnClose";
		this.btnClose.Size = new System.Drawing.Size(75, 23);
		this.btnClose.TabIndex = 5;
		this.btnClose.Text = "关闭";
		this.btnClose.UseVisualStyleBackColor = true;
		this.btnClose.Click += new System.EventHandler(btnClose_Click);
		base.AutoScaleDimensions = new System.Drawing.SizeF(6f, 12f);
		base.AutoScaleMode = System.Windows.Forms.AutoScaleMode.Font;
		base.ClientSize = new System.Drawing.Size(284, 146);
		base.Controls.Add(this.btnClose);
		base.Controls.Add(this.btnReg);
		base.Controls.Add(this.txtLicence);
		base.Controls.Add(this.label2);
		base.Controls.Add(this.txtHardware);
		base.Controls.Add(this.label1);
		base.FormBorderStyle = System.Windows.Forms.FormBorderStyle.Fixed3D;
		base.MaximizeBox = false;
		base.Name = "FormRegister";
		base.StartPosition = System.Windows.Forms.FormStartPosition.CenterParent;
		this.Text = "软件注册";
		base.Load += new System.EventHandler(FormRegister_Load);
		base.ResumeLayout(false);
		base.PerformLayout();
	}
}

using System;
using System.ComponentModel;
using System.Drawing;
using System.Windows.Forms;

namespace SimulationProject;

public class Form3 : Form
{
	private IContainer components;

	private Label label1;

	private Label label2;

	private Label label3;

	private PictureBox pictureBox1;

	private PictureBox pictureBox2;

	private Label label4;

	private Label label5;

	public Form3()
	{
		InitializeComponent();
	}

	private void Form3_Load(object sender, EventArgs e)
	{
		base.StartPosition = FormStartPosition.CenterScreen;
	}

	private void timer1_Tick(object sender, EventArgs e)
	{
	}

	private void label2_Click(object sender, EventArgs e)
	{
	}

	private void label1_Click(object sender, EventArgs e)
	{
	}

	private void label3_Click(object sender, EventArgs e)
	{
	}

	private void button1_Click(object sender, EventArgs e)
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
		System.ComponentModel.ComponentResourceManager resources = new System.ComponentModel.ComponentResourceManager(typeof(SimulationProject.Form3));
		this.label1 = new System.Windows.Forms.Label();
		this.label2 = new System.Windows.Forms.Label();
		this.label3 = new System.Windows.Forms.Label();
		this.pictureBox1 = new System.Windows.Forms.PictureBox();
		this.pictureBox2 = new System.Windows.Forms.PictureBox();
		this.label4 = new System.Windows.Forms.Label();
		this.label5 = new System.Windows.Forms.Label();
		((System.ComponentModel.ISupportInitialize)this.pictureBox1).BeginInit();
		((System.ComponentModel.ISupportInitialize)this.pictureBox2).BeginInit();
		base.SuspendLayout();
		this.label1.AutoSize = true;
		this.label1.Font = new System.Drawing.Font("宋体", 10.5f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.label1.Location = new System.Drawing.Point(327, 114);
		this.label1.Name = "label1";
		this.label1.Size = new System.Drawing.Size(196, 14);
		this.label1.TabIndex = 0;
		this.label1.Text = "活动地点：学生活动中心704室";
		this.label1.Click += new System.EventHandler(label1_Click);
		this.label2.AutoSize = true;
		this.label2.Font = new System.Drawing.Font("宋体", 18f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.label2.Location = new System.Drawing.Point(348, 12);
		this.label2.Name = "label2";
		this.label2.Size = new System.Drawing.Size(154, 24);
		this.label2.TabIndex = 1;
		this.label2.Text = "北京交通大学";
		this.label2.Click += new System.EventHandler(label2_Click);
		this.label3.AutoSize = true;
		this.label3.Font = new System.Drawing.Font("宋体", 10.5f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.label3.Location = new System.Drawing.Point(348, 90);
		this.label3.Name = "label3";
		this.label3.Size = new System.Drawing.Size(154, 14);
		this.label3.TabIndex = 2;
		this.label3.Text = "联系电话：18510859046";
		this.label3.Click += new System.EventHandler(label3_Click);
		this.pictureBox1.Image = (System.Drawing.Image)resources.GetObject("pictureBox1.Image");
		this.pictureBox1.Location = new System.Drawing.Point(30, 17);
		this.pictureBox1.Name = "pictureBox1";
		this.pictureBox1.Size = new System.Drawing.Size(183, 140);
		this.pictureBox1.SizeMode = System.Windows.Forms.PictureBoxSizeMode.StretchImage;
		this.pictureBox1.TabIndex = 4;
		this.pictureBox1.TabStop = false;
		this.pictureBox2.BackColor = System.Drawing.SystemColors.ControlLightLight;
		this.pictureBox2.Location = new System.Drawing.Point(30, 133);
		this.pictureBox2.Name = "pictureBox2";
		this.pictureBox2.Size = new System.Drawing.Size(39, 24);
		this.pictureBox2.TabIndex = 5;
		this.pictureBox2.TabStop = false;
		this.label4.AutoSize = true;
		this.label4.Font = new System.Drawing.Font("宋体", 18f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.label4.Location = new System.Drawing.Point(228, 47);
		this.label4.Name = "label4";
		this.label4.Size = new System.Drawing.Size(394, 24);
		this.label4.TabIndex = 6;
		this.label4.Text = "“电脑鼠原理与实践”课程教学软件";
		this.label5.AutoSize = true;
		this.label5.Font = new System.Drawing.Font("宋体", 10.5f, System.Drawing.FontStyle.Regular, System.Drawing.GraphicsUnit.Point, 134);
		this.label5.Location = new System.Drawing.Point(317, 138);
		this.label5.Name = "label5";
		this.label5.Size = new System.Drawing.Size(217, 14);
		this.label5.TabIndex = 7;
		this.label5.Text = "电子邮箱：18120194@bjtu.edu.cn";
		base.AutoScaleDimensions = new System.Drawing.SizeF(6f, 12f);
		base.AutoScaleMode = System.Windows.Forms.AutoScaleMode.Font;
		this.BackColor = System.Drawing.SystemColors.ControlLightLight;
		base.ClientSize = new System.Drawing.Size(655, 174);
		base.Controls.Add(this.label5);
		base.Controls.Add(this.label4);
		base.Controls.Add(this.pictureBox2);
		base.Controls.Add(this.pictureBox1);
		base.Controls.Add(this.label3);
		base.Controls.Add(this.label2);
		base.Controls.Add(this.label1);
		base.FormBorderStyle = System.Windows.Forms.FormBorderStyle.FixedSingle;
		base.MaximizeBox = false;
		base.Name = "Form3";
		base.StartPosition = System.Windows.Forms.FormStartPosition.CenterParent;
		this.Text = "关于我们";
		base.Load += new System.EventHandler(Form3_Load);
		((System.ComponentModel.ISupportInitialize)this.pictureBox1).EndInit();
		((System.ComponentModel.ISupportInitialize)this.pictureBox2).EndInit();
		base.ResumeLayout(false);
		base.PerformLayout();
	}
}

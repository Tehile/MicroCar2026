using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Drawing;
using System.Windows.Forms;

namespace SimulationProject;

public class frmRanking : Form
{
	private FrmSimulationTop father;

	public List<string> rankingList;

	private IContainer components;

	private ListView listView;

	private Button btnQuit;

	public Button btnUpdate;

	public frmRanking(FrmSimulationTop form)
	{
		InitializeComponent();
		father = form;
		listView.View = View.Details;
		listView.Columns.Add("排名");
		listView.Columns.Add("用户名");
		listView.Columns.Add("最佳成绩");
		listView.Columns.Add("提交时间");
		listView.Columns.Add("更新次数");
		listView.Columns[0].Width = 60;
		listView.Columns[1].Width = 85;
		listView.Columns[2].Width = 85;
		listView.Columns[3].Width = 140;
		listView.Columns[4].Width = 85;
		listView.FullRowSelect = true;
		listView.GridLines = true;
		listView.HeaderStyle = ColumnHeaderStyle.Nonclickable;
	}

	private void frmRanking_Load(object sender, EventArgs e)
	{
	}

	private void btnQuit_Click(object sender, EventArgs e)
	{
		Close();
	}

	private void btnUpdate_Click(object sender, EventArgs e)
	{
		string command = "5";
		object[] arrObjects = new object[2] { command, father.userID };
		btnUpdate.Enabled = false;
		father.getRankingWorker.RunWorkerAsync(arrObjects);
	}

	public void update_data()
	{
		listView.BeginUpdate();
		listView.Items.Clear();
		for (int i = 0; i < rankingList.Count; i++)
		{
			ListViewItem lvItem = new ListViewItem();
			string[] dataTemp = rankingList[i].Split(',');
			if (dataTemp[1] == father.userID)
			{
				lvItem.BackColor = Color.GreenYellow;
			}
			else
			{
				lvItem.BackColor = Color.White;
			}
			lvItem.Text = dataTemp[0];
			listView.Items.Add(lvItem);
			for (int j = 1; j < dataTemp.Length; j++)
			{
				ListViewItem.ListViewSubItem lvSubItem = new ListViewItem.ListViewSubItem();
				lvSubItem.Text = dataTemp[j];
				lvItem.SubItems.Add(lvSubItem);
			}
		}
		listView.EndUpdate();
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
		this.listView = new System.Windows.Forms.ListView();
		this.btnUpdate = new System.Windows.Forms.Button();
		this.btnQuit = new System.Windows.Forms.Button();
		base.SuspendLayout();
		this.listView.Location = new System.Drawing.Point(12, 12);
		this.listView.Name = "listView";
		this.listView.Size = new System.Drawing.Size(478, 510);
		this.listView.TabIndex = 0;
		this.listView.TabStop = false;
		this.listView.UseCompatibleStateImageBehavior = false;
		this.btnUpdate.Location = new System.Drawing.Point(112, 534);
		this.btnUpdate.Name = "btnUpdate";
		this.btnUpdate.Size = new System.Drawing.Size(89, 23);
		this.btnUpdate.TabIndex = 0;
		this.btnUpdate.Text = "刷新数据";
		this.btnUpdate.UseVisualStyleBackColor = true;
		this.btnUpdate.Click += new System.EventHandler(btnUpdate_Click);
		this.btnQuit.Location = new System.Drawing.Point(302, 534);
		this.btnQuit.Name = "btnQuit";
		this.btnQuit.Size = new System.Drawing.Size(89, 23);
		this.btnQuit.TabIndex = 1;
		this.btnQuit.Text = "退出";
		this.btnQuit.UseVisualStyleBackColor = true;
		this.btnQuit.Click += new System.EventHandler(btnQuit_Click);
		base.AutoScaleDimensions = new System.Drawing.SizeF(6f, 12f);
		base.AutoScaleMode = System.Windows.Forms.AutoScaleMode.Font;
		base.ClientSize = new System.Drawing.Size(502, 569);
		base.Controls.Add(this.btnQuit);
		base.Controls.Add(this.btnUpdate);
		base.Controls.Add(this.listView);
		base.FormBorderStyle = System.Windows.Forms.FormBorderStyle.FixedSingle;
		base.MaximizeBox = false;
		base.Name = "frmRanking";
		base.StartPosition = System.Windows.Forms.FormStartPosition.CenterParent;
		this.Text = "算法排行";
		base.Load += new System.EventHandler(frmRanking_Load);
		base.ResumeLayout(false);
	}
}

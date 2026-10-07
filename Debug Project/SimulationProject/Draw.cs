using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;
using MicromouseSimulation;

namespace SimulationProject;

public class Draw
{
	public struct PointF(double x, double y)
	{
		public double x = x;

		public double y = y;

		public double DistanceTo(PointF p)
		{
			return Math.Sqrt((p.x - x) * (p.x - x) + (p.y - y) * (p.y - y));
		}
	}

	public Mouse mouse;

	public List<double> actionX;

	public List<double> actionY;

	public List<double> actionD;

	private int speed;

	public int startMapX;

	public int startMapY;

	public int length;

	private PointF pCenter;

	private PointF pUp;

	private PointF pLeftUp;

	private PointF pLeftDown;

	private PointF pRightDown;

	private PointF pRightUp;

	private Pen penLine;

	private Pen penBlock;

	private SolidBrush brushSearched;

	private SolidBrush brushCrossed;

	private SolidBrush brushSprint;

	private SolidBrush brushMouseLocation;

	private SolidBrush brushMousePosture;

	private SolidBrush brushFlood;

	private SolidBrush brushAxis;

	private Font floodFont;

	private Font axisFont;

	public Draw(Mouse m, PictureBox p)
	{
		mouse = m;
		actionX = new List<double>();
		actionY = new List<double>();
		actionD = new List<double>();
		penLine = new Pen(Color.FromArgb(0, 0, 0), 1f);
		penLine.DashStyle = DashStyle.Custom;
		penLine.DashPattern = new float[2] { 3f, 3f };
		penBlock = new Pen(Color.FromArgb(255, 0, 0), 3f);
		penBlock.DashStyle = DashStyle.Solid;
		brushSearched = new SolidBrush(Color.Yellow);
		brushCrossed = new SolidBrush(Color.LightGreen);
		brushSprint = new SolidBrush(Color.BlueViolet);
		brushMouseLocation = new SolidBrush(Color.Red);
		brushMousePosture = new SolidBrush(Color.Red);
		brushFlood = new SolidBrush(Color.Black);
		brushAxis = new SolidBrush(Color.Black);
		floodFont = new Font("Arial", 12f);
		axisFont = new Font("Arial", 12f);
		pCenter = default(PointF);
		pUp = default(PointF);
		pLeftUp = default(PointF);
		pLeftDown = default(PointF);
		pRightDown = default(PointF);
		pRightUp = default(PointF);
		speed = 4;
		Prepare(p);
	}

	public void Prepare(PictureBox pictureBox)
	{
		int x = pictureBox.Size.Width;
		int y = pictureBox.Size.Height;
		if (x >= y)
		{
			length = (int)((double)y * 0.9 / 17.0);
			startMapX = (x - 16 * length) / 2;
			startMapY = (y - 16 * length) / 2;
		}
		else
		{
			length = (int)((double)x * 0.9 / 17.0);
			startMapX = (x - 16 * length) / 2;
			startMapY = (y - 16 * length) / 2;
		}
	}

	public void EmptyMap(Graphics g)
	{
		for (int i = 0; i < 17; i++)
		{
			g.DrawLine(penLine, startMapX + i * length, startMapY, startMapX + i * length, startMapY + 16 * length);
			g.DrawLine(penLine, startMapX, startMapY + i * length, startMapX + 16 * length, startMapY + i * length);
		}
		for (int j = 0; j < 16; j++)
		{
			g.DrawString(j.ToString(), axisFont, brushAxis, startMapX - length, startMapY + (16 - j) * length - 3 * length / 4);
			g.DrawString(j.ToString(), axisFont, brushAxis, startMapX + j * length + length / 4, startMapY + 16 * length + length / 4);
		}
	}

	public void RealMap(Graphics g)
	{
		for (int i = 0; i < 16; i++)
		{
			for (int j = 0; j < 16; j++)
			{
				if (mouse.map.blocks[i, j].baffle[0])
				{
					g.DrawLine(penBlock, startMapX + i * length, startMapY + (16 - j) * length - length, startMapX + i * length + length, startMapY + (16 - j) * length - length);
				}
				if (mouse.map.blocks[i, j].baffle[1])
				{
					g.DrawLine(penBlock, startMapX + i * length, startMapY + (16 - j) * length, startMapX + i * length, startMapY + (16 - j) * length - length);
				}
				if (mouse.map.blocks[i, j].baffle[2])
				{
					g.DrawLine(penBlock, startMapX + i * length, startMapY + (16 - j) * length, startMapX + i * length + length, startMapY + (16 - j) * length);
				}
				if (mouse.map.blocks[i, j].baffle[3])
				{
					g.DrawLine(penBlock, startMapX + i * length + length, startMapY + (16 - j) * length, startMapX + i * length + length, startMapY + (16 - j) * length - length);
				}
			}
		}
	}

	public void SearchedWay_mouse(Graphics g)
	{
		int numTem = 0;
		for (int i = 0; i < 16; i++)
		{
			for (int j = 0; j < 16; j++)
			{
				if (mouse.xStart == 0 && mouse.yStart == 0 && mouse.startDir == 0)
				{
					if (mouse.searchedList[numTem] == 1)
					{
						SearchedBlock(g, i, j);
					}
					numTem++;
				}
				else if (mouse.xStart == 0 && mouse.yStart == 0 && mouse.startDir == 3)
				{
					if (mouse.searchedList[numTem] == 1)
					{
						SearchedBlock(g, j, 15 - i);
					}
					numTem++;
				}
				else if (mouse.xStart == 0 && mouse.yStart == 15 && mouse.startDir == 3)
				{
					if (mouse.searchedList[numTem] == 1)
					{
						SearchedBlock(g, j, 15 - i);
					}
					numTem++;
				}
				else if (mouse.xStart == 0 && mouse.yStart == 15 && mouse.startDir == 2)
				{
					if (mouse.searchedList[numTem] == 1)
					{
						SearchedBlock(g, 15 - i, 15 - j);
					}
					numTem++;
				}
				else if (mouse.xStart == 15 && mouse.yStart == 0 && mouse.startDir == 0)
				{
					if (mouse.searchedList[numTem] == 1)
					{
						SearchedBlock(g, i, j);
					}
					numTem++;
				}
				else if (mouse.xStart == 15 && mouse.yStart == 0 && mouse.startDir == 1)
				{
					if (mouse.searchedList[numTem] == 1)
					{
						SearchedBlock(g, 15 - j, i);
					}
					numTem++;
				}
				else if (mouse.xStart == 15 && mouse.yStart == 15 && mouse.startDir == 1)
				{
					if (mouse.searchedList[numTem] == 1)
					{
						SearchedBlock(g, 15 - j, i);
					}
					numTem++;
				}
				else if (mouse.xStart == 15 && mouse.yStart == 15 && mouse.startDir == 2)
				{
					if (mouse.searchedList[numTem] == 1)
					{
						SearchedBlock(g, 15 - i, 15 - j);
					}
					numTem++;
				}
			}
		}
	}

	public void SearchedWay_real(Graphics g)
	{
		for (int i = 0; i < 16; i++)
		{
			for (int j = 0; j < 16; j++)
			{
				if (mouse.map.blocks[i, j].isSearched)
				{
					SearchedBlock(g, i, j);
				}
			}
		}
	}

	public void CrossWay_mouse(Graphics g)
	{
		for (int i = 0; i < mouse.crosswayX.Count; i++)
		{
			if (mouse.xStart == 0 && mouse.yStart == 0 && mouse.startDir == 0)
			{
				CrossBlock(g, mouse.crosswayX[i], mouse.crosswayY[i]);
			}
			else if (mouse.xStart == 0 && mouse.yStart == 0 && mouse.startDir == 3)
			{
				CrossBlock(g, mouse.crosswayY[i], 15 - mouse.crosswayX[i]);
			}
			else if (mouse.xStart == 0 && mouse.yStart == 15 && mouse.startDir == 3)
			{
				CrossBlock(g, mouse.crosswayY[i], 15 - mouse.crosswayX[i]);
			}
			else if (mouse.xStart == 0 && mouse.yStart == 15 && mouse.startDir == 2)
			{
				CrossBlock(g, 15 - mouse.crosswayX[i], 15 - mouse.crosswayY[i]);
			}
			else if (mouse.xStart == 15 && mouse.yStart == 0 && mouse.startDir == 0)
			{
				CrossBlock(g, mouse.crosswayX[i], mouse.crosswayY[i]);
			}
			else if (mouse.xStart == 15 && mouse.yStart == 0 && mouse.startDir == 1)
			{
				CrossBlock(g, 15 - mouse.crosswayY[i], mouse.crosswayX[i]);
			}
			else if (mouse.xStart == 15 && mouse.yStart == 15 && mouse.startDir == 1)
			{
				CrossBlock(g, 15 - mouse.crosswayY[i], mouse.crosswayX[i]);
			}
			else if (mouse.xStart == 15 && mouse.yStart == 15 && mouse.startDir == 2)
			{
				CrossBlock(g, 15 - mouse.crosswayX[i], 15 - mouse.crosswayY[i]);
			}
		}
	}

	public void FloodNum_mouse(Graphics g)
	{
		int numTem = 0;
		for (int i = 0; i < 16; i++)
		{
			for (int j = 0; j < 16; j++)
			{
				if (mouse.xStart == 0 && mouse.yStart == 0 && mouse.startDir == 0)
				{
					if (mouse.floodDataList[numTem].Length == 1)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 2 + i * length - 5, startMapY + 16 * length - 3 * length / 4 - j * length);
					}
					else if (mouse.floodDataList[numTem].Length == 2)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 3 + i * length - 5, startMapY + 16 * length - 3 * length / 4 - j * length);
					}
					else if (mouse.floodDataList[numTem].Length == 3)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 4 + i * length - 5, startMapY + 16 * length - 3 * length / 4 - j * length);
					}
					numTem++;
				}
				else if (mouse.xStart == 0 && mouse.yStart == 0 && mouse.startDir == 3)
				{
					if (mouse.floodDataList[numTem].Length == 1)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 2 + j * length - 5, startMapY + 16 * length - 3 * length / 4 - (15 - i) * length);
					}
					else if (mouse.floodDataList[numTem].Length == 2)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 3 + j * length - 5, startMapY + 16 * length - 3 * length / 4 - (15 - i) * length);
					}
					else if (mouse.floodDataList[numTem].Length == 3)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 4 + j * length - 5, startMapY + 16 * length - 3 * length / 4 - (15 - i) * length);
					}
					numTem++;
				}
				else if (mouse.xStart == 0 && mouse.yStart == 15 && mouse.startDir == 3)
				{
					if (mouse.floodDataList[numTem].Length == 1)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 2 + j * length - 5, startMapY + 16 * length - 3 * length / 4 - (15 - i) * length);
					}
					else if (mouse.floodDataList[numTem].Length == 2)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 3 + j * length - 5, startMapY + 16 * length - 3 * length / 4 - (15 - i) * length);
					}
					else if (mouse.floodDataList[numTem].Length == 3)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 4 + j * length - 5, startMapY + 16 * length - 3 * length / 4 - (15 - i) * length);
					}
					numTem++;
				}
				else if (mouse.xStart == 0 && mouse.yStart == 15 && mouse.startDir == 2)
				{
					if (mouse.floodDataList[numTem].Length == 1)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 2 + (15 - i) * length - 5, startMapY + 16 * length - 3 * length / 4 - (15 - j) * length);
					}
					else if (mouse.floodDataList[numTem].Length == 2)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 3 + (15 - i) * length - 5, startMapY + 16 * length - 3 * length / 4 - (15 - j) * length);
					}
					else if (mouse.floodDataList[numTem].Length == 3)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 4 + (15 - i) * length - 5, startMapY + 16 * length - 3 * length / 4 - (15 - j) * length);
					}
					numTem++;
				}
				else if (mouse.xStart == 15 && mouse.yStart == 0 && mouse.startDir == 0)
				{
					if (mouse.floodDataList[numTem].Length == 1)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 2 + i * length - 5, startMapY + 16 * length - 3 * length / 4 - j * length);
					}
					else if (mouse.floodDataList[numTem].Length == 2)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 3 + i * length - 5, startMapY + 16 * length - 3 * length / 4 - j * length);
					}
					else if (mouse.floodDataList[numTem].Length == 3)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 4 + i * length - 5, startMapY + 16 * length - 3 * length / 4 - j * length);
					}
					numTem++;
				}
				else if (mouse.xStart == 15 && mouse.yStart == 0 && mouse.startDir == 1)
				{
					if (mouse.floodDataList[numTem].Length == 1)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 2 + (15 - j) * length - 5, startMapY + 16 * length - 3 * length / 4 - i * length);
					}
					else if (mouse.floodDataList[numTem].Length == 2)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 3 + (15 - j) * length - 5, startMapY + 16 * length - 3 * length / 4 - i * length);
					}
					else if (mouse.floodDataList[numTem].Length == 3)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 4 + (15 - j) * length - 5, startMapY + 16 * length - 3 * length / 4 - i * length);
					}
					numTem++;
				}
				else if (mouse.xStart == 15 && mouse.yStart == 15 && mouse.startDir == 1)
				{
					if (mouse.floodDataList[numTem].Length == 1)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 2 + (15 - j) * length - 5, startMapY + 16 * length - 3 * length / 4 - i * length);
					}
					else if (mouse.floodDataList[numTem].Length == 2)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 3 + (15 - j) * length - 5, startMapY + 16 * length - 3 * length / 4 - i * length);
					}
					else if (mouse.floodDataList[numTem].Length == 3)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 4 + (15 - j) * length - 5, startMapY + 16 * length - 3 * length / 4 - i * length);
					}
					numTem++;
				}
				else if (mouse.xStart == 15 && mouse.yStart == 15 && mouse.startDir == 2)
				{
					if (mouse.floodDataList[numTem].Length == 1)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 2 + (15 - i) * length - 5, startMapY + 16 * length - 3 * length / 4 - (15 - j) * length);
					}
					else if (mouse.floodDataList[numTem].Length == 2)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 3 + (15 - i) * length - 5, startMapY + 16 * length - 3 * length / 4 - (15 - j) * length);
					}
					else if (mouse.floodDataList[numTem].Length == 3)
					{
						g.DrawString(mouse.floodDataList[numTem], floodFont, brushFlood, startMapX + length / 4 + (15 - i) * length - 5, startMapY + 16 * length - 3 * length / 4 - (15 - j) * length);
					}
					numTem++;
				}
			}
		}
	}

	public void FloodNum_real(Graphics g)
	{
		for (int i = 0; i < 16; i++)
		{
			for (int j = 0; j < 16; j++)
			{
				g.DrawString(mouse.map.blocks[i, j].flood.ToString("D3"), floodFont, brushFlood, startMapX + length / 4 + i * length - 5, startMapY + 16 * length - 3 * length / 4 - j * length);
			}
		}
	}

	public void CrossWay_real(Graphics g)
	{
		for (int i = 0; i < 16; i++)
		{
			for (int j = 0; j < 16; j++)
			{
				if (mouse.map.blocks[i, j].isCrossway)
				{
					CrossBlock(g, i, j);
				}
			}
		}
	}

	public void MousePosture(Graphics g)
	{
		getMouseOutline();
		g.FillPolygon(brushMousePosture, new Point[5]
		{
			new Point((int)pUp.x, (int)pUp.y),
			new Point((int)pLeftUp.x, (int)pLeftUp.y),
			new Point((int)pLeftDown.x, (int)pLeftDown.y),
			new Point((int)pRightDown.x, (int)pRightDown.y),
			new Point((int)pRightUp.x, (int)pRightUp.y)
		});
	}

	public void updateMouseCenter()
	{
		actionX.Clear();
		actionY.Clear();
		actionD.Clear();
		actionX.Add(((double)mouse.x + 0.5) * (double)length);
		actionY.Add(((double)mouse.y + 0.5) * (double)length);
		switch (mouse.absoluteDir)
		{
		case 0:
			actionD.Add(0.0);
			break;
		case 1:
			actionD.Add(Math.PI / 2.0);
			break;
		case 2:
			actionD.Add(Math.PI);
			break;
		case 3:
			actionD.Add(-Math.PI / 2.0);
			break;
		}
	}

	public void StepTurnRightAction()
	{
		int times = speed;
		double PI05 = Math.PI / (double)times / 2.0;
		for (int i = 0; i < times; i++)
		{
			actionX.Add(actionX[actionX.Count - 1]);
			actionY.Add(actionY[actionY.Count - 1]);
			actionD.Add(actionD[actionD.Count - 1] - PI05);
		}
	}

	public void StepTurnLeftAction()
	{
		int times = speed;
		double PI05 = Math.PI / (double)times / 2.0;
		for (int i = 0; i < times; i++)
		{
			actionX.Add(actionX[actionX.Count - 1]);
			actionY.Add(actionY[actionY.Count - 1]);
			actionD.Add(actionD[actionD.Count - 1] + PI05);
		}
	}

	public void TurnBackAction()
	{
		int times = speed * 2;
		double PI10 = Math.PI / (double)times;
		for (int i = 0; i < times; i++)
		{
			actionX.Add(actionX[actionX.Count - 1]);
			actionY.Add(actionY[actionY.Count - 1]);
			actionD.Add(actionD[actionD.Count - 1] - PI10);
		}
	}

	public void MoveOneBlockAction()
	{
		int times = speed;
		double LEN10 = (double)length * 1.0 / (double)times;
		switch (mouse.absoluteDir)
		{
		case 0:
		{
			for (int l = 0; l < times; l++)
			{
				actionX.Add(actionX[actionX.Count - 1]);
				actionY.Add(actionY[actionY.Count - 1] + LEN10);
				actionD.Add(actionD[actionD.Count - 1]);
			}
			break;
		}
		case 1:
		{
			for (int j = 0; j < times; j++)
			{
				actionX.Add(actionX[actionX.Count - 1] - LEN10);
				actionY.Add(actionY[actionY.Count - 1]);
				actionD.Add(actionD[actionD.Count - 1]);
			}
			break;
		}
		case 2:
		{
			for (int k = 0; k < times; k++)
			{
				actionX.Add(actionX[actionX.Count - 1]);
				actionY.Add(actionY[actionY.Count - 1] - LEN10);
				actionD.Add(actionD[actionD.Count - 1]);
			}
			break;
		}
		case 3:
		{
			for (int i = 0; i < times; i++)
			{
				actionX.Add(actionX[actionX.Count - 1] + LEN10);
				actionY.Add(actionY[actionY.Count - 1]);
				actionD.Add(actionD[actionD.Count - 1]);
			}
			break;
		}
		}
	}

	public void getMouseOutline()
	{
		pCenter.x = (double)startMapX + actionX[0];
		pCenter.y = (double)(startMapY + 16 * length) - actionY[0];
		double unit = (double)length / 18.0;
		PointF pUpTem = new PointF(pCenter.x, pCenter.y - 8.0 * unit);
		PointF pLeftUpTem = new PointF(pCenter.x - 4.0 * unit, pCenter.y - 4.0 * unit);
		PointF pLeftDownTem = new PointF(pCenter.x - 4.0 * unit, pCenter.y + 4.0 * unit);
		PointF pRightDownTem = new PointF(pCenter.x + 4.0 * unit, pCenter.y + 4.0 * unit);
		PointF pRightUpTem = new PointF(pCenter.x + 4.0 * unit, pCenter.y - 4.0 * unit);
		pUp = RotatePoint(pUpTem, pCenter, actionD[0]);
		pLeftUp = RotatePoint(pLeftUpTem, pCenter, actionD[0]);
		pLeftDown = RotatePoint(pLeftDownTem, pCenter, actionD[0]);
		pRightDown = RotatePoint(pRightDownTem, pCenter, actionD[0]);
		pRightUp = RotatePoint(pRightUpTem, pCenter, actionD[0]);
	}

	public void CrossBlock(Graphics g, int x, int y)
	{
		g.FillRectangle(brushCrossed, startMapX + x * length + 1, startMapY + (16 - y) * length - length + 1, length - 1, length - 1);
	}

	public void SearchedBlock(Graphics g, int x, int y)
	{
		g.FillRectangle(brushSearched, startMapX + x * length + 1, startMapY + (16 - y) * length - length + 1, length - 1, length - 1);
	}

	private static double radPOX(double x, double y)
	{
		if (x == 0.0 && y == 0.0)
		{
			return 0.0;
		}
		if (y == 0.0 && x > 0.0)
		{
			return 0.0;
		}
		if (y == 0.0 && x < 0.0)
		{
			return Math.PI;
		}
		if (x == 0.0 && y > 0.0)
		{
			return Math.PI / 2.0;
		}
		if (x == 0.0 && y < 0.0)
		{
			return 4.71238898038469;
		}
		if (x > 0.0 && y > 0.0)
		{
			return Math.Atan(y / x);
		}
		if (x < 0.0 && y > 0.0)
		{
			return Math.PI - Math.Atan(y / (0.0 - x));
		}
		if (x < 0.0 && y < 0.0)
		{
			return Math.PI + Math.Atan((0.0 - y) / (0.0 - x));
		}
		if (x > 0.0 && y < 0.0)
		{
			return Math.PI * 2.0 - Math.Atan((0.0 - y) / x);
		}
		return 0.0;
	}

	private static PointF RotatePoint(PointF P, PointF A, double rad, bool isClockwise = true)
	{
		PointF Temp1 = new PointF(P.x - A.x, P.y - A.y);
		double lenO2Temp1 = Temp1.DistanceTo(new PointF(0.0, 0.0));
		double angT2OX = radPOX(Temp1.x, Temp1.y) - (double)(isClockwise ? 1 : (-1)) * rad;
		PointF Temp2 = new PointF(lenO2Temp1 * Math.Cos(angT2OX), lenO2Temp1 * Math.Sin(angT2OX));
		return new PointF(Temp2.x + A.x, Temp2.y + A.y);
	}

	public bool UpdateUserMapBlock(int x, int y)
	{
		int thickness = 4;
		int mapX = (x - startMapX) / length;
		int mapY = 15 - (y - startMapY) / length;
		if (x >= startMapX - thickness && x <= startMapX + thickness)
		{
			if (!mouse.map.blocks[0, mapY].baffle[1])
			{
				mouse.map.blocks[0, mapY].baffle[1] = true;
			}
			else
			{
				mouse.map.blocks[0, mapY].baffle[1] = false;
			}
			return true;
		}
		if (x >= startMapX + 16 * length - thickness && x <= startMapX + 16 * length + thickness)
		{
			if (!mouse.map.blocks[15, mapY].baffle[3])
			{
				mouse.map.blocks[15, mapY].baffle[3] = true;
			}
			else
			{
				mouse.map.blocks[15, mapY].baffle[3] = false;
			}
			return true;
		}
		if (y >= startMapY - thickness && y <= startMapY + thickness)
		{
			if (!mouse.map.blocks[mapX, 15].baffle[0])
			{
				mouse.map.blocks[mapX, 15].baffle[0] = true;
			}
			else
			{
				mouse.map.blocks[mapX, 15].baffle[0] = false;
			}
			return true;
		}
		if (y >= startMapY + 16 * length - thickness && y <= startMapY + 16 * length + thickness)
		{
			if (!mouse.map.blocks[mapX, 0].baffle[2])
			{
				mouse.map.blocks[mapX, 0].baffle[2] = true;
			}
			else
			{
				mouse.map.blocks[mapX, 0].baffle[2] = false;
			}
			return true;
		}
		if (mapX >= 0 && mapX <= 15 && mapY >= 0 && mapY <= 15)
		{
			if (x - startMapX - (x - startMapX) / length * length >= 0 && x - startMapX - (x - startMapX) / length * length <= thickness)
			{
				if (!mouse.map.blocks[mapX, mapY].baffle[1])
				{
					mouse.map.blocks[mapX, mapY].baffle[1] = true;
					if (mapX != 0)
					{
						mouse.map.blocks[mapX - 1, mapY].baffle[3] = true;
					}
				}
				else
				{
					mouse.map.blocks[mapX, mapY].baffle[1] = false;
					if (mapX != 0)
					{
						mouse.map.blocks[mapX - 1, mapY].baffle[3] = false;
					}
				}
			}
			else if (x - startMapX - (x - startMapX) / length * length >= length - thickness && x - startMapX - (x - startMapX) / length * length <= length)
			{
				if (!mouse.map.blocks[mapX, mapY].baffle[3])
				{
					mouse.map.blocks[mapX, mapY].baffle[3] = true;
					if (mapX != 15)
					{
						mouse.map.blocks[mapX + 1, mapY].baffle[1] = true;
					}
				}
				else
				{
					mouse.map.blocks[mapX, mapY].baffle[3] = false;
					if (mapX != 15)
					{
						mouse.map.blocks[mapX + 1, mapY].baffle[1] = false;
					}
				}
			}
			if (y - startMapY - (y - startMapY) / length * length >= 0 && y - startMapY - (y - startMapY) / length * length <= thickness)
			{
				if (!mouse.map.blocks[mapX, mapY].baffle[0])
				{
					mouse.map.blocks[mapX, mapY].baffle[0] = true;
					if (mapY != 15)
					{
						mouse.map.blocks[mapX, mapY + 1].baffle[2] = true;
					}
				}
				else
				{
					mouse.map.blocks[mapX, mapY].baffle[0] = false;
					if (mapY != 15)
					{
						mouse.map.blocks[mapX, mapY + 1].baffle[2] = false;
					}
				}
			}
			else if (y - startMapY - (y - startMapY) / length * length >= length - thickness && y - startMapY - (y - startMapY) / length * length <= length)
			{
				if (!mouse.map.blocks[mapX, mapY].baffle[2])
				{
					mouse.map.blocks[mapX, mapY].baffle[2] = true;
					if (mapY != 0)
					{
						mouse.map.blocks[mapX, mapY - 1].baffle[0] = true;
					}
				}
				else
				{
					mouse.map.blocks[mapX, mapY].baffle[2] = false;
					if (mapY != 0)
					{
						mouse.map.blocks[mapX, mapY - 1].baffle[0] = false;
					}
				}
			}
			return true;
		}
		return false;
	}

	public void MouseLocation(Graphics g)
	{
		g.FillEllipse(brushMouseLocation, startMapX + mouse.x * length + length / 4, startMapY + (16 - mouse.y) * length - 3 * length / 4, length / 2, length / 2);
	}
}

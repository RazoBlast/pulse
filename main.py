#!/usr/bin/env python3
import glob
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import psutil
from PySide6.QtCore import Qt, QObject, QTimer, QPointF, QRectF, Signal
from PySide6.QtGui import (QBrush, QColor, QFont, QFontDatabase,
                           QLinearGradient, QPainter, QPainterPath, QPen,
                           QSyntaxHighlighter, QTextCharFormat)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup,
                               QCheckBox, QComboBox, QFrame, QGridLayout,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QSizePolicy, QStackedWidget,
                               QStyle, QStyledItemDelegate, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

BG = "#1b2023"
SURFACE = "#232a2f"
BORDER = "#36404a"
GRID = "#2e373e"
TEXT = "#efeaf1"
MUTED = "#919ba5"
ACCENT = "#a155a3"
ACCENT_LIGHT = "#cf8ed1"
ACCENT_GLOW = "#ecc6ee"
ACCENT_DEEP = "#6b2f6d"
FONT_STACK = '"JetBrains Mono", "JetBrainsMono Nerd Font", "DejaVu Sans Mono", monospace'

HISTORY = 90

QSS = f"""
* {{ font-family: {FONT_STACK}; }}
QMainWindow, QWidget#root {{ background: {BG}; }}
QLabel {{ color: {TEXT}; background: transparent; }}
QFrame#card {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 14px; }}
QLabel#title {{ color: {MUTED}; font-size: 12px; }}
QLabel#value {{ font-size: 24px; font-weight: 700; }}
QLabel#sub {{ color: {MUTED}; font-size: 12px; }}
QLabel#app {{ font-size: 20px; font-weight: 600; }}
QLabel#status {{ color: {MUTED}; font-size: 12px; }}
QPushButton, QComboBox {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 8px; padding: 6px 14px; font-size: 12px;
}}
QPushButton:hover, QComboBox:hover {{ border-color: {ACCENT_LIGHT}; }}
QPushButton:checked {{ background: {ACCENT}; color: {TEXT}; border-color: {ACCENT_LIGHT}; }}
QPushButton:disabled {{ color: {MUTED}; border-color: {GRID}; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT_LIGHT}; }}
QPushButton#primary:disabled {{ background: {ACCENT_DEEP}; color: {MUTED}; }}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background: {SURFACE}; color: {TEXT}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DEEP};
}}
QLineEdit {{
    background: {BG}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 8px; padding: 6px 10px; font-size: 12px;
    selection-background-color: {ACCENT_DEEP};
}}
QLineEdit:focus {{ border-color: {ACCENT_LIGHT}; }}
QPlainTextEdit {{
    background: {BG}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 10px; padding: 8px; font-size: 12px;
    selection-background-color: {ACCENT_DEEP};
}}
QCheckBox {{ color: {TEXT}; font-size: 12px; spacing: 8px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px; border: 1px solid {BORDER};
    border-radius: 4px; background: {BG};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT_LIGHT}; }}
QMessageBox {{ background: {SURFACE}; }}
QTableWidget {{
    background: transparent; border: none; color: {TEXT};
    gridline-color: transparent; font-size: 12px; outline: 0;
}}
QTableWidget::item {{ padding: 4px 8px; border-bottom: 1px solid {GRID}; }}
QTableWidget::item:selected {{ background: {ACCENT_DEEP}; color: {TEXT}; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
    background: transparent; color: {MUTED}; border: none;
    border-bottom: 1px solid {BORDER}; padding: 6px 8px; font-size: 12px;
}}
QScrollBar:vertical {{ background: transparent; width: 8px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {ACCENT_DEEP}; border-radius: 4px; min-height: 24px; }}
QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {ACCENT_DEEP}; border-radius: 4px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
"""

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x01\x02]")
ERR_RE = re.compile(r"\b(error|failed|failure|critical|fatal|panic)\b")
HEAD_RE = re.compile(r"^\S+\s+\S+\s+")


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_rate(n: float) -> str:
    return fmt_bytes(n) + "/s"


def load_color(pct: float) -> QColor:
    if pct < 60:
        return QColor(ACCENT)
    if pct < 85:
        return QColor(ACCENT_LIGHT)
    return QColor(ACCENT_GLOW)


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _num(path, scale=1.0):
    v = _read(path)
    try:
        return float(v) / scale
    except (TypeError, ValueError):
        return None


def run_cmd(args, timeout=15):
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, ANSI_RE.sub("", r.stdout), ANSI_RE.sub("", r.stderr)
    except FileNotFoundError:
        return 127, "", f"{args[0]} is not installed"
    except subprocess.TimeoutExpired:
        return 124, "", "the command timed out"


def last_line(*texts):
    for t in texts:
        lines = [l.strip() for l in (t or "").splitlines() if l.strip()]
        if lines:
            return lines[-1]
    return "no output"


def split_nm(line):
    parts = re.split(r"(?<!\\):", line)
    return [p.replace("\\:", ":").replace("\\\\", "\\") for p in parts]


class _Bridge(QObject):
    deliver = Signal(object, object)

    def __init__(self):
        super().__init__()
        self.deliver.connect(self._call, Qt.QueuedConnection)

    def _call(self, callback, value):
        callback(value)


_bridge = None


def run_async(fn, callback):
    global _bridge
    if _bridge is None:
        _bridge = _Bridge()

    def work():
        try:
            result = fn()
        except Exception as e:
            result = e
        _bridge.deliver.emit(callback, result)

    threading.Thread(target=work, daemon=True).start()


class GpuSource:
    def __init__(self):
        self.kind = None
        self.name = "No GPU"
        self.peak = {}
        self._nvml = None
        self._h = None
        self._dev = None
        self._hw = None
        if not self._init_nvidia():
            self._init_amd()

    def _init_nvidia(self):
        try:
            import pynvml
            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() < 1:
                return False
            self._nvml = pynvml
            self._h = pynvml.nvmlDeviceGetHandleByIndex(0)
            name = pynvml.nvmlDeviceGetName(self._h)
            self.name = name.decode() if isinstance(name, bytes) else name
            self.kind = "nvidia"
            return True
        except Exception:
            return False

    def _init_amd(self):
        for dev in sorted(glob.glob("/sys/class/drm/card[0-9]*/device")):
            if (_read(f"{dev}/vendor") == "0x1002"
                    and os.path.exists(f"{dev}/gpu_busy_percent")):
                hw = glob.glob(f"{dev}/hwmon/hwmon*")
                self._dev = dev
                self._hw = hw[0] if hw else None
                self.name = _read(f"{dev}/product_name") or "AMD Radeon GPU"
                self.kind = "amd"
                return True
        return False

    def _nv(self, fn, *args, scale=1.0):
        try:
            v = fn(self._h, *args)
            return v / scale if scale != 1.0 else v
        except Exception:
            return None

    def _sample_nvidia(self):
        n = self._nvml
        util = self._nv(n.nvmlDeviceGetUtilizationRates)
        mem = self._nv(n.nvmlDeviceGetMemoryInfo)
        pstate = self._nv(n.nvmlDeviceGetPerformanceState)
        return dict(
            util=util.gpu if util else None,
            vram_used=mem.used if mem else None,
            vram_total=mem.total if mem else None,
            temp=self._nv(n.nvmlDeviceGetTemperature, n.NVML_TEMPERATURE_GPU),
            temp_hot=None,
            temp_mem=None,
            temp_limit=self._nv(n.nvmlDeviceGetTemperatureThreshold,
                                n.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN),
            power=self._nv(n.nvmlDeviceGetPowerUsage, scale=1000.0),
            power_limit=self._nv(n.nvmlDeviceGetEnforcedPowerLimit, scale=1000.0),
            clock=self._nv(n.nvmlDeviceGetClockInfo, n.NVML_CLOCK_GRAPHICS),
            clock_max=self._nv(n.nvmlDeviceGetMaxClockInfo, n.NVML_CLOCK_GRAPHICS),
            mem_clock=self._nv(n.nvmlDeviceGetClockInfo, n.NVML_CLOCK_MEM),
            mem_clock_max=self._nv(n.nvmlDeviceGetMaxClockInfo, n.NVML_CLOCK_MEM),
            fan=self._nv(n.nvmlDeviceGetFanSpeed),
            state=f"P{pstate}" if pstate is not None else None,
        )

    def _sample_amd(self):
        d, hw = self._dev, self._hw

        def h(name, scale=1.0):
            return _num(f"{hw}/{name}", scale) if hw else None

        pwm = h("pwm1")
        return dict(
            util=_num(f"{d}/gpu_busy_percent"),
            vram_used=_num(f"{d}/mem_info_vram_used"),
            vram_total=_num(f"{d}/mem_info_vram_total"),
            temp=h("temp1_input", 1000.0),
            temp_hot=h("temp2_input", 1000.0),
            temp_mem=h("temp3_input", 1000.0),
            temp_limit=h("temp2_crit", 1000.0) or h("temp1_crit", 1000.0),
            power=h("power1_average", 1e6) or h("power1_input", 1e6),
            power_limit=h("power1_cap", 1e6),
            clock=h("freq1_input", 1e6),
            clock_max=None,
            mem_clock=h("freq2_input", 1e6),
            mem_clock_max=None,
            fan=pwm / 255 * 100 if pwm is not None else None,
            state=None,
        )

    def sample(self):
        if self.kind == "nvidia":
            s = self._sample_nvidia()
        elif self.kind == "amd":
            s = self._sample_amd()
        else:
            return None
        for key in ("clock", "mem_clock"):
            if s[key] is not None and s[key + "_max"] is None:
                self.peak[key] = max(self.peak.get(key, 0.0), s[key])
                s[key + "_max"] = self.peak[key]
        return s


class LineChart(QWidget):
    def __init__(self, colors, fixed_max=None, min_max=1.0, fixed_min=0.0,
                 parent=None):
        super().__init__(parent)
        self.colors = [QColor(c) for c in colors]
        self.series = [deque([0.0] * HISTORY, maxlen=HISTORY) for _ in colors]
        self.fixed_max = fixed_max
        self.fixed_min = fixed_min
        self.min_max = min_max
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def push(self, *values):
        for s, v in zip(self.series, values):
            s.append(float(v))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0, 6, 0, -2)

        p.setPen(QPen(QColor(GRID), 1))
        for i in range(4):
            y = r.top() + r.height() * i / 3
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))

        top = self.fixed_max or max(
            self.min_max, max(max(s) for s in self.series) * 1.25)
        base = self.fixed_min
        span = max(top - base, 1e-9)
        step = r.width() / (HISTORY - 1)

        for color, data in zip(self.colors, self.series):
            pts = [QPointF(r.left() + i * step,
                           r.bottom() - min(max((v - base) / span, 0.0), 1.0)
                           * r.height())
                   for i, v in enumerate(data)]
            line = QPainterPath(pts[0])
            for a, b in zip(pts, pts[1:]):
                cx = (a.x() + b.x()) / 2
                line.cubicTo(cx, a.y(), cx, b.y(), b.x(), b.y())

            fill = QPainterPath(line)
            fill.lineTo(pts[-1].x(), r.bottom())
            fill.lineTo(pts[0].x(), r.bottom())
            fill.closeSubpath()

            grad = QLinearGradient(0, r.top(), 0, r.bottom())
            top_c, bot_c = QColor(color), QColor(color)
            top_c.setAlpha(120)
            bot_c.setAlpha(0)
            grad.setColorAt(0, top_c)
            grad.setColorAt(1, bot_c)
            p.fillPath(fill, grad)

            stroke = QLinearGradient(r.left(), 0, r.right(), 0)
            stroke.setColorAt(0, color.darker(190))
            stroke.setColorAt(1, color.lighter(130))
            p.setPen(QPen(QBrush(stroke), 2.2, Qt.SolidLine, Qt.RoundCap,
                          Qt.RoundJoin))
            p.drawPath(line)


class CoreBars(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = []
        self.setMinimumHeight(110)

    def set_values(self, values):
        self.values = list(values)
        self.update()

    def paintEvent(self, _):
        if not self.values:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = len(self.values)
        label_h = 16
        gap = 6
        w = self.width()
        bar_w = min(28.0, max(6.0, (w - gap * (n - 1)) / n))
        track_h = self.height() - label_h
        font = QFont(self.font())
        font.setPixelSize(10)
        p.setFont(font)

        for i, v in enumerate(self.values):
            x = i * (bar_w + gap)
            track = QRectF(x, 0, bar_w, track_h)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(GRID))
            p.drawRoundedRect(track, 4, 4)

            h = max(4.0, track_h * v / 100)
            fill = QRectF(x, track_h - h, bar_w, h)
            c = load_color(v)
            g = QLinearGradient(0, fill.top(), 0, fill.bottom())
            g.setColorAt(0, c.lighter(120))
            g.setColorAt(1, c.darker(190))
            p.setBrush(g)
            p.drawRoundedRect(fill, 4, 4)

            p.setPen(QColor(MUTED))
            p.drawText(QRectF(x - 4, track_h + 2, bar_w + 8, label_h),
                       Qt.AlignHCenter | Qt.AlignVCenter, str(i))


class MetricBar(QWidget):
    def __init__(self, label, parent=None):
        super().__init__(parent)
        self.label = label
        self.text = "n/a"
        self.frac = 0.0
        self.setFixedHeight(40)

    def set(self, text, frac):
        self.text = text
        self.frac = max(0.0, min(1.0, frac))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(0, 0, w, 20), Qt.AlignLeft | Qt.AlignVCenter,
                   self.label)
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(0, 0, w, 20), Qt.AlignRight | Qt.AlignVCenter,
                   self.text)

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(GRID))
        p.drawRoundedRect(QRectF(0, 26, w, 8), 4, 4)
        if self.frac > 0:
            fill = QRectF(0, 26, max(8.0, w * self.frac), 8)
            g = QLinearGradient(fill.left(), 0, fill.right(), 0)
            g.setColorAt(0, QColor(ACCENT_DEEP))
            g.setColorAt(0.6, QColor(ACCENT))
            g.setColorAt(1, QColor(ACCENT_GLOW))
            p.setBrush(g)
            p.drawRoundedRect(fill, 4, 4)


class BarDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(option.rect)
        if option.state & QStyle.State_Selected:
            painter.fillRect(rect, QColor(ACCENT_DEEP))
        frac = index.data(Qt.UserRole)
        text = index.data(Qt.DisplayRole) or ""
        painter.setPen(QPen(QColor(GRID), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        painter.setPen(QColor(TEXT))
        bottom_pad = -10 if frac is not None else 0
        painter.drawText(rect.adjusted(8, 2, -8, bottom_pad),
                         Qt.AlignLeft | Qt.AlignVCenter, text)
        if frac is not None:
            track = QRectF(rect.left() + 8, rect.bottom() - 10,
                           rect.width() - 16, 4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(GRID))
            painter.drawRoundedRect(track, 2, 2)
            fill = QRectF(track.left(), track.top(),
                          max(4.0, track.width() * min(max(frac, 0.0), 1.0)), 4)
            g = QLinearGradient(fill.left(), 0, fill.right(), 0)
            g.setColorAt(0, QColor(ACCENT_DEEP))
            g.setColorAt(0.6, QColor(ACCENT))
            g.setColorAt(1, QColor(ACCENT_GLOW))
            painter.setBrush(g)
            painter.drawRoundedRect(fill, 2, 2)
        painter.restore()


class LogHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self.err = self._fmt(ACCENT_GLOW, True)
        self.warn = self._fmt(ACCENT_LIGHT)
        self.head = self._fmt(MUTED)

    @staticmethod
    def _fmt(color, bold=False):
        f = QTextCharFormat()
        f.setForeground(QColor(color))
        if bold:
            f.setFontWeight(QFont.Bold)
        return f

    def highlightBlock(self, text):
        low = text.lower()
        if ERR_RE.search(low):
            self.setFormat(0, len(text), self.err)
        elif "warn" in low:
            self.setFormat(0, len(text), self.warn)
        m = HEAD_RE.match(text)
        if m:
            self.setFormat(0, m.end(), self.head)


def make_table(headers, stretch=0, fixed=None):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(34)
    t.setShowGrid(False)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    hh = t.horizontalHeader()
    hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    for c in range(len(headers)):
        if fixed and c in fixed:
            hh.setSectionResizeMode(c, QHeaderView.Fixed)
            t.setColumnWidth(c, fixed[c])
        elif c == stretch:
            hh.setSectionResizeMode(c, QHeaderView.Stretch)
        else:
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
    return t


def table_item(text, color=TEXT, frac=None, key=None):
    it = QTableWidgetItem(text)
    it.setForeground(QColor(color))
    if frac is not None:
        it.setData(Qt.UserRole, frac)
    if key is not None:
        it.setData(Qt.UserRole + 1, key)
    return it


def selected_key(table):
    row = table.currentRow()
    if row < 0:
        return None
    it = table.item(row, 0)
    return it.data(Qt.UserRole + 1) if it else None


def card_frame(margins=(18, 14, 18, 14), spacing=10):
    f = QFrame()
    f.setObjectName("card")
    lay = QVBoxLayout(f)
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    return f, lay


class Card(QFrame):
    def __init__(self, title, colors, fixed_max=None, min_max=1.0,
                 fixed_min=0.0):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(2)
        self.title = QLabel(title)
        self.title.setObjectName("title")
        self.value = QLabel("n/a")
        self.value.setObjectName("value")
        self.sub = QLabel(" ")
        self.sub.setObjectName("sub")
        self.chart = LineChart(colors, fixed_max, min_max, fixed_min)
        lay.addWidget(self.title)
        lay.addWidget(self.value)
        lay.addWidget(self.sub)
        lay.addSpacing(6)
        lay.addWidget(self.chart, 1)


class GpuPage(QWidget):
    def __init__(self, gpu):
        super().__init__()
        self.gpu = gpu
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        if gpu.kind is None:
            msg = QLabel("No supported GPU found")
            msg.setObjectName("value")
            msg.setAlignment(Qt.AlignCenter)
            hint = QLabel("NVIDIA: pip install nvidia-ml-py and use the "
                          "proprietary driver. AMD: the amdgpu driver "
                          "exposes stats through sysfs.")
            hint.setObjectName("sub")
            hint.setAlignment(Qt.AlignCenter)
            hint.setWordWrap(True)
            lay.addStretch(1)
            lay.addWidget(msg)
            lay.addWidget(hint)
            lay.addStretch(1)
            return

        self.usage = Card("GPU usage", [ACCENT], fixed_max=100)
        self.vram = Card("Video memory", [ACCENT_LIGHT], fixed_max=100)
        self.temp = Card("Temperature", [ACCENT_GLOW, ACCENT],
                         fixed_max=100, fixed_min=20)
        self.power = Card("Power draw", [ACCENT], min_max=50)
        grid = QGridLayout()
        grid.setSpacing(14)
        for i, c in enumerate((self.usage, self.vram, self.temp, self.power)):
            grid.addWidget(c, i // 2, i % 2)
        lay.addLayout(grid, 5)

        bottom = QHBoxLayout()
        bottom.setSpacing(14)

        perf = QFrame(objectName="card")
        pl = QVBoxLayout(perf)
        pl.setContentsMargins(18, 14, 18, 14)
        pl.setSpacing(4)
        self.perf_title = QLabel("Performance")
        self.perf_title.setObjectName("title")
        pl.addWidget(self.perf_title)
        self.b_clock = MetricBar("Graphics clock")
        self.b_mclock = MetricBar("Memory clock")
        self.b_power = MetricBar("Power versus limit")
        self.b_fan = MetricBar("Fan speed")
        self.b_thermal = MetricBar("Temperature versus limit")
        for b in (self.b_clock, self.b_mclock, self.b_power, self.b_fan,
                  self.b_thermal):
            pl.addWidget(b)
        pl.addStretch(1)

        self.eff = Card("Efficiency", [ACCENT_LIGHT], min_max=1.0)

        bottom.addWidget(perf, 6)
        bottom.addWidget(self.eff, 5)
        lay.addLayout(bottom, 5)

    def update_stats(self, s):
        if self.gpu.kind is None or s is None:
            return
        u, p, pl = s["util"], s["power"], s["power_limit"]

        if u is not None:
            self.usage.value.setText(f"{u:.0f}%")
            self.usage.sub.setText(f"performance state {s['state']}"
                                   if s["state"] else self.gpu.name)
            self.usage.chart.push(u)

        if s["vram_used"] is not None and s["vram_total"]:
            pct = s["vram_used"] / s["vram_total"] * 100
            self.vram.value.setText(f"{pct:.0f}%")
            self.vram.sub.setText(
                f"{fmt_bytes(s['vram_used'])} of {fmt_bytes(s['vram_total'])}")
            self.vram.chart.push(pct)

        t = s["temp"]
        if t is not None:
            self.temp.value.setText(f"{t:.0f} °C")
            parts = []
            if s["temp_hot"] is not None:
                parts.append(f"hotspot {s['temp_hot']:.0f} °C")
            if s["temp_mem"] is not None:
                parts.append(f"memory {s['temp_mem']:.0f} °C")
            if not parts and s["temp_limit"]:
                parts.append(f"slowdown at {s['temp_limit']:.0f} °C")
            self.temp.sub.setText("   ".join(parts) or " ")
            hot = s["temp_hot"] if s["temp_hot"] is not None else t
            self.temp.chart.push(hot, t)

        if p is not None:
            self.power.value.setText(f"{p:.0f} W")
            self.power.sub.setText(f"limit {pl:.0f} W" if pl else " ")
            self.power.chart.fixed_max = pl or None
            self.power.chart.push(p)

        if p and p > 0.5 and u is not None:
            eff = u / p
            self.eff.value.setText(f"{eff:.2f} %/W")
            if s["clock"]:
                self.eff.sub.setText(
                    f"{s['clock'] * u / 100 / p:.1f} effective MHz per W")
            self.eff.chart.push(eff)
        else:
            self.eff.value.setText("n/a")

        if s["state"]:
            self.perf_title.setText(f"Performance ({s['state']})")

        self._bar(self.b_clock, s["clock"], s["clock_max"], "{:,.0f}", "MHz")
        self._bar(self.b_mclock, s["mem_clock"], s["mem_clock_max"],
                  "{:,.0f}", "MHz")
        self._bar(self.b_power, p, pl, "{:.0f}", "W")
        if s["fan"] is not None:
            self.b_fan.set(f"{s['fan']:.0f}%", s["fan"] / 100)
        else:
            self.b_fan.set("n/a", 0)
        self._bar(self.b_thermal, t, s["temp_limit"], "{:.0f}", "°C")

    @staticmethod
    def _bar(bar, value, limit, fmt, unit):
        if value is None:
            bar.set("n/a", 0)
        elif limit:
            bar.set(f"{fmt.format(value)} / {fmt.format(limit)} {unit}",
                    value / limit)
        else:
            bar.set(f"{fmt.format(value)} {unit}", 0)


def login1_can(method):
    rc, out, _ = run_cmd(["busctl", "--system", "call",
                          "org.freedesktop.login1", "/org/freedesktop/login1",
                          "org.freedesktop.login1.Manager", method], 5)
    m = re.search(r'"(\w+)"', out)
    return m.group(1) if rc == 0 and m else None


class PowerBar(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        row = QHBoxLayout(self)
        row.setContentsMargins(18, 12, 18, 12)
        row.setSpacing(8)

        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel("Power")
        title.setObjectName("title")
        self.msg = QLabel("Every action asks for confirmation first.")
        self.msg.setObjectName("sub")
        text.addWidget(title)
        text.addWidget(self.msg)
        row.addLayout(text)
        row.addStretch(1)

        self.btn_off = QPushButton("Shut down")
        self.btn_off.setObjectName("primary")
        self.btn_sleep = QPushButton("Suspend")
        self.btn_hib = QPushButton("Hibernate")
        self.btn_hib.hide()
        self.btn_off.clicked.connect(lambda: self._act(
            "Shut down", "Shut down this computer now?",
            ["systemctl", "poweroff"]))
        self.btn_sleep.clicked.connect(lambda: self._act(
            "Suspend", "Suspend this computer now?",
            ["systemctl", "suspend"]))
        self.btn_hib.clicked.connect(lambda: self._act(
            "Hibernate", "Hibernate this computer now?",
            ["systemctl", "hibernate"]))
        for b in (self.btn_off, self.btn_sleep, self.btn_hib):
            row.addWidget(b)

        run_async(self._probe, self._probed)

    @staticmethod
    def _probe():
        return {m: login1_can(m)
                for m in ("CanPowerOff", "CanSuspend", "CanHibernate")}

    def _probed(self, r):
        if isinstance(r, Exception):
            return
        ok = ("yes", "challenge", None)
        self.btn_off.setEnabled(r["CanPowerOff"] in ok)
        self.btn_sleep.setEnabled(r["CanSuspend"] in ok)
        self.btn_hib.setVisible(r["CanHibernate"] in ("yes", "challenge"))

    def _act(self, title, question, cmd):
        answer = QMessageBox.question(
            self, title, question,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.msg.setText(f"{title}...")
        run_async(lambda: run_cmd(cmd, 20), self._done)

    def _done(self, r):
        if isinstance(r, Exception):
            self.msg.setText(str(r))
            return
        rc, out, err = r
        self.msg.setText("Every action asks for confirmation first."
                         if rc == 0 else last_line(err, out))


class LogsPage(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.prio = QComboBox()
        for label, val in (("All levels", None), ("Warnings and above", "warning"),
                           ("Errors and above", "err")):
            self.prio.addItem(label, val)
        self.lines = QComboBox()
        for n in (200, 500, 1000, 2000):
            self.lines.addItem(f"Last {n}", n)
        self.boot = QCheckBox("This boot only")
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter text")
        self.refresh_btn = QPushButton("Refresh")
        row.addWidget(self.prio)
        row.addWidget(self.lines)
        row.addWidget(self.boot)
        row.addWidget(self.filter, 1)
        row.addWidget(self.refresh_btn)
        lay.addLayout(row)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.highlighter = LogHighlighter(self.view.document())
        lay.addWidget(self.view, 1)

        self.footer = QLabel(" ")
        self.footer.setObjectName("sub")
        lay.addWidget(self.footer)

        self.raw = []
        self.prio.currentIndexChanged.connect(self.refresh)
        self.lines.currentIndexChanged.connect(self.refresh)
        self.boot.toggled.connect(self.refresh)
        self.refresh_btn.clicked.connect(self.refresh)
        self.filter.textChanged.connect(self._render)

    def refresh(self, *_):
        args = ["journalctl", "--no-pager", "-o", "short-iso",
                "-n", str(self.lines.currentData())]
        if self.boot.isChecked():
            args.append("-b")
        if self.prio.currentData():
            args += ["-p", self.prio.currentData()]
        self.footer.setText("Loading...")
        run_async(lambda: run_cmd(args, 25), self._loaded)

    def _loaded(self, r):
        if isinstance(r, Exception):
            self.raw = [str(r)]
        else:
            rc, out, err = r
            self.raw = out.splitlines()
            if rc != 0 and not self.raw:
                self.raw = [last_line(err)]
            elif err.strip() and not self.raw:
                self.raw = [last_line(err)]
        self._render()

    def _render(self, *_):
        needle = self.filter.text().lower()
        shown = [l for l in self.raw if needle in l.lower()]
        self.view.setPlainText("\n".join(shown))
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())
        self.footer.setText(f"{len(shown)} of {len(self.raw)} lines")


class PartitionsPage(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        row = QHBoxLayout()
        self.loops = QCheckBox("Show loop devices")
        self.refresh_btn = QPushButton("Refresh")
        self.info = QLabel(" ")
        self.info.setObjectName("sub")
        row.addWidget(self.info)
        row.addStretch(1)
        row.addWidget(self.loops)
        row.addWidget(self.refresh_btn)
        lay.addLayout(row)

        self.table = make_table(
            ["Device", "Size", "Type", "Filesystem", "Label or model",
             "Mount point", "Usage"], stretch=5, fixed={6: 230})
        self.table.setItemDelegateForColumn(6, BarDelegate(self.table))
        lay.addWidget(self.table, 1)

        self.loops.toggled.connect(self.refresh)
        self.refresh_btn.clicked.connect(self.refresh)

    def refresh(self, *_):
        show_loops = self.loops.isChecked()
        run_async(lambda: self._fetch(show_loops), self._loaded)

    @staticmethod
    def _fetch(show_loops):
        rows = []

        def walk(devs, depth):
            for d in devs:
                mounts = d.get("mountpoints")
                mount = next((m for m in (mounts or []) if m), None) \
                    or d.get("mountpoint")
                typ = d.get("type") or ""
                if typ == "loop" and not show_loops:
                    continue
                used = total = None
                if mount:
                    try:
                        u = psutil.disk_usage(mount)
                        used, total = u.used, u.total
                    except OSError:
                        pass
                rows.append(dict(
                    depth=depth, name=d.get("name", ""),
                    size=int(d.get("size") or 0), type=typ,
                    fstype=d.get("fstype") or "",
                    label=d.get("label") or (d.get("model") or "").strip(),
                    mount=mount or "", used=used, total=total))
                walk(d.get("children") or [], depth + 1)

        rc, out, err = run_cmd(
            ["lsblk", "-J", "-b", "-o",
             "NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS,LABEL,MODEL"], 10)
        if rc == 0:
            walk(json.loads(out).get("blockdevices", []), 0)
            return rows, None
        for p in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            rows.append(dict(depth=0, name=p.device, size=u.total, type="part",
                             fstype=p.fstype, label="", mount=p.mountpoint,
                             used=u.used, total=u.total))
        return rows, last_line(err)

    def _loaded(self, r):
        if isinstance(r, Exception):
            self.info.setText(str(r))
            return
        rows, note = r
        self.table.setRowCount(len(rows))
        for i, d in enumerate(rows):
            prefix = ("  " * (d["depth"] - 1) + "└ ") if d["depth"] else ""
            is_disk = d["depth"] == 0
            self.table.setItem(i, 0, table_item(
                prefix + d["name"], ACCENT_LIGHT if is_disk else TEXT))
            self.table.setItem(i, 1, table_item(fmt_bytes(d["size"])))
            self.table.setItem(i, 2, table_item(d["type"], MUTED))
            self.table.setItem(i, 3, table_item(d["fstype"], MUTED))
            self.table.setItem(i, 4, table_item(d["label"], MUTED))
            self.table.setItem(i, 5, table_item(d["mount"]))
            if d["total"]:
                self.table.setItem(i, 6, table_item(
                    f"{fmt_bytes(d['used'])} / {fmt_bytes(d['total'])}",
                    frac=d["used"] / d["total"]))
            else:
                self.table.setItem(i, 6, table_item(""))
        count = sum(1 for d in rows if d["depth"])
        self.info.setText(note and f"lsblk unavailable ({note}), showing mounted "
                          f"filesystems" or f"{count} partitions")


class WifiPage(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.state = QLabel("Wi-Fi")
        self.state.setObjectName("sub")
        self.radio = QPushButton("Wi-Fi off")
        self.radio.setCheckable(True)
        self.rescan = QPushButton("Rescan")
        top.addWidget(self.state)
        top.addStretch(1)
        top.addWidget(self.radio)
        top.addWidget(self.rescan)
        lay.addLayout(top)

        self.table = make_table(["", "Network", "Signal", "Security"],
                                stretch=1, fixed={2: 170})
        self.table.setItemDelegateForColumn(2, BarDelegate(self.table))
        lay.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText(
            "Password (only for networks you haven't joined before)")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.disconnect_btn = QPushButton("Disconnect")
        bottom.addWidget(self.password, 1)
        bottom.addWidget(self.connect_btn)
        bottom.addWidget(self.disconnect_btn)
        lay.addLayout(bottom)

        self.msg = QLabel(" ")
        self.msg.setObjectName("sub")
        lay.addWidget(self.msg)

        self.radio.toggled.connect(self._toggle_radio)
        self.rescan.clicked.connect(lambda: self.refresh(rescan=True))
        self.connect_btn.clicked.connect(self._connect)
        self.disconnect_btn.clicked.connect(self._disconnect)
        self.table.itemDoubleClicked.connect(lambda _: self._connect())

    def refresh(self, *_, rescan=False):
        self.msg.setText("Scanning..." if rescan else "Loading networks...")
        run_async(lambda: self._fetch(rescan), self._loaded)

    @staticmethod
    def _fetch(rescan):
        rc, out, err = run_cmd(["nmcli", "radio", "wifi"], 8)
        if rc != 0:
            return dict(enabled=False, nets=[], error=last_line(err, out))
        enabled = out.strip() == "enabled"
        nets, error = [], None
        if enabled:
            rc, out, err = run_cmd(
                ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY",
                 "dev", "wifi", "list", "--rescan",
                 "yes" if rescan else "auto"], 30)
            if rc != 0:
                error = last_line(err, out)
            best = {}
            for line in out.splitlines():
                f = split_nm(line)
                if len(f) < 4 or not f[1]:
                    continue
                try:
                    sig = int(f[2] or 0)
                except ValueError:
                    sig = 0
                entry = dict(ssid=f[1], signal=sig,
                             security=f[3] if f[3] not in ("", "--") else "Open",
                             active=f[0].strip() == "*")
                cur = best.get(f[1])
                if (cur is None or entry["active"]
                        or (not cur["active"] and sig > cur["signal"])):
                    best[f[1]] = entry
            nets = sorted(best.values(),
                          key=lambda n: (not n["active"], -n["signal"]))
        return dict(enabled=enabled, nets=nets, error=error)

    def _loaded(self, r):
        if isinstance(r, Exception):
            self.msg.setText(str(r))
            return
        self.radio.blockSignals(True)
        self.radio.setChecked(r["enabled"])
        self.radio.setText("Wi-Fi on" if r["enabled"] else "Wi-Fi off")
        self.radio.blockSignals(False)

        active = next((n for n in r["nets"] if n["active"]), None)
        if not r["enabled"]:
            self.state.setText("Wi-Fi is turned off")
        elif active:
            self.state.setText(f"Connected to {active['ssid']}")
        else:
            self.state.setText("Not connected")

        self.table.setRowCount(len(r["nets"]))
        for i, n in enumerate(r["nets"]):
            self.table.setItem(i, 0, table_item(
                "●" if n["active"] else "", ACCENT_GLOW, key=n))
            self.table.setItem(i, 1, table_item(
                n["ssid"], ACCENT_GLOW if n["active"] else TEXT))
            self.table.setItem(i, 2, table_item(
                f"{n['signal']}%", frac=n["signal"] / 100))
            self.table.setItem(i, 3, table_item(n["security"], MUTED))
        self.msg.setText(r["error"] or f"{len(r['nets'])} networks")

    def _toggle_radio(self, on):
        self.msg.setText("Updating radio...")
        run_async(lambda: run_cmd(["nmcli", "radio", "wifi",
                                   "on" if on else "off"], 10),
                  lambda _: self.refresh())

    def _connect(self):
        net = selected_key(self.table)
        if not net:
            self.msg.setText("Select a network first.")
            return
        cmd = ["nmcli", "dev", "wifi", "connect", net["ssid"]]
        if self.password.text():
            cmd += ["password", self.password.text()]
        self.msg.setText(f"Connecting to {net['ssid']}...")
        self.connect_btn.setEnabled(False)
        run_async(lambda: run_cmd(cmd, 45), self._connected)

    def _connected(self, r):
        self.connect_btn.setEnabled(True)
        if isinstance(r, Exception):
            self.msg.setText(str(r))
            return
        rc, out, err = r
        if rc == 0:
            self.password.clear()
        self.refresh()
        self.msg.setText(last_line(out, err) if rc == 0 else last_line(err, out))

    def _disconnect(self):
        self.msg.setText("Disconnecting...")
        run_async(self._disconnect_job, self._disconnected)

    @staticmethod
    def _disconnect_job():
        rc, out, err = run_cmd(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE",
                                "dev"], 8)
        for line in out.splitlines():
            f = split_nm(line)
            if len(f) >= 3 and f[1] == "wifi" and f[2].startswith("connected"):
                return run_cmd(["nmcli", "dev", "disconnect", f[0]], 15)
        return 1, "", "No active Wi-Fi connection"

    def _disconnected(self, r):
        if isinstance(r, Exception):
            self.msg.setText(str(r))
            return
        rc, out, err = r
        self.refresh()
        self.msg.setText(last_line(out, err) if rc == 0 else last_line(err, out))


class BluetoothPage(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.state = QLabel("Bluetooth")
        self.state.setObjectName("sub")
        self.power = QPushButton("Bluetooth off")
        self.power.setCheckable(True)
        self.scan_btn = QPushButton("Scan for devices")
        top.addWidget(self.state)
        top.addStretch(1)
        top.addWidget(self.power)
        top.addWidget(self.scan_btn)
        lay.addLayout(top)

        self.table = make_table(["Device", "Address", "State"], stretch=0,
                                fixed={1: 180, 2: 150})
        lay.addWidget(self.table, 1)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        self.pair_btn = QPushButton("Pair")
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.disconnect_btn = QPushButton("Disconnect")
        bottom.addStretch(1)
        for b in (self.pair_btn, self.connect_btn, self.disconnect_btn):
            bottom.addWidget(b)
        lay.addLayout(bottom)

        self.msg = QLabel(" ")
        self.msg.setObjectName("sub")
        lay.addWidget(self.msg)

        self.power.toggled.connect(self._toggle_power)
        self.scan_btn.clicked.connect(self._scan)
        self.pair_btn.clicked.connect(self._pair)
        self.connect_btn.clicked.connect(
            lambda: self._simple("connect", "Connecting", 25))
        self.disconnect_btn.clicked.connect(
            lambda: self._simple("disconnect", "Disconnecting", 15))
        self.table.itemDoubleClicked.connect(
            lambda _: self._simple("connect", "Connecting", 25))

    def refresh(self, *_):
        self.msg.setText("Loading devices...")
        run_async(self._snapshot, self._loaded)

    @staticmethod
    def _snapshot():
        rc, out, err = run_cmd(["bluetoothctl", "show"], 8)
        if rc != 0 or "No default controller" in out or not out.strip():
            return dict(error=last_line(err, out) if (err or out).strip()
                        else "No Bluetooth controller found",
                        powered=False, devs=[])
        powered = "Powered: yes" in out
        rc, out, _ = run_cmd(["bluetoothctl", "devices"], 8)
        devs = []
        for line in out.splitlines():
            m = re.match(r"Device\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s*(.*)",
                         line.strip())
            if not m:
                continue
            mac, name = m.groups()
            info = run_cmd(["bluetoothctl", "info", mac], 6)[1]
            devs.append(dict(mac=mac, name=name or mac,
                             paired="Paired: yes" in info,
                             connected="Connected: yes" in info))
        devs.sort(key=lambda d: (not d["connected"], not d["paired"],
                                 d["name"].lower()))
        return dict(error=None, powered=powered, devs=devs)

    def _loaded(self, r):
        self.scan_btn.setEnabled(True)
        if isinstance(r, Exception):
            self.msg.setText(str(r))
            return
        self.power.blockSignals(True)
        self.power.setChecked(r["powered"])
        self.power.setText("Bluetooth on" if r["powered"] else "Bluetooth off")
        self.power.blockSignals(False)

        connected = [d["name"] for d in r["devs"] if d["connected"]]
        if r["error"]:
            self.state.setText("Unavailable")
        elif not r["powered"]:
            self.state.setText("Bluetooth is turned off")
        elif connected:
            self.state.setText("Connected to " + ", ".join(connected))
        else:
            self.state.setText("No device connected")

        self.table.setRowCount(len(r["devs"]))
        for i, d in enumerate(r["devs"]):
            label = ("Connected" if d["connected"]
                     else "Paired" if d["paired"] else "Available")
            color = (ACCENT_GLOW if d["connected"]
                     else ACCENT_LIGHT if d["paired"] else MUTED)
            self.table.setItem(i, 0, table_item(d["name"], key=d))
            self.table.setItem(i, 1, table_item(d["mac"], MUTED))
            self.table.setItem(i, 2, table_item(label, color))
        self.msg.setText(r["error"] or f"{len(r['devs'])} devices")

    def _toggle_power(self, on):
        self.msg.setText("Updating power...")
        run_async(lambda: run_cmd(["bluetoothctl", "power",
                                   "on" if on else "off"], 10),
                  lambda _: self.refresh())

    def _scan(self):
        self.scan_btn.setEnabled(False)
        self.msg.setText("Scanning for 8 seconds...")
        run_async(self._scan_job, self._loaded)

    @classmethod
    def _scan_job(cls):
        run_cmd(["bluetoothctl", "power", "on"], 8)
        run_cmd(["bluetoothctl", "--timeout", "8", "scan", "on"], 16)
        return cls._snapshot()

    def _selected(self):
        dev = selected_key(self.table)
        if not dev:
            self.msg.setText("Select a device first.")
        return dev

    def _simple(self, verb, progress, timeout):
        dev = self._selected()
        if not dev:
            return
        self.msg.setText(f"{progress} {dev['name']}...")
        run_async(lambda: run_cmd(["bluetoothctl", verb, dev["mac"]], timeout),
                  self._acted)

    def _pair(self):
        dev = self._selected()
        if not dev:
            return
        self.msg.setText(f"Pairing with {dev['name']}...")
        run_async(lambda: self._pair_job(dev["mac"]), self._acted)

    @staticmethod
    def _pair_job(mac):
        rc, out, err = run_cmd(["bluetoothctl", "--agent", "NoInputNoOutput",
                                "pair", mac], 40)
        if rc == 0:
            run_cmd(["bluetoothctl", "trust", mac], 8)
        return rc, out, err

    def _acted(self, r):
        if isinstance(r, Exception):
            self.msg.setText(str(r))
            return
        rc, out, err = r
        text = last_line(out, err) if rc == 0 else last_line(err, out)
        run_async(self._snapshot, lambda snap: (self._loaded(snap),
                                                self.msg.setText(text)))


class UtilsPage(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        self.power = PowerBar()
        lay.addWidget(self.power)

        nav = QHBoxLayout()
        nav.setSpacing(6)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.pages = [LogsPage(), PartitionsPage(), WifiPage(), BluetoothPage()]
        self.stack = QStackedWidget()
        for i, (label, page) in enumerate(zip(
                ("System logs", "Partitions", "Wi-Fi", "Bluetooth"),
                self.pages)):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(i == 0)
            self.group.addButton(b, i)
            nav.addWidget(b)
            self.stack.addWidget(page)
        nav.addStretch(1)
        lay.addLayout(nav)
        lay.addWidget(self.stack, 1)
        self.group.idClicked.connect(self._select)

    def _select(self, i):
        self.stack.setCurrentIndex(i)
        self.pages[i].refresh()

    def activate(self):
        self.pages[self.stack.currentIndex()].refresh()


class Analyzer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pulse")
        self.resize(1100, 760)

        self.gpu = GpuSource()

        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 18, 22, 14)
        outer.setSpacing(14)

        head = QHBoxLayout()
        title = QLabel("Pulse")
        title.setObjectName("app")
        self.cpu_info = (f"{psutil.cpu_count(logical=False) or '?'} cores, "
                         f"{psutil.cpu_count()} threads")
        self.host = QLabel(self.cpu_info)
        self.host.setObjectName("status")

        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        nav_row = QHBoxLayout()
        nav_row.setSpacing(6)
        for i, label in enumerate(("Overview", "GPU", "Utils")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(i == 0)
            self.nav.addButton(b, i)
            nav_row.addWidget(b)
        self.nav.idClicked.connect(self._show_page)

        self.interval = QComboBox()
        for label, ms in (("0.5 s", 500), ("1 s", 1000), ("2 s", 2000)):
            self.interval.addItem(label, ms)
        self.interval.setCurrentIndex(1)
        self.interval.currentIndexChanged.connect(self._set_interval)
        self.pause = QPushButton("Pause")
        self.pause.setCheckable(True)
        self.pause.toggled.connect(
            lambda on: self.pause.setText("Resume" if on else "Pause"))

        head.addWidget(title)
        head.addSpacing(12)
        head.addWidget(self.host)
        head.addSpacing(18)
        head.addLayout(nav_row)
        head.addStretch(1)
        head.addWidget(QLabel("Refresh"))
        head.addWidget(self.interval)
        head.addWidget(self.pause)
        outer.addLayout(head)

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)

        overview = QWidget()
        ov = QVBoxLayout(overview)
        ov.setContentsMargins(0, 0, 0, 0)
        ov.setSpacing(14)

        self.cpu = Card("Processor", [ACCENT], fixed_max=100)
        self.mem = Card("Memory", [ACCENT_LIGHT], fixed_max=100)
        self.disk = Card("Disk", [ACCENT, ACCENT_GLOW], min_max=1 << 20)
        self.net = Card("Network", [ACCENT, ACCENT_GLOW], min_max=1 << 14)
        grid = QGridLayout()
        grid.setSpacing(14)
        for i, c in enumerate((self.cpu, self.mem, self.disk, self.net)):
            grid.addWidget(c, i // 2, i % 2)
        ov.addLayout(grid, 5)

        bottom = QHBoxLayout()
        bottom.setSpacing(14)

        cores_card = QFrame(objectName="card")
        cl = QVBoxLayout(cores_card)
        cl.setContentsMargins(18, 14, 18, 14)
        ct = QLabel("Load per thread")
        ct.setObjectName("title")
        self.cores = CoreBars()
        cl.addWidget(ct)
        cl.addWidget(self.cores, 1)

        proc_card = QFrame(objectName="card")
        pl = QVBoxLayout(proc_card)
        pl.setContentsMargins(12, 12, 12, 8)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Process", "PID", "CPU", "Memory"])
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2, 3):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        for col in range(4):
            hi = self.table.horizontalHeaderItem(col)
            hi.setTextAlignment((Qt.AlignLeft if col == 0 else Qt.AlignRight)
                                | Qt.AlignVCenter)
        pl.addWidget(self.table)

        bottom.addWidget(cores_card, 5)
        bottom.addWidget(proc_card, 6)
        ov.addLayout(bottom, 5)

        self.gpu_page = GpuPage(self.gpu)
        self.utils = UtilsPage()
        self.stack.addWidget(overview)
        self.stack.addWidget(self.gpu_page)
        self.stack.addWidget(self.utils)

        self.status = QLabel(" ")
        self.status.setObjectName("status")
        outer.addWidget(self.status)

        self._last_t = time.monotonic()
        self._disk = psutil.disk_io_counters()
        self._net = psutil.net_io_counters()
        self._tick_n = 0
        psutil.cpu_percent(None, percpu=True)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.tick()

    def _show_page(self, index):
        self.stack.setCurrentIndex(index)
        self.host.setText((self.cpu_info, self.gpu.name,
                           socket.gethostname())[index])
        if index == 2:
            self.utils.activate()

    def _set_interval(self):
        self.timer.setInterval(self.interval.currentData())

    def tick(self):
        if self.pause.isChecked():
            return
        now = time.monotonic()
        dt = max(now - self._last_t, 1e-3)
        self._last_t = now

        per = psutil.cpu_percent(None, percpu=True)
        total = sum(per) / len(per)
        self.cpu.value.setText(f"{total:.0f}%")
        extra = []
        freq = psutil.cpu_freq()
        if freq:
            extra.append(f"{freq.current / 1000:.2f} GHz")
        temp = self._cpu_temp()
        if temp is not None:
            extra.append(f"{temp:.0f} °C")
        self.cpu.sub.setText("  ".join(extra) or f"{len(per)} threads")
        self.cpu.chart.push(total)
        self.cores.set_values(per)

        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        self.mem.value.setText(f"{vm.percent:.0f}%")
        self.mem.sub.setText(
            f"{fmt_bytes(vm.used)} of {fmt_bytes(vm.total)}   "
            f"swap {fmt_bytes(sw.used)}")
        self.mem.chart.push(vm.percent)

        d = psutil.disk_io_counters()
        if d and self._disk:
            r = (d.read_bytes - self._disk.read_bytes) / dt
            w = (d.write_bytes - self._disk.write_bytes) / dt
            self.disk.value.setText(f"{fmt_rate(r + w)}")
            self.disk.sub.setText(f"read {fmt_rate(r)}   write {fmt_rate(w)}")
            self.disk.chart.push(r, w)
        self._disk = d

        n = psutil.net_io_counters()
        down = (n.bytes_recv - self._net.bytes_recv) / dt
        up = (n.bytes_sent - self._net.bytes_sent) / dt
        self.net.value.setText(fmt_rate(down + up))
        self.net.sub.setText(f"down {fmt_rate(down)}   up {fmt_rate(up)}")
        self.net.chart.push(down, up)
        self._net = n

        if self._tick_n % 2 == 0:
            self._refresh_processes()
        self._tick_n += 1

        self.gpu_page.update_stats(self.gpu.sample())

        self.status.setText(
            f"{len(psutil.pids())} processes   "
            f"uptime {self._uptime()}   updated {time.strftime('%H:%M:%S')}")

    @staticmethod
    def _cpu_temp():
        try:
            temps = psutil.sensors_temperatures()
        except (AttributeError, OSError):
            return None
        for key in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            if key in temps and temps[key]:
                return temps[key][0].current
        return None

    @staticmethod
    def _uptime():
        s = int(time.time() - psutil.boot_time())
        d, s = divmod(s, 86400)
        h, s = divmod(s, 3600)
        m = s // 60
        return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"

    def _refresh_processes(self):
        ncpu = psutil.cpu_count() or 1
        rows = []
        for p in psutil.process_iter():
            try:
                with p.oneshot():
                    rows.append((p.name(), p.pid,
                                 p.cpu_percent(None) / ncpu,
                                 p.memory_info().rss))
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                continue
        rows.sort(key=lambda r: (r[2], r[3]), reverse=True)
        rows = rows[:30]

        self.table.setRowCount(len(rows))
        for i, (name, pid, cpu, mem) in enumerate(rows):
            cells = (name, str(pid), f"{cpu:.1f}%", fmt_bytes(mem))
            for j, text in enumerate(cells):
                item = self.table.item(i, j)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(i, j, item)
                item.setText(text)
                if j:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if j == 2:
                    item.setForeground(
                        QColor(ACCENT_GLOW if cpu >= 30 else ACCENT_LIGHT))
                else:
                    item.setForeground(QColor(TEXT if j == 0 else MUTED))


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    for ttf in (Path(__file__).parent / "fonts").glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(ttf))
    f = app.font()
    f.setFamilies(["JetBrains Mono", "JetBrainsMono Nerd Font",
                   "DejaVu Sans Mono", "Monospace"])
    f.setPixelSize(12)
    app.setFont(f)
    app.setStyleSheet(QSS)
    w = Analyzer()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

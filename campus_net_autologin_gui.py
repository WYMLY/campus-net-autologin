#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
校园网自动认证 —— 图形界面版
================================
给 campus_net_autologin.py 套一个 Windows 图形界面，双击就能用，不用敲命令。

功能：
  - 填门户地址 / ac_id / 账号 / 密码 / 校园 WiFi 名 / 轮询间隔
  - 保存配置 -> 写同目录 config.json（命令行版也读这个文件）
  - 检测一次 -> 跑一轮判断，不启动常驻
  - 开始监控 -> 后台线程每 N 秒检测，掉线自动重连；点“停止”退出循环
  - 实时日志：控制台输出同时显示在窗口里，成功/失败按颜色区分

依赖：只用 Python 标准库（tkinter），不需要 pip 安装任何东西。
运行：双击本文件，或 python campus_net_autologin_gui.py
"""
import os
import sys
import json
import time
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import messagebox

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import campus_net_autologin as core

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# ---------------- 配色 ----------------
BG = "#12151c"          # 窗口底
CARD = "#1b2029"        # 卡片
CARD_HI = "#232a35"     # 卡片内次级块 / 按钮
INPUT = "#161a22"       # 输入框
BORDER = "#2c3441"
BORDER_FOCUS = "#4c8dff"
TEXT = "#e6e9ef"
TEXT_DIM = "#8b95a7"
TEXT_FAINT = "#5c6474"
ACCENT = "#4c8dff"
ACCENT_HOVER = "#6a9fff"
ACCENT_DOWN = "#3a76e0"
OK = "#3ddc84"
WARN = "#f0b429"
ERR = "#ff6b6b"
MONO_BG = "#0e1116"

F_TITLE = ("Microsoft YaHei UI", 17, "bold")
F_SUB = ("Microsoft YaHei UI", 9)
F_LABEL = ("Microsoft YaHei UI", 9)
F_BODY = ("Microsoft YaHei UI", 10)
F_BTN = ("Microsoft YaHei UI", 10)
F_MONO = ("Consolas", 9)


def _measure(text, font):
    try:
        return tkfont.Font(font=font).measure(text)
    except Exception:
        return max(40, len(text) * 9)


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画圆角矩形（smooth 多边形近似，够圆）"""
    r = max(1, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [
        x1 + r, y1, x2 - r, y1, x2, y1,
        x2, y1 + r, x2, y2 - r, x2, y2,
        x2 - r, y2, x1 + r, y2, x1, y2,
        x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(pts, smooth=True, splinesteps=18, **kw)


class RoundedEntry(tk.Canvas):
    """圆角输入框：Canvas 画框 + 内嵌 Entry，聚焦时描边变色，带灰色占位提示"""

    def __init__(self, master, textvariable, height=36, radius=9, show="",
                 bg=CARD, placeholder="", eye=False):
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0)
        self._r = radius
        self._h = height
        self._ph = placeholder
        self._shape = None
        self._focused = False
        self._eye_on = False
        self._secret = show
        self.entry = tk.Entry(self, textvariable=textvariable, font=F_BODY,
                              bg=INPUT, fg=TEXT, insertbackground=ACCENT,
                              relief="flat", bd=0, highlightthickness=0,
                              selectbackground="#2f4f8f", selectforeground=TEXT,
                              show=show)
        self.entry.place(x=12, y=(height - 22) // 2, relwidth=1,
                         width=-(24 + 30) if eye else -24, height=22)
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", lambda e: self.entry.focus_set())
        self._var = textvariable

        # 密码框右侧的小眼睛：点一下明文 / 点一下圆点
        self._eye = None
        if eye:
            self._eye = tk.Canvas(self, width=26, height=height - 8, bg=INPUT,
                                  highlightthickness=0, bd=0, cursor="hand2")
            self._eye.place(relx=1.0, x=-32, y=4, width=26, height=height - 8)
            self._eye.bind("<Button-1>", lambda e: self.toggle_secret())
            self._paint_eye()

        if placeholder:
            self._ph_label = tk.Label(self, text=placeholder, font=F_BODY, bg=INPUT,
                                      fg="#57607a", bd=0, padx=0, pady=0)
            self._var.trace_add("write", lambda *a: self._sync_placeholder())
        self._draw()
        self._sync_placeholder()

    def toggle_secret(self):
        """明文 / 密文切换"""
        self._eye_on = not self._eye_on
        self.entry.configure(show="" if self._eye_on else self._secret)
        self._paint_eye()

    def _paint_eye(self):
        if not self._eye:
            return
        c = self._eye
        c.delete("all")
        w = 26
        h = self._h - 8
        col = ACCENT if self._eye_on else TEXT_FAINT
        cy = h / 2.0
        c.create_oval(4, cy - 6, w - 4, cy + 6, outline=col, width=1.5)
        c.create_oval(w / 2 - 2.5, cy - 2.5, w / 2 + 2.5, cy + 2.5,
                      fill=col, outline=col)
        if not self._eye_on:
            # 打一条斜杠表示“现在看不见”
            c.create_line(5, h - 5, w - 5, 5, fill=col, width=1.5)

    def _draw(self):
        w = self.winfo_width()
        h = self.winfo_height()
        if w <= 1 or h <= 1:
            return
        self.delete("shape")
        self._shape = round_rect(self, 1, 1, w - 1, h - 1, self._r,
                                 fill=INPUT, outline=BORDER, width=1, tags="shape")
        self.entry.lift()
        self._sync_placeholder()

    def _sync_placeholder(self):
        if not self._ph:
            return
        # 读变量而不是 entry.get()：trace 回调可能早于 Entry 刷新显示
        try:
            empty = not self._var.get()
        except Exception:
            empty = not self.entry.get()
        if empty and not self._focused:
            self._ph_label.place(x=13, y=(self.winfo_height() - 18) // 2)
            self._ph_label.lift()
        else:
            self._ph_label.place_forget()

    def _on_focus_in(self, _):
        self._focused = True
        self._ring(True)
        self._sync_placeholder()

    def _on_focus_out(self, _):
        self._focused = False
        self._ring(False)
        self._sync_placeholder()

    def _ring(self, focused):
        if self._shape:
            self.itemconfigure(self._shape,
                               outline=BORDER_FOCUS if focused else BORDER,
                               width=1)

    def set_show(self, show):
        self.entry.configure(show=show)

    def set_state(self, state):
        self.entry.configure(state=state)


class RoundedButton(tk.Canvas):
    """圆角按钮，四种样式：primary / secondary / ghost / danger"""

    STYLES = {
        "primary":   dict(fill=ACCENT, text="#ffffff", hover=ACCENT_HOVER, press=ACCENT_DOWN, outline=""),
        "secondary": dict(fill=CARD_HI, text=TEXT, hover="#2b3442", press="#1e2530", outline=BORDER),
        "ghost":     dict(fill=CARD, text=TEXT_DIM, hover=CARD_HI, press="#1e2530", outline=""),
        "danger":    dict(fill=CARD, text=ERR, hover="#3a2226", press="#2a1b1e", outline="#5a2b2f"),
    }

    def __init__(self, master, text, command=None, kind="secondary",
                 width=None, height=36, bg=CARD, font=F_BTN):
        self._text = text
        self._font = font
        w = width or (_measure(text, font) + 34)
        super().__init__(master, width=w, height=height, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._kind = kind
        self._cmd = command
        self._enabled = True
        self._hovering = False
        self._pressing = False
        self._id = round_rect(self, 1, 1, w - 1, height - 1, 9,
                              fill=self.STYLES[kind]["fill"],
                              outline=self.STYLES[kind]["outline"] or "",
                              width=1)
        self._label = self.create_text(w / 2, height / 2, text=text, font=font,
                                       fill=self.STYLES[kind]["text"])
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _paint(self):
        s = self.STYLES[self._kind]
        if not self._enabled:
            fill, fg, outline = CARD_HI, TEXT_FAINT, ""
        elif self._pressing:
            fill, fg, outline = s["press"], s["text"], s["outline"]
        elif self._hovering:
            fill, fg, outline = s["hover"], s["text"], s["outline"]
        else:
            fill, fg, outline = s["fill"], s["text"], s["outline"]
        self.itemconfigure(self._id, fill=fill,
                           outline=outline or fill)
        self.itemconfigure(self._label, fill=fg)

    def _on_enter(self, _):
        self._hovering = True
        self._paint()

    def _on_leave(self, _):
        self._hovering = False
        self._pressing = False
        self._paint()

    def _on_press(self, _):
        if not self._enabled:
            return
        self._pressing = True
        self._paint()

    def _on_release(self, _):
        if not self._enabled:
            return
        was = self._pressing
        self._pressing = False
        self._paint()
        if was and self._cmd:
            self._cmd()

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._paint()

    def set_text(self, text):
        self.itemconfigure(self._label, text=text)



class SlimScroll(tk.Canvas):
    """细长自定义滚动条（原生 Scrollbar 在深色界面里是白的，很难看）"""

    def __init__(self, master, target, bg=MONO_BG, width=10):
        super().__init__(master, width=width, bg=bg, highlightthickness=0, bd=0)
        self.target = target
        self._first, self._last = 0.0, 1.0
        self._drag_y = None
        self._hover = False
        self._thumb = None
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Enter>", lambda e: setattr(self, "_hover", True) or self._draw())
        self.bind("<Leave>", lambda e: setattr(self, "_hover", False) or self._draw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", lambda e: setattr(self, "_drag_y", None))

    def set(self, first, last):
        self._first, self._last = float(first), float(last)
        self._draw()

    def _draw(self):
        self.delete("all")
        self._thumb = None
        h, w = self.winfo_height(), self.winfo_width()
        if h <= 1:
            return
        if self._last - self._first >= 0.999:
            return
        top = 2 + (h - 4) * self._first
        bottom = 2 + (h - 4) * self._last
        if bottom - top < 26:
            mid = (top + bottom) / 2
            top, bottom = mid - 13, mid + 13
            top = max(2, top)
            bottom = min(h - 2, bottom)
        color = "#3c4a63" if self._hover else "#2b3442"
        self._thumb = round_rect(self, 3, top, w - 3, bottom, 4, fill=color, outline="")

    def _press(self, e):
        h = self.winfo_height()
        if self._thumb:
            c = self.coords(self._thumb)
            if c and c[1] <= e.y <= c[5]:
                self._drag_y = e.y
                return
        frac = max(0.0, min(1.0, (e.y - 2) / max(1, h - 4)))
        self.target.yview_moveto(frac)

    def _drag(self, e):
        if self._drag_y is None:
            return
        h = self.winfo_height()
        delta = (e.y - self._drag_y) / max(1, h - 4)
        self._drag_y = e.y
        self.target.yview_moveto(self._first + delta)


class StatusPill(tk.Canvas):
    """右上角状态胶囊：小圆点 + 文字（宽度跟着文字走）"""

    def __init__(self, master, bg=BG):
        super().__init__(master, width=140, height=28, bg=bg,
                         highlightthickness=0, bd=0)
        self._text = "未运行"
        self._color = TEXT_DIM
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, text, color=None):
        self._text = text
        if color:
            self._color = color
        need = _measure(text, F_LABEL) + 40
        if int(self["width"]) != need:
            self.configure(width=need)
        self._draw()

    def _draw(self):
        self.delete("all")
        w = max(self.winfo_width(), 10)
        round_rect(self, 1, 1, w - 1, 27, 13, fill=CARD, outline=BORDER, width=1)
        self.create_oval(12, 11, 20, 19, fill=self._color, outline="")
        self.create_text(28, 14, text=self._text, font=F_LABEL,
                         fill=self._color, anchor="w")


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("校园网自动认证")
        self.root.configure(bg=BG)
        self.root.geometry("780x720")
        self.root.minsize(720, 640)

        self.worker = None
        self.stop_event = threading.Event()
        self.log_queue = queue.Queue()
        self.ui_queue = queue.Queue()
        self._buttons = []

        self._build_ui()
        self._load_config_into_ui()
        core.LOG_SINK = self._sink_log
        self._append("准备就绪：填好上面的配置，点「开始监控」就能挂后台跑了", "dim")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(80, self._pump_ui_queue)

    # ---------------- 界面骨架 ----------------
    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG)
        header.pack(fill="x", padx=22, pady=(18, 12))
        left = tk.Frame(header, bg=BG)
        left.pack(side="left", anchor="w")
        tk.Label(left, text="校园网自动认证", font=F_TITLE, bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(left, text="深澜 SRUN v4 · 连上校园网就自动登录，掉线自动重连",
                 font=F_SUB, bg=BG, fg=TEXT_FAINT).pack(anchor="w", pady=(4, 0))
        self.pill = StatusPill(header, bg=BG)
        self.pill.pack(side="right", anchor="e", pady=(6, 0))
        self.pill.set("未运行", TEXT_DIM)

        # ---- 配置卡片 ----
        card = tk.Frame(self.root, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        card.pack(fill="x", padx=18, pady=(0, 10))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="x", padx=18, pady=(14, 16))
        inner.columnconfigure(0, weight=1, uniform="c")
        inner.columnconfigure(1, weight=1, uniform="c")

        self.var_host = tk.StringVar()
        self.var_acid = tk.StringVar()
        self.var_user = tk.StringVar()
        self.var_pass = tk.StringVar()
        self.var_ssid = tk.StringVar()
        self.var_interval = tk.StringVar()

        self.e_host = self._field(inner, "门户地址", self.var_host, row=0, col=0, span=2,
                                  ph="http://10.100.0.1",
                                  hint="怎么填：浏览器打开校园网登录页，地址栏里 ?ac_id 前面那段就是它")
        self.e_user = self._field(inner, "账号 / 学号", self.var_user, row=1, col=0,
                                  ph="学号或上网账号")
        self.e_pass = self._field(inner, "密码", self.var_pass, row=1, col=1,
                                  show="*", ph="校园网密码", eye=True)
        self.e_acid = self._field(inner, "ac_id", self.var_acid, row=2, col=0, ph="2")
        self.e_interval = self._field(inner, "轮询间隔（秒）", self.var_interval, row=2, col=1,
                                      ph="60")
        self.e_ssid = self._field(inner, "校园 WiFi 名称（可留空）", self.var_ssid,
                                  row=3, col=0, span=2, ph="例如 Campus-WiFi",
                                  hint="只在连上这个 WiFi 时才认证，回家连热点不会瞎登录")

        # ---- 按钮 ----
        bar = tk.Frame(self.root, bg=BG)
        bar.pack(fill="x", padx=18, pady=(0, 10))
        self.btn_start = RoundedButton(bar, "开始监控", command=self.on_start,
                                      kind="primary", bg=BG, height=38)
        self.btn_start.pack(side="left")
        self.btn_once = RoundedButton(bar, "检测一次", command=self.on_check_once,
                                     kind="secondary", bg=BG, height=38)
        self.btn_once.pack(side="left", padx=8)
        self.btn_save = RoundedButton(bar, "保存配置", command=self.on_save,
                                     kind="ghost", bg=BG, height=38)
        self.btn_save.pack(side="left")
        self.btn_stop = RoundedButton(bar, "停止", command=self.on_stop,
                                     kind="danger", bg=BG, height=38, width=88)
        self.btn_stop.pack(side="right")
        self._buttons = [self.btn_start, self.btn_once, self.btn_save, self.btn_stop]
        self.btn_stop.set_enabled(False)

        self.path_var = tk.StringVar(value="配置文件：{}".format(CONFIG_FILE))
        foot = tk.Label(self.root, textvariable=self.path_var, font=("Microsoft YaHei UI", 8),
                        bg=BG, fg="#4a5261", anchor="w")
        foot.pack(side="bottom", fill="x", padx=22, pady=(0, 12))

        # ---- 日志卡片 ----
        logcard = tk.Frame(self.root, bg=CARD, highlightbackground=BORDER,
                           highlightthickness=1)
        logcard.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        head = tk.Frame(logcard, bg=CARD)
        head.pack(fill="x", padx=16, pady=(12, 6))
        tk.Label(head, text="运行日志", font=("Microsoft YaHei UI", 10, "bold"),
                 bg=CARD, fg=TEXT).pack(side="left")
        self.link_clear = tk.Label(head, text="清空", font=F_SUB, bg=CARD,
                                   fg=TEXT_FAINT, cursor="hand2")
        self.link_clear.pack(side="right")
        self.link_clear.bind("<Button-1>", lambda e: self._clear_log())
        self.link_clear.bind("<Enter>", lambda e: self.link_clear.configure(fg=ACCENT))
        self.link_clear.bind("<Leave>", lambda e: self.link_clear.configure(fg=TEXT_FAINT))

        body = tk.Frame(logcard, bg=MONO_BG, highlightbackground=BORDER,
                        highlightthickness=1)
        body.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self.text = tk.Text(body, font=F_MONO, wrap="word", bg=MONO_BG, fg="#c3cad6",
                            relief="flat", bd=0, highlightthickness=0,
                            insertbackground=ACCENT, padx=10, pady=8,
                            selectbackground="#2f4f8f", height=12)
        self.scroll = SlimScroll(body, None, bg=MONO_BG)
        self.text.configure(yscrollcommand=self.scroll.set)
        self.scroll.target = self.text
        self.text.pack(side="left", fill="both", expand=True)
        self.scroll.pack(side="right", fill="y", pady=6, padx=(0, 4))
        self.text.tag_config("ok", foreground=OK)
        self.text.tag_config("err", foreground=ERR)
        self.text.tag_config("warn", foreground=WARN)
        self.text.tag_config("dim", foreground=TEXT_FAINT)
        self.text.tag_config("ts", foreground="#5b6a80")
        self.text.configure(state="disabled")

    def _field(self, parent, label, var, row, col, span=1, show="", ph="",
               hint="", eye=False):
        holder = tk.Frame(parent, bg=CARD)
        holder.grid(row=row, column=col, columnspan=span, sticky="ew",
                    padx=(0, 12) if col == 0 and span == 1 else (0, 0),
                    pady=(0, 10))
        tk.Label(holder, text=label, font=F_LABEL, bg=CARD, fg=TEXT_DIM).pack(anchor="w")
        ent = RoundedEntry(holder, var, show=show, bg=CARD, placeholder=ph, eye=eye)
        ent.pack(fill="x", pady=(5, 0))
        if hint:
            tk.Label(holder, text=hint, font=F_SUB, bg=CARD, fg=TEXT_FAINT,
                     anchor="w", justify="left").pack(anchor="w", pady=(4, 0))
        return ent

    # ---------------- 配置读写 ----------------
    def _load_config_into_ui(self):
        cfg = dict(core.DEFAULT_CONFIG)
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                    user_cfg = json.load(f)
                if isinstance(user_cfg, dict):
                    cfg.update({k: v for k, v in user_cfg.items() if k in cfg})
            except Exception as e:
                self._append("读取 config.json 失败：{}".format(e), "err")
        self.var_host.set(str(cfg.get("portal_host", "")))
        self.var_acid.set(str(cfg.get("ac_id", "2")))
        self.var_user.set(str(cfg.get("username", "")))
        self.var_pass.set(str(cfg.get("password", "")))
        self.var_ssid.set(str(cfg.get("target_wifi_ssid", "")))
        self.var_interval.set(str(cfg.get("check_interval", 60)))
        core.apply_config(cfg)

    def _collect_config(self):
        try:
            interval = int(self.var_interval.get().strip() or 60)
        except ValueError:
            raise ValueError("轮询间隔必须是整数秒")
        interval = max(10, interval)
        host = self.var_host.get().strip().rstrip("/")
        if not host.startswith("http"):
            raise ValueError("门户地址要以 http:// 或 https:// 开头")
        return {
            "portal_host": host,
            "ac_id": self.var_acid.get().strip() or "2",
            "username": self.var_user.get().strip(),
            "password": self.var_pass.get(),
            "target_wifi_ssid": self.var_ssid.get().strip(),
            "check_interval": interval,
            "timeout": core.DEFAULT_CONFIG["timeout"],
            "n": core.DEFAULT_CONFIG["n"],
            "type": core.DEFAULT_CONFIG["type"],
        }

    def _save_config(self, quiet=False):
        try:
            cfg = self._collect_config()
        except ValueError as e:
            messagebox.showwarning("配置有问题", str(e), parent=self.root)
            return None
        if not cfg["username"] or not cfg["password"]:
            messagebox.showwarning("配置不完整", "账号和密码都要填，不然没法认证。",
                                   parent=self.root)
            return None
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("保存失败", "写 config.json 出错：{}".format(e),
                                 parent=self.root)
            return None
        core.apply_config(cfg)
        if not quiet:
            self._append("配置已保存到 config.json", "ok")
        return cfg

    # ---------------- 按钮 ----------------
    def on_save(self):
        if self._save_config():
            self.pill.set("配置已保存", OK)

    def on_check_once(self):
        cfg = self._save_config(quiet=True)
        if not cfg:
            return
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("正在跑", "监控已经在跑了，先停一下再单独检测。",
                                parent=self.root)
            return
        self._append("── 开始单次检测 ──", "dim")
        self.pill.set("检测中…", WARN)
        self._set_buttons(running=True)
        self.worker = threading.Thread(target=self._run_once, daemon=True)
        self.worker.start()

    def on_start(self):
        cfg = self._save_config(quiet=True)
        if not cfg:
            return
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()
        self._append("── 开始常驻监控，间隔 {} 秒 ──".format(cfg["check_interval"]), "dim")
        self.pill.set("监控中…", WARN)
        self._set_buttons(running=True)
        self.worker = threading.Thread(target=self._run_loop, daemon=True)
        self.worker.start()

    def on_stop(self):
        self.stop_event.set()
        self._append("── 正在停止监控 ──", "warn")
        self.pill.set("正在停止…", TEXT_DIM)

    def _on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("还在监控", "监控还在跑，关掉窗口就停了。确定关？",
                                      parent=self.root):
                return
            self.stop_event.set()
            time.sleep(0.3)
        self.root.destroy()

    # ---------------- 后台线程（只往队列塞事件，绝不直接碰 Tk） ----------------
    # 线程里直接 root.after() 会抛 RuntimeError: main thread is not in main loop
    def _post(self, *item):
        self.ui_queue.put(item)

    def _run_once(self):
        try:
            ok, action = core.ensure_online()
            self._post("log", "[结果] {}".format(action), "ok" if ok else "err")
            self._post("pill", "已认证" if ok else "未认证", OK if ok else ERR)
        except Exception as e:
            self._post("log", "[异常] {}".format(e), "err")
            self._post("pill", "出错了", ERR)
        finally:
            self._post("buttons", False)

    def _run_loop(self):
        while not self.stop_event.is_set():
            try:
                ok, action = core.ensure_online()
                if ok:
                    self._post("log", "[结果] {}".format(action), "ok")
                    self._post("pill", "在线（已认证）", OK)
                else:
                    self._post("log", "[结果] {}".format(action), "warn")
                    self._post("pill", "未认证 · 重试中", ERR)
            except Exception as e:
                self._post("log", "[异常] {}".format(e), "err")
                self._post("pill", "出错了", ERR)

            waited = 0.0
            total = float(core.CHECK_INTERVAL)
            while waited < total and not self.stop_event.is_set():
                time.sleep(0.2)
                waited += 0.2

        self._post("log", "── 监控已停止 ──", "warn")
        self._post("pill", "已停止", TEXT_DIM)
        self._post("buttons", False)

    # ---------------- 界面更新（只在主线程） ----------------
    def _set_buttons(self, running):
        # 主线程也走队列，省得两套路径
        self._post("buttons", running)

    def _apply_buttons(self, running):
        self.btn_start.set_enabled(not running)
        self.btn_once.set_enabled(not running)
        self.btn_save.set_enabled(not running)
        self.btn_stop.set_enabled(running)

    def _sink_log(self, line):
        self._post("log", line, None)

    def _pump_ui_queue(self):
        """主线程定时把队列里的活儿干完"""
        try:
            while True:
                item = self.ui_queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append(item[1], item[2])
                elif kind == "buttons":
                    self._apply_buttons(item[1])
                elif kind == "pill":
                    self.pill.set(item[1], item[2])
        except queue.Empty:
            pass
        except Exception:
            pass
        self.root.after(80, self._pump_ui_queue)

    @staticmethod
    def _line_tag(line):
        if "成功" in line or "已认证" in line or "认证已生效" in line:
            return "ok"
        if "失败" in line or "异常" in line or "error=" in line or "出错" in line:
            return "err"
        if "非目标" in line or "不可达" in line or "跳过" in line:
            return "warn"
        return None

    def _clear_log(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def _append(self, line, tag=None):
        """只在主线程调用"""
        if tag is None:
            tag = self._line_tag(line)
        self.text.configure(state="normal")
        self.text.insert("end", line + "\n", tag or ())
        count = int(self.text.index("end-1c").split(".")[0])
        if count > 800:
            self.text.delete("1.0", "{}.0".format(count - 800))
        self.text.see("end")
        self.text.configure(state="disabled")


def _enable_dpi_awareness():
    """高分屏下窗口不糊"""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _dark_titlebar(root):
    """Windows 10/11：把标题栏也刷成深色，别一块灰一块黑"""
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        # 1) 让整个进程走深色应用模式（Win10 1903+ 的 uxtheme 隐藏接口）
        try:
            uxtheme = ctypes.WinDLL("uxtheme")
            uxtheme[135](2)          # SetPreferredAppMode(ForceDark)
            uxtheme[133](hwnd, True)  # AllowDarkModeForWindow
        except Exception:
            pass
        # 2) 官方接口：DWM 深色标题栏（20 是新值，19 兼容旧版本）
        value = ctypes.c_int(1)
        for attr in (20, 19):
            try:
                if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                        hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                    break
            except Exception:
                continue
        # 3) 逼 DWM 重画非客户区，不然要等下次尺寸变化才变色
        SWP_FLAGS = 0x0002 | 0x0001 | 0x0004 | 0x0020  # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)
    except Exception:
        pass


def _res(name):
    """找资源：源码跑时在同目录，pyinstaller 打包后在 _MEIPASS"""
    base = getattr(sys, "_MEIPASS", BASE_DIR)
    return os.path.join(base, name)


def _set_icon(root):
    """窗口/任务栏图标：ico 给 Windows 标题栏，png 给 iconphoto 兜底"""
    ico = _res("app.ico")
    png = _res("app.png")
    try:
        if os.path.exists(ico):
            root.iconbitmap(default=ico)
    except Exception:
        pass
    try:
        if os.path.exists(png):
            root.iconphoto(True, tk.PhotoImage(file=png))
    except Exception:
        pass


def main():
    _enable_dpi_awareness()
    root = tk.Tk()
    _set_icon(root)
    App(root)
    _dark_titlebar(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""铁行择优 RailPick · 桌面版（tkinter，零新依赖，不用浏览器、不占端口）.

运行：  python desktop.py
冒烟：  python desktop.py --smoke
"""
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from datetime import date, timedelta, datetime

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)
HIST_FILE = os.path.join(BASE, "hist.json")
CONFIG_FILE = os.path.join(BASE, "config.json")
PRESALE_DAYS = 15
INK = "#1B211F"
RAIL = "#0E3B2E"
SIGNAL = "#FF5A1F"
GOLD = "#C9A227"
PAPER = "#F5EFE1"

from backend.search import search_combos  # noqa: E402
from backend import c12306 as api  # noqa: E402
from backend import crawler as crawlmod  # noqa: E402
from backend import graphdb  # noqa: E402
from backend import graphsearch as GSmod  # noqa: E402
from backend.cancel import SearchCancelled  # noqa: E402


def iso_plus(n):
    return (date.today() + timedelta(days=n)).isoformat()


def fmt_dur(m):
    try:
        m = int(m)
    except Exception:
        return "-"
    return f"{m // 60}h{m % 60:02d}m" if m % 60 else f"{m // 60}h"


def fmt_price(p):
    return f"¥{round(p)}" if p is not None else "待查"


class App:
    def __init__(self, root):
        self.root = root
        root.title("铁行择优 RailPick · 桌面版")
        root.geometry("1240x800")
        root.minsize(1024, 640)
        try:  # 高 DPI 下不发虚
            root.tk.call("tk", "scaling", max(1.0, root.winfo_fpixels("1i") / 72.0))
        except Exception:
            pass
        self.combos = []
        self.filtered = []
        self.dots = []  # canvas 散点与方案下标的映射
        self.bench_refs = {}  # 三卡 -> combo 引用
        self.searching = False
        self.job_token = 0  # 取消/新查询时递增，过期结果直接丢弃
        self.search_stop = None  # 本次查询的停止事件，cancel() 里置位
        self.crawling = False
        self.crawl_stop = None
        self.crawl_t0 = 0
        self.t0 = 0
        self.last_status = ""
        self.last_source = "实时"
        self._stations_ready = False
        self._debounce_ids = {}  # 按键防抖：key -> after id
        self._build()
        self.load_config()
        self.btn_go.config(state="disabled")
        self.set_status("正在加载站名库…")
        threading.Thread(target=self._preload, daemon=True).start()

    def _preload(self):
        """启动时后台预热站名库：首字联想/输入校验不再卡 UI 线程."""
        try:
            api.load_stations()
        except Exception:
            pass
        self.root.after(0, self._preload_done)

    def _preload_done(self):
        self._stations_ready = True
        self.btn_go.config(state="normal")
        self.set_status((self.last_status or "就绪") + "｜站名库就绪")

    # ---------- 界面 ----------
    def _build(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        self.topbar1 = ttk.Frame(top)
        self.topbar1.pack(fill="x")
        self.topbar2 = ttk.Frame(top)
        self.topbar2.pack(fill="x", pady=(6, 0))
        top1, top2 = self.topbar1, self.topbar2
        ttk.Label(top1, text="出发").pack(side="left")
        self.e_from = ttk.Entry(top1, width=12)
        self.e_from.insert(0, "北京")
        self.e_from.pack(side="left", padx=4)
        ttk.Button(top1, text="⇄", width=3, command=self.swap).pack(side="left")
        ttk.Label(top1, text="到达").pack(side="left", padx=(6, 0))
        self.e_to = ttk.Entry(top1, width=12)
        self.e_to.insert(0, "上海")
        self.e_to.pack(side="left", padx=4)
        ttk.Label(top1, text="日期").pack(side="left", padx=(6, 0))
        self.e_date = ttk.Entry(top1, width=12)
        self.e_date.insert(0, iso_plus(1))
        self.e_date.pack(side="left", padx=4)
        for txt, n in (("今", 0), ("明", 1), ("后", 2)):
            ttk.Button(top1, text=txt, width=3,
                       command=lambda n=n: self.set_date(n)).pack(side="left", padx=1)
        self.btn_go = ttk.Button(top1, text="查询组合", command=self.query)
        self.btn_go.pack(side="left", padx=10)
        self.btn_cancel = ttk.Button(top1, text="取消", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="left")
        ttk.Label(top2, text="换乘").pack(side="left")
        self.cb_mt = ttk.Combobox(top2, width=14, state="readonly",
                                  values=["仅直达", "直达+1次中转", "直达+1~2次中转"])
        self.cb_mt.current(1)
        self.cb_mt.pack(side="left", padx=4)
        ttk.Label(top2, text="指定中转").pack(side="left", padx=(6, 0))
        self.e_mid = ttk.Entry(top2, width=9)
        self.e_mid.pack(side="left", padx=4)
        ttk.Label(top2, text="广度").pack(side="left", padx=(6, 0))
        self.cb_breadth = ttk.Combobox(top2, width=8, state="readonly",
                                       values=["快速", "标准", "地毯式"])
        self.cb_breadth.current(1)
        self.cb_breadth.pack(side="left", padx=4)
        ttk.Label(top2, text="最短换乘").pack(side="left", padx=(6, 0))
        self.e_min = ttk.Entry(top2, width=5)
        self.e_min.insert(0, "20")
        self.e_min.pack(side="left", padx=2)
        ttk.Label(top2, text="最长等待").pack(side="left", padx=(6, 0))
        self.e_max = ttk.Entry(top2, width=5)
        self.e_max.insert(0, "240")
        self.e_max.pack(side="left", padx=2)
        for w in (self.e_from, self.e_to, self.e_date, self.e_mid):
            w.bind("<Return>", lambda e: self.query())
        self._bind_suggest(self.e_from)
        self._bind_suggest(self.e_to)
        self._bind_suggest(self.e_mid)
        self._sug_after = None
        self._sug_pop = None

        self.hist_frame = ttk.Frame(self.root, padding=(8, 0))
        self.hist_frame.pack(fill="x")
        self.render_hist()

        body = ttk.Frame(self.root, padding=8)
        body.pack(fill="both", expand=True)
        left = ttk.LabelFrame(body, text="快速定位需求", padding=8, width=230)
        left.pack(side="left", fill="y", padx=(0, 8))
        left.pack_propagate(False)
        self._build_filters(left)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        bench = ttk.Frame(right)
        bench.pack(fill="x", pady=(0, 6))
        self.b_fast = ttk.Label(bench, text="最快：—", relief="groove", padding=6)
        self.b_fast.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self.b_mid = ttk.Label(bench, text="★中间值：—", relief="groove", padding=6)
        self.b_mid.pack(side="left", fill="x", expand=True, padx=4)
        self.b_cheap = ttk.Label(bench, text="最便宜：—", relief="groove", padding=6)
        self.b_cheap.pack(side="left", fill="x", expand=True, padx=(4, 0))

        ttk.Label(right, text="价格—时间星图（横轴耗时→，纵轴价格→，点越左下越优，点击圆点定位）").pack(anchor="w")
        ttk.Label(right, text="颜色：绿直达 / 橙1次 / 蓝2次 / 紫3次 / 黄4次 / 空心=票价待查",
                  font=("", 8), foreground="#6B6F6A").pack(anchor="w")
        self.cv = tk.Canvas(right, height=210, bg="#101614", highlightthickness=1,
                            highlightbackground="#2b3532")
        self.cv.pack(fill="x", pady=4)
        self.cv.bind("<Button-1>", self.on_dot)
        self.cv.bind("<Motion>", self.on_hover)
        self.cv.bind("<Leave>", lambda e: self.set_status(self.last_status))

        self.notice = ttk.Label(right, text="", foreground="#B7791F", wraplength=900)
        self.notice.pack(anchor="w")
        self.mid_label = ttk.Label(right, text="", foreground="#6B6F6A", wraplength=900,
                                   font=("", 8))
        self.mid_label.pack(anchor="w")

        tools = ttk.Frame(right)
        tools.pack(fill="x", pady=2)
        ttk.Button(tools, text="复制购票清单", command=self.copy_buy).pack(side="left", padx=(0, 6))
        ttk.Button(tools, text="导出全部TXT", command=self.export_txt).pack(side="left", padx=6)
        ttk.Button(tools, text="重置过滤", command=self.reset_filters).pack(side="left", padx=6)
        ttk.Label(tools, text="限次").pack(side="left", padx=(6, 0))
        self.e_crawlmax = ttk.Entry(tools, width=6)
        self.e_crawlmax.insert(0, "1000")
        self.e_crawlmax.pack(side="left", padx=2)
        ttk.Label(tools, text="优先站").pack(side="left", padx=(6, 0))
        self.e_crawfrom = ttk.Entry(tools, width=14)
        self.e_crawfrom.pack(side="left", padx=2)
        self.cb_phase = ttk.Combobox(tools, width=8, state="readonly",
                                     values=["自动", "仅干线", "仅地级"])
        self.cb_phase.current(0)
        self.cb_phase.pack(side="left", padx=4)
        ttk.Button(tools, text="一键爬取", command=self.crawl_db).pack(side="left", padx=6)
        ttk.Button(tools, text="查看图库", command=self.view_graph).pack(side="left", padx=6)

        progf = ttk.Frame(right)
        progf.pack(fill="x", pady=2)
        self.prog_var = tk.IntVar(value=0)
        self.prog = ttk.Progressbar(progf, maximum=100, variable=self.prog_var, mode="determinate")
        self.prog.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.prog_stage = ttk.Label(progf, text="", anchor="e", wraplength=460,
                                    justify="right")
        self.prog_stage.pack(side="right")

        pan = ttk.PanedWindow(right, orient="horizontal")
        pan.pack(fill="both", expand=True, pady=4)
        lf = ttk.Frame(pan)
        self.lb = tk.Listbox(lf, width=52, activestyle="dotbox")
        self.lb.pack(side="left", fill="both", expand=True)
        sb1 = ttk.Scrollbar(lf, orient="vertical", command=self.lb.yview)
        sb1.pack(side="right", fill="y")
        self.lb.config(yscrollcommand=sb1.set)
        self.lb.bind("<<ListboxSelect>>", lambda e: self.show_detail())
        self.lb.bind("<Double-Button-1>", lambda e: self.copy_buy())
        pan.add(lf, weight=1)
        rf = ttk.Frame(pan)
        self.tx = tk.Text(rf, wrap="word", width=60, font=("", 10), spacing1=3, spacing3=3)
        self.tx.pack(side="left", fill="both", expand=True)
        sb2 = ttk.Scrollbar(rf, orient="vertical", command=self.tx.yview)
        sb2.pack(side="right", fill="y")
        self.tx.config(yscrollcommand=sb2.set, state="disabled")
        pan.add(rf, weight=1)

        self.status = ttk.Label(self.root, text="就绪：输入出发/到达/日期后点查询组合｜双击列表复制清单，三卡/星图圆点可定位",
                                relief="sunken",
                                padding=4, anchor="w")
        self.status.pack(fill="x", side="bottom")
        self.last_status = self.status.cget("text")
        self._build_menu()

    def _build_menu(self):
        mb = tk.Menu(self.root)
        mf = tk.Menu(mb, tearoff=0)
        mf.add_command(label="导出全部TXT", command=self.export_txt)
        mf.add_separator()
        mf.add_command(label="退出", command=self.root.destroy)
        mh = tk.Menu(mb, tearoff=0)
        mh.add_command(label="使用说明", command=self.help_text)
        mh.add_command(label="关于数据", command=lambda: messagebox.showinfo(
            "关于数据", "直连 12306 网页版同款接口（零登录只查余票/票价/经停站），不代购不抢票。\n"
            "12306 有 burst 保护：失败会自动重试+读缓存，勿连续狂点，限流等30秒。"))
        mb.add_cascade(label="文件", menu=mf)
        mb.add_cascade(label="帮助", menu=mh)
        self.root.config(menu=mb)

    def help_text(self):
        messagebox.showinfo("使用说明",
            "1. 出发/到达支持站名、拼音、代码，有联想；日期点 今/明/后最快。\n"
            "2. 想走特定中转填“指定中转”（如太原），留空自动推荐顺路站。\n"
            "3. 查一次 20~70 秒，可点取消；三卡是 最快/中间值/最便宜，点击定位。\n"
            "4. 星图越左下越优，悬停看方案，点击定位；双击列表复制购票清单。\n"
            "5. 左侧滑杆+时段+车型+只看经停，拖动即过滤；条件自动记住。\n"
            "6. 拿着清单去 12306 分段买，或走 App“中转/接续换乘”直接出票。\n"
            "7. 图库深挖：先离线枚举再核验，中转≤/核验数/前后天在左侧调，挖小众线用它。\n"
            "8. 一键爬取攒时刻表（约4秒1次），图越全挖得越准，可随时取消。\n"
            "9. 被限流会显示演示数据或缓存，标题带[演示]/[缓存]标记，等30秒重试。")

    def _build_filters(self, p):
        ttk.Label(p, text="搜索模式", font=("", 9, "bold")).pack(anchor="w")
        self.cb_smode = ttk.Combobox(p, state="readonly", width=20,
                                     values=["实时深查", "图库深挖"])
        self.cb_smode.current(0)
        self.cb_smode.pack(anchor="w")
        fr0 = ttk.Frame(p)
        fr0.pack(anchor="w", pady=4)
        ttk.Label(fr0, text="中转≤").pack(side="left")
        self.cb_maxtr = ttk.Combobox(fr0, state="readonly", width=4,
                                     values=["1", "2", "3", "4"])
        self.cb_maxtr.current(1)
        self.cb_maxtr.pack(side="left", padx=2)
        ttk.Label(fr0, text="核验").pack(side="left", padx=(6, 0))
        self.e_vtop = ttk.Entry(fr0, width=5)
        self.e_vtop.insert(0, "40")
        self.e_vtop.pack(side="left", padx=2)
        ttk.Label(fr0, text="枚举").pack(side="left", padx=(6, 0))
        self.e_enum = ttk.Entry(fr0, width=6)
        self.e_enum.insert(0, "300")
        self.e_enum.pack(side="left", padx=2)
        self.v_win = tk.BooleanVar(value=True)
        ttk.Checkbutton(p, text="前后一天也看看", variable=self.v_win).pack(anchor="w")
        ttk.Label(p, text="最高总价").pack(anchor="w", pady=(8, 0))
        self.v_price = tk.IntVar(value=5000)
        self.lb_price = ttk.Label(p, text="不限")
        self.lb_price.pack(anchor="w")
        ttk.Scale(p, from_=50, to=5000, variable=self.v_price,
                  command=lambda v: self.on_slide()).pack(fill="x")
        ttk.Label(p, text="最长耗时").pack(anchor="w", pady=(8, 0))
        self.v_dur = tk.IntVar(value=4320)
        self.lb_dur = ttk.Label(p, text="不限")
        self.lb_dur.pack(anchor="w")
        ttk.Scale(p, from_=120, to=4320, variable=self.v_dur,
                  command=lambda v: self.on_slide()).pack(fill="x")

        ttk.Label(p, text="车型组合", font=("", 9, "bold")).pack(anchor="w", pady=(10, 2))
        self.kind_vars = {}
        for k in ["直达", "全高铁", "高铁+普速", "全普速", "动车系"]:
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(p, text=k, variable=v, command=self.apply_filters).pack(anchor="w")
            self.kind_vars[k] = v
        ttk.Label(p, text="换乘次数", font=("", 9, "bold")).pack(anchor="w", pady=(10, 2))
        self.tr_vars = {}
        for k in ["0", "1", "2", "3", "4"]:
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(p, text="直达" if k == "0" else f"{k}次", variable=v,
                            command=self.apply_filters).pack(anchor="w")
            self.tr_vars[k] = v
        ttk.Label(p, text="出发时段", font=("", 9, "bold")).pack(anchor="w", pady=(10, 2))
        self.dp_vars = {}
        for k, name in [("0-6", "凌晨"), ("6-12", "上午"), ("12-18", "下午"), ("18-24", "晚上")]:
            v = tk.BooleanVar(value=True)
            ttk.Checkbutton(p, text=name, variable=v, command=self.apply_filters).pack(anchor="w")
            self.dp_vars[k] = v
        ttk.Label(p, text="精确时间（HH:MM，空=不限，可过夜如22:00-06:00）", font=("", 8),
                  foreground="#6B6F6A", wraplength=200).pack(anchor="w", pady=(6, 0))
        fr1 = ttk.Frame(p)
        fr1.pack(anchor="w", pady=2)
        ttk.Label(fr1, text="出发").pack(side="left")
        self.e_dep_lo = ttk.Entry(fr1, width=6)
        self.e_dep_lo.pack(side="left", padx=2)
        ttk.Label(fr1, text="-").pack(side="left")
        self.e_dep_hi = ttk.Entry(fr1, width=6)
        self.e_dep_hi.pack(side="left", padx=2)
        fr2 = ttk.Frame(p)
        fr2.pack(anchor="w", pady=2)
        ttk.Label(fr2, text="到达").pack(side="left")
        self.e_arr_lo = ttk.Entry(fr2, width=6)
        self.e_arr_lo.pack(side="left", padx=2)
        ttk.Label(fr2, text="-").pack(side="left")
        self.e_arr_hi = ttk.Entry(fr2, width=6)
        self.e_arr_hi.pack(side="left", padx=2)
        for e in (self.e_dep_lo, self.e_dep_hi, self.e_arr_lo, self.e_arr_hi):
            e.bind("<KeyRelease>", lambda ev: self._schedule_filter())
        self.v_ticket = tk.BooleanVar(value=True)
        ttk.Checkbutton(p, text="只看有票", variable=self.v_ticket,
                        command=self.apply_filters).pack(anchor="w", pady=(10, 0))
        ttk.Label(p, text="只看经停", font=("", 9, "bold")).pack(anchor="w", pady=(10, 2))
        self.e_via = ttk.Entry(p, width=22)
        self.e_via.pack(anchor="w")
        self.e_via.bind("<KeyRelease>", lambda e: self._schedule_filter())
        ttk.Label(p, text="如填太原，只留经太原方案", font=("", 8), foreground="#6B6F6A").pack(anchor="w")
        ttk.Label(p, text="排序", font=("", 9, "bold")).pack(anchor="w", pady=(10, 2))
        self.cb_sort = ttk.Combobox(p, state="readonly", width=20,
                                    values=["智能推荐", "总价最低", "总耗时最短", "换乘等待最短", "出发时间"])
        self.cb_sort.current(0)
        self.cb_sort.pack(anchor="w")
        self.cb_sort.bind("<<ComboboxSelected>>", lambda e: self.apply_filters())

    def _debounce(self, key, ms, fn):
        """按键防抖：ms 内重复触发只执行最后一次（大数据量下打字过滤不卡）."""
        old = self._debounce_ids.pop(key, None)
        if old is not None:
            try:
                self.root.after_cancel(old)
            except Exception:
                pass

        def fire():
            self._debounce_ids.pop(key, None)
            fn()

        self._debounce_ids[key] = self.root.after(ms, fire)

    def _schedule_filter(self, ms=250):
        self._debounce("filter", ms, self.apply_filters)

    # ---------- 历史 ----------
    def load_hist(self):
        try:
            h = json.load(open(HIST_FILE, encoding="utf-8"))
            return [x for x in h if isinstance(x, (list, tuple)) and len(x) == 2]
        except Exception:
            return []

    def render_hist(self):
        for w in self.hist_frame.winfo_children():
            w.destroy()
        h = self.load_hist()
        if not h:
            return
        ttk.Label(self.hist_frame, text="最近查过：").pack(side="left")
        for f, t in h[:6]:
            ttk.Button(self.hist_frame, text=f"{f}→{t}", width=14,
                       command=lambda f=f, t=t: self.hist_go(f, t)).pack(side="left", padx=2)

    def hist_go(self, f, t):
        self.e_from.delete(0, "end"); self.e_from.insert(0, f)
        self.e_to.delete(0, "end"); self.e_to.insert(0, t)
        self.query()

    def save_hist(self, f, t):
        try:
            h = [x for x in self.load_hist() if not (x[0] == f and x[1] == t)]
            json.dump([[f, t]] + h[:5], open(HIST_FILE, "w", encoding="utf-8"),
                      ensure_ascii=False)
        except Exception:
            pass
        self.render_hist()

    # ---------- 查询 ----------
    def set_date(self, n):
        self.e_date.delete(0, "end")
        self.e_date.insert(0, iso_plus(n))

    def swap(self):
        a = self.e_from.get()
        self.e_from.delete(0, "end"); self.e_from.insert(0, self.e_to.get())
        self.e_to.delete(0, "end"); self.e_to.insert(0, a)

    @staticmethod
    def _clamp_gaps(lo, hi):
        """最长等待钳到 1440 分钟（和服务端 le=1440 对齐），返回 (lo, hi, 是否被钳)。"""
        if hi > 1440:
            return lo, 1440, True
        return lo, hi, False

    def cancel(self):
        if self.searching:
            self.job_token += 1  # 后台线程回来发现 token 对不上就丢弃
            if self.search_stop is not None:
                self.search_stop.set()  # 协作退出：不再发起新请求
            self.searching = False
            self.btn_go.config(state="normal", text="查询组合")
            self.btn_cancel.config(state="disabled")
            self.prog_var.set(0)
            self.prog_stage.config(text="已取消")
            self.set_status("已取消：后台请求跑完会自动丢弃，可直接发起新查询")
        elif getattr(self, "crawling", False):
            if getattr(self, "crawl_stop", None) is not None:
                self.crawl_stop.set()
            self.crawling = False
            if not self.searching:
                self.btn_cancel.config(state="disabled")
            self.set_status("正在停止爬取（跑完当前请求就停，已入库的保留）…")

    # ----- 站名联想 -----
    def _bind_suggest(self, entry):
        entry.bind("<KeyRelease>", lambda e: self._on_sug_key(e, entry))
        entry.bind("<FocusOut>", lambda e: self.root.after(150, self._hide_suggest))
        entry.bind("<Escape>", lambda e: self._hide_suggest())

    def _on_sug_key(self, ev, entry):
        if ev.keysym in ("Up", "Down", "Return", "Escape"):
            if self._sug_pop:
                lb = self._sug_pop.lb
                items = lb.size()
                if not items:
                    return
                cur = lb.curselection()[0] if lb.curselection() else -1
                if ev.keysym == "Up":
                    lb.selection_clear(0, "end")
                    lb.selection_set(max(0, cur - 1))
                elif ev.keysym == "Down":
                    lb.selection_clear(0, "end")
                    lb.selection_set(min(items - 1, cur + 1))
                elif ev.keysym == "Return" and cur >= 0:
                    entry.delete(0, "end")
                    entry.insert(0, lb.get(cur).split(" ")[0])
                    self._hide_suggest()
                    return "break"
            return
        if self._sug_after:
            self.root.after_cancel(self._sug_after)
        self._sug_after = self.root.after(
            280, lambda: self._show_suggest(entry))

    def _show_suggest(self, entry):
        q = entry.get().strip()
        self._hide_suggest()
        if not q:
            return
        try:
            ss = api.search_stations(q, 8)
        except Exception:
            return
        if not ss:
            return
        pop = tk.Toplevel(self.root)
        pop.wm_overrideredirect(True)
        pop.wm_attributes("-topmost", True)
        x = entry.winfo_rootx()
        y = entry.winfo_rooty() + entry.winfo_height() + 2
        pop.wm_geometry(f"+{x}+{y}")
        lb = tk.Listbox(pop, width=max(20, entry.winfo_width() // 8), height=min(8, len(ss)))
        lb.pack()
        for s in ss:
            lb.insert("end", f"{s['name']} {s['code']}")
        lb.selection_set(0)

        def pick(_e=None):
            if lb.curselection():
                entry.delete(0, "end")
                entry.insert(0, lb.get(lb.curselection()[0]).split(" ")[0])
            self._hide_suggest()
        lb.bind("<Button-1>", lambda e: self.root.after(50, pick))
        lb.bind("<Double-Button-1>", lambda e: pick())
        pop.lb = lb
        self._sug_pop = pop

    def _hide_suggest(self):
        if self._sug_pop:
            try:
                self._sug_pop.destroy()
            except Exception:
                pass
            self._sug_pop = None

    # ----- 配置记忆 -----
    def save_config(self):
        try:
            cfg = {"from": self.e_from.get(), "to": self.e_to.get(),
                   "date": self.e_date.get(), "mt": self.cb_mt.get(),
                   "mid": self.e_mid.get(), "min": self.e_min.get(),
                   "max": self.e_max.get(), "price": self.v_price.get(),
                   "dur": self.v_dur.get(), "sort": self.cb_sort.get(),
                   "via": self.e_via.get() if hasattr(self, "e_via") else "",
                   "breadth": self.cb_breadth.get() if hasattr(self, "cb_breadth") else "标准",
                   "crawlmax": self.e_crawlmax.get() if hasattr(self, "e_crawlmax") else "1000",
                   "crawfrom": self.e_crawfrom.get() if hasattr(self, "e_crawfrom") else "",
                   "crawphase": self.cb_phase.get() if hasattr(self, "cb_phase") else "自动",
                   "smode": self.cb_smode.get() if hasattr(self, "cb_smode") else "实时深查",
                   "maxtr": self.cb_maxtr.get() if hasattr(self, "cb_maxtr") else "2",
                   "vtop": self.e_vtop.get() if hasattr(self, "e_vtop") else "40",
                   "enum": self.e_enum.get() if hasattr(self, "e_enum") else "300",
                   "win": self.v_win.get() if hasattr(self, "v_win") else True,
                   "dep_lo": self.e_dep_lo.get() if hasattr(self, "e_dep_lo") else "",
                   "dep_hi": self.e_dep_hi.get() if hasattr(self, "e_dep_hi") else "",
                   "arr_lo": self.e_arr_lo.get() if hasattr(self, "e_arr_lo") else "",
                   "arr_hi": self.e_arr_hi.get() if hasattr(self, "e_arr_hi") else "",
                   "ticket": self.v_ticket.get(),
                   "kinds": {k: v.get() for k, v in self.kind_vars.items()},
                   "trs": {k: v.get() for k, v in self.tr_vars.items()},
                   "dps": {k: v.get() for k, v in self.dp_vars.items()}}
            json.dump(cfg, open(CONFIG_FILE, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass

    def load_config(self):
        try:
            cfg = json.load(open(CONFIG_FILE, encoding="utf-8"))
        except Exception:
            return
        try:
            for e, k in ((self.e_from, "from"), (self.e_to, "to"),
                         (self.e_date, "date"), (self.e_mid, "mid"),
                         (self.e_min, "min"), (self.e_max, "max")):
                if cfg.get(k):
                    e.delete(0, "end"); e.insert(0, str(cfg[k]))
            if cfg.get("via") and hasattr(self, "e_via"):
                self.e_via.delete(0, "end"); self.e_via.insert(0, str(cfg["via"]))
            if cfg.get("breadth") in ("快速", "标准", "地毯式") and hasattr(self, "cb_breadth"):
                self.cb_breadth.set(cfg["breadth"])
            if str(cfg.get("crawlmax", "")).isdigit() and hasattr(self, "e_crawlmax"):
                self.e_crawlmax.delete(0, "end"); self.e_crawlmax.insert(0, str(cfg["crawlmax"]))
            if cfg.get("crawfrom") and hasattr(self, "e_crawfrom"):
                self.e_crawfrom.delete(0, "end"); self.e_crawfrom.insert(0, str(cfg["crawfrom"]))
            if cfg.get("crawphase") in ("自动", "仅干线", "仅地级") and hasattr(self, "cb_phase"):
                self.cb_phase.set(cfg["crawphase"])
            if cfg.get("smode") in ("实时深查", "图库深挖") and hasattr(self, "cb_smode"):
                self.cb_smode.set(cfg["smode"])
            if str(cfg.get("maxtr", "")) in ("1", "2", "3", "4") and hasattr(self, "cb_maxtr"):
                self.cb_maxtr.set(cfg["maxtr"])
            if str(cfg.get("vtop", "")).isdigit() and hasattr(self, "e_vtop"):
                self.e_vtop.delete(0, "end"); self.e_vtop.insert(0, str(cfg["vtop"]))
            if str(cfg.get("enum", "")).isdigit() and hasattr(self, "e_enum"):
                self.e_enum.delete(0, "end"); self.e_enum.insert(0, str(cfg["enum"]))
            if "win" in cfg and hasattr(self, "v_win"):
                self.v_win.set(bool(cfg["win"]))
            for e, k in ((self.e_dep_lo, "dep_lo"), (self.e_dep_hi, "dep_hi"),
                         (self.e_arr_lo, "arr_lo"), (self.e_arr_hi, "arr_hi")):
                if hasattr(self, "e_dep_lo") and cfg.get(k):
                    e.delete(0, "end"); e.insert(0, str(cfg[k]))
            if cfg.get("mt") in ("仅直达", "直达+1次中转", "直达+1~2次中转"):
                self.cb_mt.set(cfg["mt"])
            self.v_price.set(int(cfg.get("price", 2000)))
            self.v_dur.set(int(cfg.get("dur", 1800)))
            if cfg.get("sort") in ("智能推荐", "总价最低", "总耗时最短", "换乘等待最短", "出发时间"):
                self.cb_sort.set(cfg["sort"])
            self.v_ticket.set(bool(cfg.get("ticket", True)))
            for k, v in (cfg.get("kinds") or {}).items():
                if k in self.kind_vars:
                    self.kind_vars[k].set(bool(v))
            for k, v in (cfg.get("trs") or {}).items():
                if k in self.tr_vars:
                    self.tr_vars[k].set(bool(v))
            for k, v in (cfg.get("dps") or {}).items():
                if k in self.dp_vars:
                    self.dp_vars[k].set(bool(v))
            self.on_slide()
            self.set_status("已恢复上次查询条件，直接点查询组合即可")
        except Exception:
            pass

    def suggest(self, kw):
        try:
            ss = api.search_stations(kw, 5)
            return "、".join(s["name"] for s in ss)
        except Exception:
            return ""

    def query(self):
        if self.searching:
            return
        if not getattr(self, "_stations_ready", True):
            messagebox.showinfo("提示", "站名库加载中，再等1秒")
            return
        frm, to, dt = self.e_from.get().strip(), self.e_to.get().strip(), self.e_date.get().strip()
        if not frm or not to or not dt:
            messagebox.showwarning("提示", "请填写出发/到达/日期")
            return
        try:
            datetime.strptime(dt, "%Y-%m-%d")
        except Exception:
            messagebox.showwarning("提示", f"日期格式不对，请写成 YYYY-MM-DD（如{iso_plus(1)}），或点 今/明/后 快捷按钮")
            return
        if dt > iso_plus(PRESALE_DAYS):
            messagebox.showwarning("提示", f"12306 预售期{PRESALE_DAYS}天，{dt}还查不到")
            return
        if dt < iso_plus(0):
            messagebox.showwarning("提示", "日期已过去，换个今天或之后的日期吧")
            return
        for kw, role in ((frm, "出发"), (to, "到达")):
            name, code = api.resolve_station(kw)
            if not code:
                messagebox.showwarning("提示",
                                       f"{role}站“{kw}”找不到，你是不是想查：{self.suggest(kw)}")
                return
        try:
            lo, hi = int(self.e_min.get()), int(self.e_max.get())
        except Exception:
            messagebox.showwarning("提示", "换乘时间请填数字")
            return
        if lo < 0 or hi <= 0 or lo > hi:
            messagebox.showwarning("提示", "换乘时间不合理：要求 0≤最短≤最长，已为你对调" if lo > hi > 0 else "换乘时间请填正数")
            if lo > hi > 0:
                lo, hi = hi, lo
                self.e_min.delete(0, "end"); self.e_min.insert(0, str(lo))
                self.e_max.delete(0, "end"); self.e_max.insert(0, str(hi))
            else:
                return
        lo, hi, clamped = self._clamp_gaps(lo, hi)
        if clamped:
            self.e_max.delete(0, "end"); self.e_max.insert(0, str(hi))
        mt = {"仅直达": 0, "直达+1次中转": 1, "直达+1~2次中转": 2}[self.cb_mt.get()]
        breadth = {"快速": "quick", "标准": "normal", "地毯式": "deep"}[self.cb_breadth.get()]
        mid = self.e_mid.get().strip()
        if mid:
            mname, mcode = api.resolve_station(mid)
            if not mcode:
                messagebox.showwarning("提示", f"中转站“{mid}”找不到，你是不是想查：{self.suggest(mid)}")
                return
            if mt == 0:  # 指定了中转站就别再“仅直达”了
                mt = 1
                self.cb_mt.current(1)
        self.save_config()
        self.job_token += 1
        tok = self.job_token
        self.t0 = time.time()
        self.searching = True
        self.search_stop = threading.Event()
        self.btn_go.config(state="disabled", text="查询中…")
        self.btn_cancel.config(state="normal")
        self.notice.config(text="")
        self.prog_var.set(0)
        self.prog_stage.config(text="正在提交…")
        if self.cb_smode.get() == "图库深挖":
            try:
                maxtr = int(self.cb_maxtr.get())
            except Exception:
                maxtr = 2
            try:
                vtop = max(5, min(1000, int((self.e_vtop.get() or "40").strip())))
            except Exception:
                vtop = 40
            try:
                enum = max(10, min(2000, int((self.e_enum.get() or "300").strip())))
            except Exception:
                enum = 300
            win = bool(self.v_win.get())
            th = threading.Thread(target=self._work_graph,
                                  args=(frm, to, dt, maxtr, lo, hi, mid, win, vtop, enum, tok),
                                  kwargs={"should_stop": self.search_stop.is_set},
                                  daemon=True)
        else:
            th = threading.Thread(target=self._work,
                                  args=(frm, to, dt, mt, lo, hi, mid, breadth, tok),
                                  kwargs={"should_stop": self.search_stop.is_set},
                                  daemon=True)
        th.start()

    def _on_prog(self, frac, msg, tok):
        if tok != self.job_token:
            return
        try:
            self.prog_var.set(int(frac * 100))
        except Exception:
            pass
        el = int(time.time() - self.t0) if self.t0 else 0
        self.prog_stage.config(text=f"{msg}（已用{el}秒）")
        self.set_status(f"查询中：{msg}（已用{el}秒，可点取消）")

    def _work_graph(self, frm, to, dt, maxtr, lo, hi, mid, win, vtop, enum=300, tok=0,
                      should_stop=None):
        def cb(frac, msg):
            self.root.after(0, lambda: self._on_prog(frac, msg, tok))
        try:
            r = GSmod.run_graph_search(frm, to, dt, max_transfers=maxtr, min_gap=lo,
                                       max_gap=hi, limit=enum, verify_top=vtop, window=win,
                                       must_middle=mid, on_progress=cb, should_stop=should_stop)
            self.root.after(0, lambda: self._done(frm, to, r, None, tok))
        except SearchCancelled:
            return  # cancel() 已恢复界面，直接静默退出
        except Exception as e:
            # 注意：except 的 e 在块结束会被 Python 删除，lambda 里不能直接 str(e)，
            # 否则回调执行时 NameError，界面永远卡在“查询中”（线上真实卡死过）。
            err = str(e)
            self.root.after(0, lambda: self._done(frm, to, None, err, tok))

    def _work(self, frm, to, dt, mt, lo, hi, mid="", breadth="normal", tok=0,
              should_stop=None):
        def cb(frac, msg):
            self.root.after(0, lambda: self._on_prog(frac, msg, tok))
        try:
            r = search_combos(frm, to, dt, max_transfer=mt, min_gap=lo,
                              max_gap=hi, limit=40, with_price=True, must_middle=mid,
                              breadth=breadth, on_progress=cb, should_stop=should_stop)
            self.root.after(0, lambda: self._done(frm, to, r, None, tok))
        except SearchCancelled:
            return
        except Exception as e:
            err = str(e)  # 同上：先转字符串再进 lambda，否则 NameError 卡死界面
            self.root.after(0, lambda: self._done(frm, to, None, err, tok))

    def _done(self, frm, to, r, err, tok=0):
        if tok != self.job_token:
            return  # 已取消或被新查询取代，直接丢弃
        self.searching = False
        self.btn_go.config(state="normal", text="查询组合")
        self.btn_cancel.config(state="disabled")
        el = int(time.time() - self.t0) if self.t0 else 0
        if err or r is None:
            self.set_status(f"查询失败（用时{el}秒）：{err}，等30秒再试")
            messagebox.showerror("查询失败", f"{err}\n等30秒点查询组合重试")
            return
        self.combos = r.get("combos", [])
        self.flags = {k: r.get(k) for k in ("mock", "cached", "stale", "warn")}
        if not self.combos and r.get("error"):
            self.set_status(f"查询失败：{r['error']}")
            messagebox.showerror("查询失败", f"{r['error']}\n等30秒重试")
            return
        if not self.combos:
            if r.get("graph") and self._graph_is_empty():
                self.notice.config(text="图库还是空的：点下面“一键爬取”先攒时刻表（约4秒1次，可随时取消），攒完再用图库深挖")
            else:
                self.notice.config(text="")
            self.set_status("无可行组合，试试放宽最长等待或换日期")
            return
        self.save_hist(frm, to)
        n = len(self.combos)
        flag = "演示数据" if r.get("mock") else ("缓存时刻" if r.get("stale") else ("缓存" if r.get("cached") else "实时"))
        self.last_source = "演示" if r.get("mock") else ("图库" if r.get("graph") else flag)
        if r.get("mock"):
            self.notice.config(text=f"12306限流中，当前为演示数据：{r.get('warn','')}，等30秒再点查询组合")
        elif r.get("graph"):
            st = r.get("stats", {})
            extra = f"＋实时补{r['live_added']}条" if r.get("live_added") else ""
            if r.get("live_warn"):
                extra += "（补查失败，纯图结果）"
            self.notice.config(
                text=f"图库深挖：离线枚举{r.get('candidates', '?')}条候选，实时核验{r.get('verified', '?')}条，"
                     f"成活{len(self.combos)}条{extra}（打了{st.get('requests', '?')}次12306）")
        elif r.get("stale"):
            self.notice.config(text=f"12306刚才繁忙：时刻为缓存数据，余票可能滞后。{r.get('warn','')}")
        elif r.get("cached"):
            self.notice.config(text="命中本地缓存（2分钟内查过，免打扰12306）")
        else:
            self.notice.config(text="")
        mids = r.get("middles") or []
        if mids:
            self.mid_label.config(text="已覆盖中转站：" + "、".join(m["name"] for m in mids[:20]))
        else:
            self.mid_label.config(text="")
        st = r.get("stats") or {}
        req = f"，打了{st.get('requests', '?')}次12306" if st else ""
        self.prog_var.set(100)
        self.prog_stage.config(text=f"完成（用时{el}秒）")
        self.set_status(f"共{n}个方案（含{r.get('direct_count',0)}趟直达，{flag}，用时{el}秒{req}）｜双击复制清单，三卡/星图圆点可定位")
        self.render_bench()
        self.apply_filters()

    def set_status(self, s):
        self.last_status = s
        self.status.config(text=s)

    # ---------- 过滤/展示 ----------
    def on_slide(self):
        mp, md = self.v_price.get(), self.v_dur.get()
        self.lb_price.config(text="不限" if mp >= 5000 else f"≤¥{mp}")
        self.lb_dur.config(text="不限" if md >= 4320 else f"≤{fmt_dur(md)}")
        self._schedule_filter(150)

    def norm_kind(self, k):
        return "直达" if (k or "").startswith("直达") else k

    def has_ticket(self, c):
        if not self.v_ticket.get():
            return True
        for l in c["legs"]:
            if any(v and v not in ("--", "无") for v in (l.get("seats") or {}).values()):
                return True
        return False

    def smart(self, c):
        ps = [x["total_price"] for x in self.combos if x["total_price"]]
        pmin, pmax = min(ps or [0]), max(ps or [1])
        ts = [x["total_min"] for x in self.combos]
        tmin = min(ts)
        tmax = max(ts + [tmin + 60])
        pn = (c["total_price"] - pmin) / max(1, pmax - pmin) if c["total_price"] else 0.7
        tn = (c["total_min"] - tmin) / max(1, tmax - tmin)
        return pn * 0.55 + tn * 0.45 + c["transfers"] * 0.05

    def reset_filters(self):
        self.v_price.set(5000)
        self.v_dur.set(4320)
        for v in list(self.kind_vars.values()) + list(self.tr_vars.values()) + list(self.dp_vars.values()):
            v.set(True)
        self.v_ticket.set(True)
        self.cb_sort.current(0)
        self.e_via.delete(0, "end")
        for e in (self.e_dep_lo, self.e_dep_hi, self.e_arr_lo, self.e_arr_hi):
            e.delete(0, "end")
        self.on_slide()
        self.set_status("过滤条件已重置")

    def via_ok(self, c):
        q = self.e_via.get().strip() if hasattr(self, "e_via") else ""
        if not q:
            return True
        for l in c["legs"]:
            txt = (l.get("from_name", "") + l.get("to_name", "") + l.get("from_code", "") +
                   l.get("to_code", "") + l.get("code", ""))
            if q in txt:
                return True
        return False

    @staticmethod
    def _hm(s):
        """HH:MM -> 分钟，无效返回 None。"""
        try:
            h, m = s.strip().split(":")
            h, m = int(h), int(m)
            if 0 <= h < 24 and 0 <= m < 60:
                return h * 60 + m
        except Exception:
            pass
        return None

    @staticmethod
    def _in_range(clock_min, lo, hi):
        if lo is None and hi is None:
            return True
        if lo is None:
            return clock_min <= hi
        if hi is None:
            return clock_min >= lo
        if lo <= hi:
            return lo <= clock_min <= hi
        return clock_min >= lo or clock_min <= hi  # 过夜区间

    def clock_ok(self, c):
        try:
            dep = self._hm(c["legs"][0].get("start_time", ""))
            arr = self._hm(c["legs"][-1].get("arrive_time", ""))
        except Exception:
            return True
        dlo = self._hm(self.e_dep_lo.get()) if hasattr(self, "e_dep_lo") else None
        dhi = self._hm(self.e_dep_hi.get()) if hasattr(self, "e_dep_hi") else None
        alo = self._hm(self.e_arr_lo.get()) if hasattr(self, "e_arr_lo") else None
        ahi = self._hm(self.e_arr_hi.get()) if hasattr(self, "e_arr_hi") else None
        if dep is not None and not self._in_range(dep, dlo, dhi):
            return False
        if arr is not None and not self._in_range(arr, alo, ahi):
            return False
        return True

    def apply_filters(self):
        if not self.combos:
            return
        mp, md = self.v_price.get(), self.v_dur.get()
        kinds = {k for k, v in self.kind_vars.items() if v.get()}
        trs = {k for k, v in self.tr_vars.items() if v.get()}
        dps = [tuple(map(int, k.split("-"))) for k, v in self.dp_vars.items() if v.get()]
        arr = []
        for c in self.combos:
            if c["total_price"] and c["total_price"] > mp:
                continue
            if c["total_min"] > md:
                continue
            if self.norm_kind(c["combo_kind"]) not in kinds:
                continue
            if str(c["transfers"]) not in trs:
                continue
            if dps:
                try:
                    h = int((c["legs"][0]["start_time"] or "0:0").split(":")[0])
                except Exception:
                    h = 0
                if not any(a <= h < b for a, b in dps):
                    continue
            else:
                continue
            if not self.has_ticket(c):
                continue
            if not self.via_ok(c):
                continue
            if not self.clock_ok(c):
                continue
            arr.append(c)
        s = self.cb_sort.get()
        if s == "总价最低":
            arr.sort(key=lambda c: c["total_price"] if c["total_price"] else 9e9)
        elif s == "总耗时最短":
            arr.sort(key=lambda c: c["total_min"])
        elif s == "换乘等待最短":
            arr.sort(key=lambda c: sum(c.get("waits", [])))
        elif s == "出发时间":
            arr.sort(key=lambda c: (c["legs"][0].get("dep_plus", 0),
                                    c["legs"][0].get("start_time", "")))
        else:
            arr.sort(key=lambda c: self.smart(c))
        self.filtered = arr
        self.render_list()
        self.draw_scatter()

    def summary(self, c, i):
        tag = "★" if i == 0 else f"{i+1}."
        mid = "" if c["transfers"] == 0 else f"中转{c['transfers']}次·"
        return (f"{tag}{c['codes']} {fmt_price(c['total_price'])} "
                f"{fmt_dur(c['total_min'])} {mid}{c['combo_kind']}")

    def render_list(self):
        self.lb.delete(0, "end")
        rows = [self.summary(c, i) for i, c in enumerate(self.filtered[:5000])]
        if rows:
            self.lb.insert("end", *rows)
        if self.filtered:
            self.lb.selection_set(0)
            self.show_detail()
        else:
            self.tx.config(state="normal")
            self.tx.delete("1.0", "end")
            self.tx.insert("end", "被过滤完了，放宽左侧滑杆试试。")
            self.tx.config(state="disabled")

    def render_bench(self):
        pool = [c for c in self.combos if c["total_price"]] or self.combos
        if not pool:
            return
        fast = min(pool, key=lambda c: c["total_min"])
        cheap = min(pool, key=lambda c: c["total_price"] if c["total_price"] else 9e9)
        mid = min(pool, key=lambda c: self.smart(c))
        self.bench_refs = {"fast": fast, "mid": mid, "cheap": cheap}
        f = lambda c: f"{c['codes']} {fmt_price(c['total_price'])} {fmt_dur(c['total_min'])}"
        self.b_fast.config(text=f"最快：{f(fast)}（点击定位）")
        self.b_mid.config(text=f"★中间值：{f(mid)}（点击定位）")
        self.b_cheap.config(text=f"最便宜：{f(cheap)}（点击定位）")
        for w, k in ((self.b_fast, "fast"), (self.b_mid, "mid"), (self.b_cheap, "cheap")):
            w.bind("<Button-1>", lambda e, k=k: self.locate_bench(k))
            w.config(cursor="hand2")

    def locate_bench(self, k):
        c = self.bench_refs.get(k)
        if c is None:
            return
        for i, x in enumerate(self.filtered):
            if x is c:
                self.lb.selection_clear(0, "end")
                self.lb.selection_set(i)
                self.lb.see(i)
                self.display_combo(i)
                return
        # 被过滤掉了：直接展示，并提示
        self.display_combo_object(c)
        self.set_status("该方案被当前过滤条件筛掉了，已直接展示详情（点重置过滤可找回）")

    def show_detail(self):
        sel = self.lb.curselection()
        if not sel or sel[0] >= len(self.filtered):
            return
        self.display_combo(sel[0])

    def display_combo(self, idx):
        if idx < 0 or idx >= len(self.filtered):
            return
        self.display_combo_object(self.filtered[idx])

    def combo_detail_parts(self, c):
        """详情行 [(文本, tag)]，tag in title/price/warn/normal."""
        tr = "直达" if c["transfers"] == 0 else "中转%d次" % c["transfers"]
        P = [(f"{c['codes']}  {fmt_price(c['total_price'])}  {fmt_dur(c['total_min'])}  "
              f"{tr}  {c['combo_kind']}", "title")]
        P.append((f"行程：{c.get('dep_date','')} → {c.get('arr_date','')}，等待合计"
                  f"{sum(c.get('waits',[]))}分钟", "normal"))
        P.append(("-" * 46, "normal"))
        for j, l in enumerate(c["legs"]):
            dp = f"+{l['dep_plus']}天" if l.get("dep_plus") else ""
            ap = f"+{l['arr_plus']}天" if l.get("arr_plus") else ""
            seats = " ".join(f"{k}{v}" for k, v in (l.get("seats") or {}).items()
                             if v and k in ("二等", "一等", "商务特等", "硬座", "硬卧", "无座")) or "余票见12306"
            P.append((f"{j+1}. {l['code']} {l.get('from_name',l.get('from_code'))}"
                      f"{l['start_time']}{dp} → {l.get('to_name',l.get('to_code'))}"
                      f"{l['arrive_time']}{ap} 历时{l.get('duration','')}", "normal"))
            P.append((f"   票价约{fmt_price(l.get('price'))}{l.get('price_name') or ''}｜{seats}", "price"))
            if j < len(c["legs"]) - 1:
                mid = c["legs"][j].get("to_name") or c["legs"][j].get("to_code")
                P.append((f"   ▼ {mid}换乘 {c['waits'][j]}分钟", "warn"))
        P.append(("-" * 46, "normal"))
        P.append(("购票：去12306 App/官网按上面分段分别购票（或用“中转/接续换乘”页直接出票）。", "normal"))
        P.append(("注意：同站换乘最稳（预留30分钟+）；同城不同站预留1小时+市内交通；夜车看清+1天。", "warn"))
        return P

    def combo_buy_text(self, c):
        """纯文本购票清单（复制/导出用）。"""
        L = [f"【铁行择优】{c['codes']} {fmt_price(c['total_price'])} {fmt_dur(c['total_min'])}",
             f"行程 {c.get('dep_date','')} → {c.get('arr_date','')}"]
        for j, l in enumerate(c["legs"]):
            dp = f"+{l['dep_plus']}天" if l.get("dep_plus") else ""
            ap = f"+{l['arr_plus']}天" if l.get("arr_plus") else ""
            L.append(f"{j+1}. {l['code']} {l.get('from_name',l.get('from_code'))}{l['start_time']}{dp}"
                     f"→{l.get('to_name',l.get('to_code'))}{l['arrive_time']}{ap}"
                     f"（约{fmt_price(l.get('price'))}{l.get('price_name') or ''}）")
            if j < len(c["legs"]) - 1:
                mid = c["legs"][j].get("to_name") or c["legs"][j].get("to_code")
                L.append(f"   {mid}换乘{c['waits'][j]}分钟")
        L.append("去12306分段购票或走接续换乘；同城换乘预留1小时；夜车看清日期。")
        return "\n".join(L)

    def current_combo(self):
        sel = self.lb.curselection()
        if sel and sel[0] < len(self.filtered):
            return self.filtered[sel[0]]
        return self.filtered[0] if self.filtered else None

    def graph_stats_text(self):
        try:
            con = graphdb.connect()
            try:
                st = graphdb.stats(con)
            finally:
                con.close()
            return f"图里有{st['trains']}趟车·{st['stations']}个站"
        except Exception:
            return "图暂无数据"

    def _graph_is_empty(self):
        """图库是否空（0 趟车）：空库时深挖必然无结果，给引导而不是干等。"""
        try:
            con = graphdb.connect()
            try:
                return graphdb.stats(con)["trains"] == 0
            finally:
                con.close()
        except Exception:
            return False

    def view_graph(self):
        """数据模块：概览统计 + 每次爬取记录 + 按车次查经停 + 按车站查过路车 + 最近入库。"""
        old = getattr(self, "_graph_win", None)
        if old is not None:
            try:
                if old.winfo_exists():
                    old.lift()
                    old.focus_force()
                    return old
            except Exception:
                pass
        win = tk.Toplevel(self.root)
        win.title("数据模块 · 时刻表图库")
        win.geometry("660x520")
        try:
            con = graphdb.connect()
            try:
                st = graphdb.stats(con)
                recent = graphdb.recent_trains(con, 30)
                runs = graphdb.recent_runs(con, 8)
                rate = graphdb.meta_get(con, "rate_interval", "?")
            finally:
                con.close()
            try:
                dbsize = os.path.getsize(os.path.join(BASE, "timetable.db")) // 1024
                dbs = f" · 库文件{dbsize}KB"
            except Exception:
                dbs = ""
            head = (f"图里共 {st['trains']} 趟车 · {st['stations']} 个站 · 已啃完 {st['ods']} 个OD对{dbs}\n"
                    f"自适应间隔上次停在 {rate} 秒/次。数据来自后台爬取的公开经停站，\n"
                    f"只做中转候选，最终以实时余票为准。防重：车次主键去重＋OD做完标记＋已知车跳过。")
        except Exception as e:
            head = f"图库打不开：{e}"
            recent, runs = [], []
        ttk.Label(win, text=head, padding=8, wraplength=620, justify="left").pack(anchor="w")
        qf = ttk.Frame(win, padding=(8, 0))
        qf.pack(fill="x")
        ttk.Label(qf, text="车次").pack(side="left")
        e_code = ttk.Entry(qf, width=10)
        e_code.pack(side="left", padx=4)
        ttk.Label(qf, text="车站").pack(side="left", padx=(8, 0))
        e_st = ttk.Entry(qf, width=12)
        e_st.pack(side="left", padx=4)
        tx = tk.Text(win, wrap="word", height=20)
        tx.pack(fill="both", expand=True, padx=8, pady=8)
        tx.tag_config("title", font=("", 10, "bold"))

        def show(lines, title=""):
            tx.config(state="normal")
            tx.delete("1.0", "end")
            if title:
                tx.insert("end", title + "\n", "title")
            tx.insert("end", "\n".join(lines) if lines else "（无结果：图还没爬到，或名字不对）")
            tx.config(state="disabled")

        def q_code(_e=None):
            kw = e_code.get().strip()
            if not kw:
                return
            try:
                con = graphdb.connect()
                try:
                    rows = graphdb.train_detail(con, kw)
                finally:
                    con.close()
            except Exception as e:
                show([], f"查询失败：{e}")
                return
            show([f"{n} {a or '--'}开 → {d or '--'}到" for _, n, a, d in rows],
                 f"车次 {kw} 共{len(rows)}站：")

        def q_st(_e=None):
            kw = e_st.get().strip()
            if not kw:
                return
            name, code = api.resolve_station(kw)
            if not code:
                show([], f"车站“{kw}”找不到")
                return
            try:
                con = graphdb.connect()
                try:
                    rows = graphdb.trains_at(con, code, 80)
                finally:
                    con.close()
            except Exception as e:
                show([], f"查询失败：{e}")
                return
            show([f"{c or t}" for t, c in rows], f"经停{name}的车（{len(rows)}趟）：")

        ttk.Button(qf, text="查车次", command=q_code).pack(side="left", padx=4)
        ttk.Button(qf, text="查车站", command=q_st).pack(side="left", padx=4)
        ttk.Button(qf, text="爬取记录", command=lambda: show(
            [f"{ts} 请求{req}次 新增{new}趟 优先:{pri or '无'}" for ts, req, new, pri in runs],
            f"最近{len(runs)}次爬取（多次爬取不重复：做完的OD打标记，已知车次跳过）：")).pack(side="left", padx=4)
        e_code.bind("<Return>", q_code)
        e_st.bind("<Return>", q_st)
        show([f"{c or t}（{d}入库）" for t, c, d in recent], "最近入库：")
        self._graph_win = win
        return win

    def crawl_db(self):
        if getattr(self, "crawling", False):
            messagebox.showinfo("提示", "正在爬取中，稍等")
            return
        if self.searching:
            messagebox.showwarning("提示", "先等这次查询跑完再一键爬取（避免并发打 12306 被限流）")
            return
        try:
            cmax = int(self.e_crawlmax.get().strip())
            if not (cmax == 0 or 10 <= cmax <= 5000):
                raise ValueError()
        except Exception:
            messagebox.showwarning("提示", "限次请填 0（无限）或 10~5000 的整数")
            return
        import re
        prio = [x for x in re.split(r"[,，]", self.e_crawfrom.get()) if x.strip()]
        phase = {"自动": "auto", "仅干线": "mesh", "仅地级": "fanout"}[self.cb_phase.get()]
        self.crawling = True
        self.crawl_stop = threading.Event()
        self.btn_cancel.config(state="normal")
        self.crawl_t0 = time.time()
        if cmax == 0:
            self.prog.config(mode="indeterminate")
            self.prog.start(50)
        else:
            try:
                self.prog.stop()
            except Exception:
                pass
            self.prog.config(mode="determinate")
            self.prog_var.set(0)
        self.prog_stage.config(text="准备中，正在连 12306…")
        self.set_status("一键爬取已启动，正在连 12306（可点取消）…")

        def cb(u, m, msg):
            self.root.after(0, lambda: self._on_crawl_prog(u, m, msg))

        def work():
            try:
                st = crawlmod.run(max_requests=cmax, interval=4.0, on_progress=cb,
                                  should_stop=self.crawl_stop.is_set, priority_from=prio,
                                  phase=phase)
                msg = (f"一键爬取完成：本次{st['requests']}次请求、新增{st['new_trains']}趟车，"
                       f"最快间隔{st.get('min_interval','?')}秒、退避{st.get('backoffs',0)}次；"
                       f"图里共{st['trains']}趟·{st['stations']}站")
            except Exception as e:
                msg = f"更新中断：{e}"
            self.root.after(0, lambda: self._crawl_done(msg))

        threading.Thread(target=work, daemon=True).start()

    def _on_crawl_prog(self, u, m, msg):
        if not getattr(self, "crawling", False):
            return
        free = not m  # 限次 0 = 无限
        try:
            if not free:
                self.prog_var.set(int(u / m * 100))
        except Exception:
            pass
        el = int(time.time() - getattr(self, "crawl_t0", time.time()))
        if free:
            eta_s = "（无限模式，可点取消）"
        else:
            eta = f"，预计还需{int(el / max(1, u) * (m - u))}秒" if u and m else ""
            eta_s = f"（已用{el}秒{eta}）"
        total = f"[{u}/∞]" if free else f"[{u}/{m}]"
        self.prog_stage.config(text=f"爬取 {total} {msg}{eta_s if not free else ''}")
        self.set_status(f"一键爬取中 {total} {msg}{eta_s}，可点取消")

    def _crawl_done(self, msg):
        self.crawling = False
        self.crawl_stop = None
        if not self.searching:
            self.btn_cancel.config(state="disabled")
        try:
            self.prog.stop()
        except Exception:
            pass
        self.prog.config(mode="determinate")
        self.prog_var.set(100)
        self.prog_stage.config(text="爬取结束")
        self.set_status(msg)
        messagebox.showinfo("一键爬取", msg)

    def copy_buy(self):
        c = self.current_combo()
        if not c:
            messagebox.showinfo("提示", "还没有可复制的方案，先查询")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.combo_buy_text(c))
        self.set_status(f"已复制：{c['codes']} 购票清单，去12306粘贴对照着买")

    def export_txt(self):
        if not self.filtered:
            messagebox.showinfo("提示", "还没有可导出的方案，先查询")
            return
        p = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("文本", "*.txt")],
                                         initialfile="railpick.txt")
        if not p:
            return
        try:
            with open(p, "w", encoding="utf-8") as f:
                for i, c in enumerate(self.filtered[:5000]):
                    f.write(f"[{i+1}] {self.summary(c, i)}\n{self.combo_buy_text(c)}\n\n")
            self.set_status(f"已导出 {min(5000, len(self.filtered))} 个方案 → {p}")
        except Exception as e:
            messagebox.showerror("导出失败", str(e))

    def display_combo_object(self, c):
        self.tx.config(state="normal")
        self.tx.delete("1.0", "end")
        self.tx.tag_config("title", font=("", 10, "bold"))
        self.tx.tag_config("price", foreground="#0E3B2E")
        self.tx.tag_config("warn", foreground="#B7791F")
        self.tx.tag_config("normal", foreground="#1B211F")
        parts = self.combo_detail_parts(c)
        if parts:
            src = getattr(self, "last_source", "")
            if src:
                parts[0] = (f"[{src}] " + parts[0][0], parts[0][1])
        for txt, tag in parts:
            self.tx.insert("end", txt + "\n", tag)
        self.tx.config(state="disabled")

    # ---------- 星图 ----------
    def draw_scatter(self):
        cv = self.cv
        cv.delete("all")
        self.dots = []
        arr = self.filtered[:300]
        W = cv.winfo_width() or 900
        H = 210
        if not arr:
            cv.create_text(W // 2, H // 2, text="暂无数据", fill="#8b968f")
            return
        ts = [c["total_min"] for c in arr]
        ps = [c["total_price"] or 0 for c in arr]
        tmin, tmax = min(ts), max(ts + [min(ts) + 60])
        pmax = max([p for p in ps if p] or [1])
        colors = {0: "#7ee2a8", 1: "#FF5A1F", 2: "#6aa8ff", 3: "#c792ea", 4: "#ffcb6b"}
        X = lambda t: 40 + (t - tmin) / max(1, tmax - tmin) * (W - 80)
        Y = lambda p: H - 30 - (p / max(1, pmax)) * (H - 70)
        for i, c in enumerate(arr):
            x, y = X(c["total_min"]), Y(c["total_price"] or pmax)
            r = 7 if i == 0 else 5
            if c["total_price"] is None:
                # 无价画空心圈，别跟最高价扎堆混淆
                cv.create_oval(x - r, y - r, x + r, y + r, fill="", outline="#8b968f")
            else:
                cv.create_oval(x - r, y - r, x + r, y + r,
                               fill=colors.get(c["transfers"], "#fff"), outline="")
            self.dots.append((x, y, i))
        cv.create_text(W - 120, 14, text=f"共{len(arr)}个方案", fill="#8b968f")
        cv.create_text(60, H - 12, text=fmt_dur(int(tmin)), fill="#8b968f")
        cv.create_text(W - 60, H - 12, text=fmt_dur(int(tmax)), fill="#8b968f")
        cv.create_text(34, 16, text=fmt_price(pmax), fill="#8b968f")

    def on_hover(self, ev):
        if not self.dots or not self.filtered:
            return
        bi = min(range(len(self.dots)),
                 key=lambda i: (self.dots[i][0] - ev.x) ** 2 + (self.dots[i][1] - ev.y) ** 2)
        x, y, idx = self.dots[bi]
        if abs(x - ev.x) + abs(y - ev.y) > 26 or idx >= len(self.filtered):
            self.set_status(self.last_status)
            return
        c = self.filtered[idx]
        self.set_status(f"{c['codes']} {fmt_price(c['total_price'])} {fmt_dur(c['total_min'])} "
                        f"{c['combo_kind']}（点击定位详情）")

    def on_dot(self, ev):
        if not self.dots:
            return
        bi = min(range(len(self.dots)),
                 key=lambda i: (self.dots[i][0] - ev.x) ** 2 + (self.dots[i][1] - ev.y) ** 2)
        idx = self.dots[bi][2]
        self.lb.selection_clear(0, "end")
        self.lb.selection_set(idx)
        self.lb.see(idx)
        self.show_detail()


def main():
    root = tk.Tk()
    try:
        root.option_add("*Font", ("Microsoft YaHei UI", 9))
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


def smoke():
    """冒烟：构造界面+跑过滤逻辑，不弹窗阻塞（显式设初态，不受 config.json 残留影响）。"""
    import tempfile
    global CONFIG_FILE
    CONFIG_FILE = os.path.join(tempfile.gettempdir(), "railpick_smoke_cfg.json")
    try:
        os.remove(CONFIG_FILE)
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()
    app = App(root)
    app.v_price.set(2000)
    app.v_dur.set(1800)
    for v in list(app.kind_vars.values()) + list(app.tr_vars.values()) + list(app.dp_vars.values()):
        v.set(True)
    app.v_ticket.set(True)
    app.cb_sort.set("智能推荐")
    app.e_via.delete(0, "end")
    app.combos = [
        {"codes": "G1", "total_price": 600, "total_min": 300, "transfers": 0,
         "combo_kind": "直达高铁", "waits": [], "dep_date": "2026-09-10",
         "arr_date": "2026-09-10",
         "legs": [{"code": "G1", "type_letter": "G", "train_type": "高铁",
                   "start_time": "08:00", "arrive_time": "13:00", "duration": "05:00",
                   "duration_min": 300, "from_code": "VNP", "to_code": "AOH",
                   "from_name": "北京南", "to_name": "上海虹桥",
                   "dep_plus": 0, "arr_plus": 0, "price": 600, "price_name": "二等",
                   "seats": {"二等": "有"}}]},
        {"codes": "G1+K2", "total_price": 300, "total_min": 500, "transfers": 1,
         "combo_kind": "高铁+普速", "waits": [40], "dep_date": "2026-09-10",
         "arr_date": "2026-09-10",
         "legs": [{"code": "G1", "type_letter": "G", "train_type": "高铁",
                   "start_time": "08:00", "arrive_time": "10:00", "duration": "02:00",
                   "duration_min": 120, "from_code": "VNP", "to_code": "UUH",
                   "from_name": "北京南", "to_name": "徐州东",
                   "dep_plus": 0, "arr_plus": 0, "price": 200, "price_name": "二等",
                   "seats": {"二等": "有"}},
                  {"code": "K2", "type_letter": "K", "train_type": "快速",
                   "start_time": "10:40", "arrive_time": "16:00", "duration": "05:20",
                   "duration_min": 320, "from_code": "XCH", "to_code": "SHH",
                   "from_name": "徐州", "to_name": "上海",
                   "dep_plus": 0, "arr_plus": 0, "price": 100, "price_name": "硬座",
                   "seats": {"硬座": "有"}}]},
    ]
    app.render_bench()
    app.apply_filters()
    assert len(app.filtered) == 2, app.filtered
    assert len(app.dots) == 2, app.dots
    app.show_detail()
    detail = app.tx.get("1.0", "end")
    assert "徐州" in detail and "接续换乘" in detail
    # 三卡定位
    app.locate_bench("mid")
    assert app.tx.get("1.0", "end").strip() != ""
    # 只看经停过滤
    app.e_via.insert(0, "徐州")
    app.apply_filters()
    assert len(app.filtered) == 1 and app.filtered[0]["codes"] == "G1+K2", app.filtered
    app.e_via.delete(0, "end")
    app.apply_filters()
    assert len(app.filtered) == 2
    # 购票清单文本
    buy = app.combo_buy_text(app.filtered[0])
    assert "铁行择优" in buy and "G1" in buy
    # 一键爬取按钮存在（曾有用户反馈找不到：之前叫更新时刻表，已统一）
    texts = set()

    def walk(w):
        try:
            texts.add(w.cget("text"))
        except Exception:
            pass
        for ch in w.winfo_children():
            walk(ch)
    walk(root)
    assert "一键爬取" in texts, sorted(texts)
    # 重置过滤
    app.v_price.set(100)
    app.apply_filters()
    assert len(app.filtered) <= 2
    app.reset_filters()
    # reset 走防抖（150ms），等一拍再断言，避免读到旧值
    import time as _t
    root.update()
    _t.sleep(0.3)
    root.update()
    assert len(app.filtered) == 2 and app.v_price.get() == 5000
    # 出发时间排序不崩
    app.cb_sort.set("出发时间")
    app.apply_filters()
    # 精确时间范围：09点后出发全走，12-14点到达只留G1
    app.e_dep_lo.insert(0, "09:00"); app.e_dep_hi.insert(0, "12:00")
    app.apply_filters()
    assert len(app.filtered) == 0, len(app.filtered)
    app.e_dep_lo.delete(0, "end"); app.e_dep_hi.delete(0, "end")
    app.e_arr_lo.insert(0, "12:00"); app.e_arr_hi.insert(0, "14:00")
    app.apply_filters()
    assert len(app.filtered) == 1 and app.filtered[0]["codes"] == "G1", len(app.filtered)
    app.e_arr_lo.delete(0, "end"); app.e_arr_hi.delete(0, "end")
    app.apply_filters()
    assert len(app.filtered) == 2
    # 爬取限次默认 1000
    assert app.e_crawlmax.get() == "1000", app.e_crawlmax.get()
    assert app.e_crawfrom.get() == "", repr(app.e_crawfrom.get())
    assert app.e_enum.get() == "300", app.e_enum.get()
    # 空闲点取消不崩；爬取取消置位有效
    app.cancel()
    app.crawling = True
    import threading as _th
    app.crawl_stop = _th.Event()
    app.btn_cancel.config(state="normal")
    app.cancel()
    assert app.crawl_stop.is_set() and app.crawling is False
    # 爬取进度回调直接刷进度条/阶段（第一 OD 跑完前也有心跳，不会“点了没反应”）
    app.crawling = True
    app._on_crawl_prog(7, 100, "BJP→SHH 采集中")
    assert app.prog_var.get() == 7, app.prog_var.get()
    assert "BJP" in app.prog_stage.cget("text")
    app.crawling = False
    # 图库查看器能打开（用空库，不崩即可）
    win = app.view_graph()
    assert win is not None
    win.destroy()
    # 工作线程抛错：错误回调必须送达，界面不能卡死在“查询中”
    # （回归：曾因 lambda 里直接 str(e) 导致 NameError，按钮永远 disabled）
    import tkinter.messagebox as _mb
    _real_err = _mb.showerror
    _mb.showerror = lambda *a, **k: None
    try:
        app.searching = True
        app.job_token += 1
        tok = app.job_token
        try:
            raise RuntimeError("boom")
        except Exception as e:
            err = str(e)  # 生产代码同款写法：先转字符串再进 lambda
            app.root.after(0, lambda: app._done("甲", "乙", None, err, tok))
        app.root.update()
        assert app.searching is False
        assert str(app.btn_go.cget("state")) == "normal"
    finally:
        _mb.showerror = _real_err
    # ---- 新增回归：钳制 / 防抖 / 来源标记 / 顶栏两行 / 预热 / 图库单例 / 空图引导 ----
    assert App._clamp_gaps(20, 99999) == (20, 1440, True)
    assert App._clamp_gaps(20, 240) == (20, 240, False)
    assert App._clamp_gaps(30, 10) == (30, 10, False)
    calls = []
    app._debounce("smoke-t", 30, lambda: calls.append(1))
    app._debounce("smoke-t", 30, lambda: calls.append(1))
    app._debounce("smoke-t", 30, lambda: calls.append(1))
    _t.sleep(0.1)
    root.update()
    assert calls == [1], calls
    assert app.topbar1.winfo_children() and app.topbar2.winfo_children()
    app.last_source = "图库"
    app.display_combo_object(app.filtered[0])
    assert "[图库]" in app.tx.get("1.0", "end")
    import backend.c12306 as _api
    _real_load = _api.load_stations
    _api.load_stations = lambda *a, **k: []
    try:
        app._stations_ready = False
        app.btn_go.config(state="disabled")
        app._preload()
        root.update()
        assert app._stations_ready is True
        assert str(app.btn_go.cget("state")) == "normal"
        assert "站名库就绪" in app.status.cget("text")
    finally:
        _api.load_stations = _real_load
    w1 = app.view_graph()
    w2 = app.view_graph()
    assert w1 is w2
    w1.destroy()
    import backend.graphdb as _gdb
    _real_stats = _gdb.stats
    _gdb.stats = lambda con: {"trains": 0, "stations": 0, "ods": 0}
    try:
        app.searching = True
        app.job_token += 1
        tok3 = app.job_token
        app._done("甲", "乙", {"graph": True, "combos": []}, None, tok3)
        assert "一键爬取" in app.notice.cget("text")
    finally:
        _gdb.stats = _real_stats
    # 最小窗口下顶栏两行无裁剪（回归：单行顶栏在 1024 宽下挤掉按钮）
    root.deiconify()
    root.geometry("1024x640")
    root.update()
    root.update_idletasks()
    for _rowname in ("topbar1", "topbar2"):
        _row = getattr(app, _rowname)
        _W = _row.winfo_width()
        for _ch in _row.winfo_children():
            _x0, _x1 = _ch.winfo_x(), _ch.winfo_x() + _ch.winfo_width()
            assert 0 <= _x0 and _x1 <= _W, (_rowname, _x0, _x1, _W)
    root.withdraw()
    # 大数据量过滤性能 guard（2000 条 5 秒内；回归逐条 insert 卡顿）
    import time as _tm
    _big = []
    for _i in range(2000):
        _big.append({"codes": "G%d" % _i, "total_price": 100 + _i, "total_min": 300,
                     "transfers": 1, "combo_kind": "高铁+普速", "waits": [10],
                     "dep_date": "2026-09-10", "arr_date": "2026-09-10",
                     "legs": [{"code": "G1", "type_letter": "G", "start_time": "08:00",
                               "arrive_time": "13:00", "duration": "05:00",
                               "from_code": "VNP", "to_code": "AOH",
                               "from_name": "北京南", "to_name": "上海虹桥",
                               "dep_plus": 0, "arr_plus": 0,
                               "seats": {"二等": "有"}}]})
    app.combos = _big
    _t0 = _tm.time()
    app.apply_filters()
    assert _tm.time() - _t0 < 5, "filter too slow"
    assert len(app.filtered) == 2000
    print("SMOKE OK: bench+filter+scatter+detail+via+copy+reset+sort")
    root.destroy()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--smoke":
        smoke()
    else:
        main()

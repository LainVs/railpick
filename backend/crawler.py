"""礼貌爬虫：后台慢慢攒时刻表图，不打扰正常查询.

链路：种子OD对查余票 -> 发现车次号 -> 逐车拉全程经停站 -> 入库.
1 个 OD 查询发现几十趟车，1 趟车 1 个请求拿下全线车站——
覆盖效率比站站对查高两个数量级。

礼貌策略：默认 4 秒/请求 + 抖动；连续失败 5 次就停（大概率被限流了）；
预算制（--max），跑完即停。只在用户点击“更新时刻表”或手动跑 CLI 时工作，
绝不开机自启、绝不抢正常查询的配额。

    python -m backend.crawler --max 1000         # 爬最多 1000 个请求（默认）
    python -m backend.crawler --stats            # 看图里有多少货
    python -m backend.crawler --max 50 --interval 6
"""
import argparse
import random
import sys
import time
from datetime import date, timedelta

from . import c12306 as api
from . import graphdb
from . import geo

# 种子枢纽（电报码，省掉解析）：覆盖主要干线交汇点
SEED = ["BJP", "SHH", "GZQ", "SZQ", "ZZF", "WHN", "CSQ", "XAY", "CDW", "CQW",
        "HZH", "NJH", "JNK", "SJP", "SYT", "TJP", "HBB", "LZJ", "KMM", "CCT"]


def crawl_date():
    return (date.today() + timedelta(days=2)).isoformat()


class Throttled(Exception):
    """被 12306 接口级限流（返回 HTML 拦截页，不是普通网络错误）.

    实测：触发后余票接口会封很久（>25 分钟），期间 init 页和车站列表仍正常。
    遇到它应该立刻停止本次爬取，而不是继续重试（继续打只会拖长封禁）。
    """


def seed_ods():
    return build_ods()


def build_ods(priority_codes=None, seed=None):
    """OD 顺序：优先站的进出对排最前（常坐的站先攒），再跟全量种子对，去重保序.

    优先站不能只和 20 个种子站配对：唐山这类站的真正换乘邻居是济南/泰安/天津，
    它们不在种子里，只跟种子配对就永远爬不到顺路中转（图里全是京沪京广的干线车）。
    所以优先站 = 种子 + 走廊上最近的若干地级市，两头都爬。
    """
    seed = list(seed or SEED)
    ordered, seen = [], set()
    for p in (priority_codes or []):
        partners = list(seed)
        # 按“顺路程度”补充优先站的邻居：以优先站为起点、各个方向的走廊都算，
        # 每个城市只取主站（站名等于城市名或最短的那个），避免“天津北/北京通州”
        # 这类小站把名额占满。
        try:
            pname = None
            for s in api.load_stations():
                if s["code"] == p:
                    pname = s["name"]
                    break
            pc = geo.coords_for(pname) if pname else None
            if pc:
                near = []
                for city in geo.CITY:
                    if city == pname:
                        continue
                    c = geo.coords_for(city)
                    if not c:
                        continue
                    cands = [s for s in api.load_stations()
                             if s["name"] == city or s["name"].startswith(city)]
                    if not cands:
                        continue
                    main = min(cands, key=lambda s: (s["name"] != city, len(s["name"])))
                    code = main["code"]
                    if not code or code == p or code in partners:
                        continue
                    d2 = (c[0] - pc[0]) ** 2 + (c[1] - pc[1]) ** 2
                    near.append((d2, code))
                near.sort()
                for _d2, code in near[:24]:
                    if code not in partners:
                        partners.append(code)
        except Exception:
            pass
        for b in partners:
            if b == p:
                continue
            for o in ((p, b), (b, p)):
                if o not in seen:
                    seen.add(o)
                    ordered.append(o)
    for a in seed:
        for b in seed:
            if a != b and (a, b) not in seen:
                seen.add((a, b))
                ordered.append((a, b))
    return ordered


def resolve_codes(names):
    """站名/代码列表 -> 电报码列表（解析不到的丢弃）。"""
    out = []
    for n in names or []:
        n = (n or "").strip()
        if not n:
            continue
        if len(n) == 3 and n.isupper():
            out.append(n)
            continue
        _, code = api.resolve_station(n)
        if code and code not in out:
            out.append(code)
    return out


def polite_sleep(interval):
    time.sleep(interval + random.uniform(0, 2))


class RateController:
    """自适应调速（AIMD）：成功就慢慢加速，失败就翻倍退避.

    为什么不用固定间隔去“测上限”：往死里压测等于主动把自家 IP 送进
    12306 黑名单——真买票的时候被封就搞笑了。让系统自己摸安全速率：
    下限 1.5s（保守红线，不再往下探），上限 30s。每次跑完把速率存库，
    下次接着来。统计里会告诉你本次最快到多少、有没有被退避过。

    ---- 2026-10 实测（TSP->BJP 等 6 组真实 OD，每次换日期避开本地缓存）----
      间隔 1.5s : 8/8 成功
      间隔 1.0s : 8/8 成功
      间隔 0.6s : 4/8 成功（开始返回 HTML 拦截页）
      间隔 0.35s: 0/8 成功
    结论：安全边界在 1.0~0.6s 之间。但一旦触发限流，**余票接口会被封很久**
    （实测 25 分钟以上仍未解封，而 init 页/车站列表仍正常，是接口级封禁），
    且封禁期间无法查询余票，只能干等。所以下限仍取 1.5s：
    用 50% 的速度换“基本不会被封”，对低频查询工具是划算的。
    """

    def __init__(self, cur=4.0, lo=1.5, hi=30.0):
        self.cur = min(hi, max(lo, float(cur)))
        self.lo, self.hi = lo, hi
        self.backoffs = 0
        self.min_seen = self.cur

    def wait(self):
        # 实际间隔 = cur + 0~1s 抖动。抖动是刻意的：固定节奏更容易被识别。
        # 注意统计里报的 cur 是“基准间隔”，真实间隔比它多约 0.5s。
        time.sleep(self.cur + random.uniform(0, 1))
        self.min_seen = min(self.min_seen, self.cur)
        return self.cur

    def ok(self):
        self.cur = max(self.lo, self.cur - 0.2)

    def fail(self):
        self.backoffs += 1
        self.cur = min(self.hi, self.cur * 2.0)


def fetch_route_stops(train_no, from_code, to_code, day):
    """拉一趟车全程经停 -> [(站码, 站名, 到, 发)]，失败返回 []."""
    try:
        data = api.query_route(train_no, from_code, to_code, day)
    except Exception:
        return []
    lst = data.get("data", []) if isinstance(data, dict) else []
    if not isinstance(lst, list):
        return []
    out = []
    for s in lst:
        if not isinstance(s, dict):
            continue
        name = (s.get("station_name") or "").strip()
        if not name:
            continue
        _, code = api.resolve_station(name)
        if not code:
            continue
        out.append((code, name, s.get("arrive_time") or "", s.get("start_time") or ""))
    return out


def crawl_step(con, frm, too, day, rc, budget, should_stop=None, on_beat=None, beat_every=5):
    """爬一个 OD 对，返回 (请求数, 新入库车次数, 查询成功否, 是否啃完, 新车代码).

    预算耗尽时 break（已用计数保留），只有整单啃完才标 OD done，
    剩下的下轮继续（已知车次会自动跳过）。
    on_beat(used)：每采 beat_every 个请求心跳一次（一个 OD 几十趟车时，
    不加心跳 UI 会半天没动静——用户报过“点了没反应”）。
    """
    used, fresh, recent = 0, 0, []
    try:
        rc.wait()
        used += 1
        trains, _ = api.query_tickets(frm, too, day, retries=1)
        rc.ok()
    except Exception as e:
        rc.fail()
        # 关键：把“被限流”与普通网络错误分开。限流会持续很久（实测 >25 分钟），
        # 继续跑只会白白累积失败次数，还可能延长封禁。
        msg = str(e)
        if "非JSON" in msg or "HTML" in msg or "限流" in msg or "空响应" in msg:
            raise Throttled(f"{frm}→{too} 被限流：{msg[:120]}") from e
        return used, fresh, False, False, recent
    done_all = True
    for t in trains:
        if should_stop and should_stop():
            done_all = False
            break
        if budget is not None and used >= budget:
            done_all = False
            break
        tno = t.get("train_no")
        if not tno or graphdb.has_train(con, tno):
            continue
        rc.wait()
        used += 1
        stops = fetch_route_stops(tno, t.get("from_code") or frm, t.get("to_code") or too, day)
        if on_beat and used % beat_every == 0:
            try:
                on_beat(used)
            except Exception:
                pass
        if len(stops) >= 2:
            graphdb.upsert_train(con, tno, t.get("code", ""), stops, day)
            fresh += 1
            code = t.get("code") or tno
            if code not in recent:
                recent.append(code)
            rc.ok()
        else:
            rc.fail()
    if done_all:
        graphdb.mark_od(con, frm, too, day, fresh)
    return used, fresh, True, done_all, recent[-3:]


# 地级散射：250 城 × 最近枢纽。干线网啃完（或并行）后，用它去抓支线车。
FANOUT_HUB_NAMES = ["北京", "上海", "广州", "深圳", "郑州", "武汉", "西安", "成都"]


def nearest_hubs(origin_name, hub_names, k=5):
    """离出发城市最近的 k 个枢纽（纯地理，未知坐标就按原序）。"""
    a = geo.coords_for(origin_name)
    if a is None:
        return list(hub_names[:k])

    def dist(h):
        c = geo.coords_for(h)
        return ((c[0] - a[0]) ** 2 + (c[1] - a[1]) ** 2) if c else float("inf")

    return sorted(hub_names, key=dist)[:k]


def build_fanout(origins, hub_names, per_origin=5):
    """origins/hubs: [(站名, 站码)]。每城连最近的几个枢纽（双向），去重保序。纯函数，可离线测。"""
    hubs = list(hub_names)
    ods, seen = [], set()
    for oname, ocode in origins:
        near = nearest_hubs(oname, [h[0] for h in hubs], per_origin)
        cmap = dict(hubs)
        for hname in near:
            hcode = cmap.get(hname)
            if not hcode or hcode == ocode:
                continue
            for o in ((ocode, hcode), (hcode, ocode)):
                if o not in seen:
                    seen.add(o)
                    ods.append(o)
    return ods


def order_ods(pool, meta):
    """待啃 OD 排序：没啃过的优先（潜力最大），中断过的按历史产量降序，做完的丢掉."""
    unseen, seen = [], []
    for o in pool:
        d, f = meta.get(o, (0, 0))
        if d:
            continue
        (seen if o in meta else unseen).append(o)
    seen.sort(key=lambda o: -meta[o][1])
    return unseen + seen


def run(db_path=None, max_requests=1000, interval=4.0, day=None, on_progress=None,
        should_stop=None, priority_from=None, phase="auto"):
    """跑一批，返回统计。on_progress(已用, 上限, 消息). should_stop() 为真就尽快收尾.

    priority_from：常用站名/代码列表，这些站的进出 OD 排最前先爬。
    phase：auto=干线先啃再散射地级，mesh=只干线，fanout=只地级。
    """
    con = graphdb.connect(db_path)
    try:
        saved = float(graphdb.meta_get(con, "rate_interval", interval))
    except Exception:
        saved = interval
    rc = RateController(cur=saved)
    day = day or crawl_date()
    cap = max_requests if (max_requests or 0) > 0 else float("inf")  # 0 = 无限：啃完就开新一轮
    used_total, fresh_total, fails = 0, 0, 0
    recent_all = []
    throttled_msg = ""
    prio = resolve_codes(priority_from)
    mesh = build_ods(prio)
    fan = []
    if phase in ("auto", "fanout"):
        origins, hubs = [], []
        for name in geo.CITY:
            _, code = api.resolve_station(name)
            if code:
                origins.append((name, code))
        for h in FANOUT_HUB_NAMES:
            _, code = api.resolve_station(h)
            if code:
                hubs.append((h, code))
        fan = build_fanout(origins, hubs)
    pool = {"mesh": mesh, "fanout": fan}.get(phase, mesh + fan)
    ods = order_ods(pool, graphdb.od_table(con))
    if not ods:  # 一轮跑完就重开新一轮（时刻表会变）
        graphdb.reset_ods(con)
        ods = list(pool)
    for frm, too in ods:
        if used_total >= cap:
            break
        if should_stop and should_stop():
            break
        cur_od = [frm, too]
        _beat_base = [used_total]

        def beat2(u, _od=cur_od):
            if on_progress:
                on_progress(min(_beat_base[0] + u, cap) if cap != float("inf") else _beat_base[0] + u,
                            max_requests, f"{_od[0]}→{_od[1]} 采集中…间隔{rc.cur:.1f}s")

        try:
            used, fresh, ok, _, recent = crawl_step(con, frm, too, day, rc,
                                                    cap - used_total, should_stop,
                                                    on_beat=beat2)
        except Throttled as e:
            # 被接口级限流：立刻停，别继续打（继续打会拖长封禁）。
            # 已入库的车次保留，归档照写。
            used_total += 1
            throttled_msg = str(e)
            if on_progress:
                on_progress(used_total, max_requests,
                            "已被 12306 限流，本次停止（已入库数据保留）")
            break
        used_total += used
        fresh_total += fresh
        fails = 0 if ok else fails + 1
        for c in recent:
            if c not in recent_all:
                recent_all.append(c)
        recent_all = recent_all[-5:]
        if on_progress:
            try:
                st0 = graphdb.stats(con)
                total = (f"累计{st0['trains']}趟/{st0['stations']}站·覆盖{st0.get('coverage', 0)}%")
            except Exception:
                total = ""
            new_s = ("新入库:" + " ".join(recent_all)) if recent_all else "（本轮多为已知车次）"
            on_progress(used_total, max_requests,
                        f"{frm}→{too} 新增{fresh}趟 · {total} · {new_s} · 间隔{rc.cur:.1f}s")
        if fails >= 5:
            if on_progress:
                on_progress(used_total, max_requests, "连续失败，可能被限流，已停止")
            break
    # 归档必须在 finally 里：中途取消或异常时，前面已爬的车次已经入库，
    # 归档记录不能跟着丢（之前只有正常跑完才写，取消一次就等于没爬过）。
    _archive(con, used_total, fresh_total, prio, rc, day)
    st = graphdb.stats(con)
    st.update({"requests": used_total, "new_trains": fresh_total,
               "min_interval": round(rc.min_seen, 1), "backoffs": rc.backoffs,
               "priority": prio})
    if throttled_msg:
        st["throttled"] = True
        st["throttled_msg"] = throttled_msg
    return st


def _archive(con, used_total, fresh_total, prio, rc, day):
    """把本次爬取写进归档：自适应速率 + 一条 crawl_runs 记录。失败不抛."""
    try:
        graphdb.meta_set(con, "rate_interval", f"{rc.cur:.1f}")
    except Exception:
        pass
    try:
        graphdb.log_run(con, used_total, fresh_total, prio, day=day)
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="时刻表礼貌爬虫")
    ap.add_argument("--max", type=int, default=1000, help="最多请求数（默认1000）")
    ap.add_argument("--interval", type=float, default=4.0, help="起始间隔秒（自适应会自己调）")
    ap.add_argument("--from", dest="from_st", default="",
                    help="优先站，逗号分隔，如“唐山,北京”（这些站的进出先爬）")
    ap.add_argument("--phase", default="auto", choices=["auto", "mesh", "fanout"],
                    help="auto=干线先啃再散射地级，mesh=只干线，fanout=只地级")
    ap.add_argument("--stats", action="store_true", help="只看图统计")
    ap.add_argument("--db", default=None, help="db 路径")
    ns = ap.parse_args(argv)
    if ns.stats:
        print(graphdb.stats(graphdb.connect(ns.db)))
        return
    import re
    prio = [x for x in re.split(r"[,，]", ns.from_st) if x.strip()]

    def pg(u, m, msg):
        print(f"[{u}/{m}] {msg}", flush=True)
    st = run(ns.db, ns.max, ns.interval, on_progress=pg, priority_from=prio, phase=ns.phase)
    print("done:", st)


if __name__ == "__main__":
    main()

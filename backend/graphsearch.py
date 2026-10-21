"""离线图搜：在本地时刻表图上无上限枚举换乘，全程零请求；再对头部结果实时核验.

为什么需要它：在线搜索的中转候选、配对、组合数全被截断（防限流），
挖小众路线时天花板太低。图里 4000+ 趟车随便翻，翻完只对选中的几十条
打 12306 核验余票和价格——又自由又省配额。
"""
import heapq
import os
import threading
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, time as dtime, date

from . import c12306 as api
from .search import build_combo, station_name, parse_date, city_group, consume_ordered
from .cancel import check as check_stop, SearchCancelled

TYPE_MAP = {"G": "高铁", "C": "城际", "D": "动车", "Z": "直达",
            "T": "特快", "K": "快速"}


def _clock(s):
    try:
        h, m = str(s).split(":")
        h, m = int(h), int(m)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except Exception:
        pass
    return None


def _fmt_clock(m):
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


def _fmt_dur(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def build_network(con):
    """全图建边：每趟车任意两站 i<j 都是一条可乘段.

    时长用“逐跳时钟差累加”推导（每跳<24h恒成立），跨天/超24h自然正确，
    不依赖库里有没有存时长。
    返回 (net, n_trains)：net[from] = [(dep_clock, dur, train_no, code, to)].
    """
    net = defaultdict(list)
    n_trains = 0
    cur, acc = None, []
    rows = con.execute(
        "SELECT s.train_no, t.code, s.seq, s.station, s.arrive, s.depart"
        " FROM stops s JOIN trains t ON t.train_no=s.train_no"
        " ORDER BY s.train_no, s.seq").fetchall()

    def flush():
        if len(acc) >= 2:
            deps, arrs = [], []
            for (_st, arr, dep) in acc:
                c = _clock(dep) if dep and dep != "----" else None
                if c is None:
                    c = _clock(arr) if arr and arr != "----" else None
                deps.append(c)
                a = _clock(arr) if arr and arr != "----" else None
                arrs.append(a if a is not None else c)
            if all(c is not None for c in deps) and all(c is not None for c in arrs):
                cum_dep, cum_arr = [0], [0]
                for k in range(1, len(deps)):
                    # 下车时刻 = 上一站发出 + 本跳到达差（逐跳<24h恒成立）
                    cum_arr.append(cum_dep[k - 1] + (arrs[k] - deps[k - 1]) % 1440)
                    cum_dep.append(cum_dep[k - 1] + (deps[k] - deps[k - 1]) % 1440)
                tno, tcode = acc_code[0]
                sts = [s for (s, _a, _d) in acc]
                for i in range(len(acc)):
                    for j in range(i + 1, len(acc)):
                        dur = cum_arr[j] - cum_dep[i]
                        if dur <= 0 or dur > 3 * 1440:
                            continue
                        net[sts[i]].append((deps[i], dur, tno, tcode, sts[j]))
                return True
        return False

    acc_code = [None]
    for r in rows:
        if r["train_no"] != cur:
            if cur is not None and flush():
                n_trains += 1
            cur, acc = r["train_no"], []
            acc_code[0] = (r["train_no"], r["code"] or "")
        acc.append((r["station"], r["arrive"], r["depart"]))
    if cur is not None and flush():
        n_trains += 1
    return net, n_trains


# ---------- 建图缓存：图是“库文件的函数”，库没变就不用重建 ----------
_NET_CACHE = {"key": None, "net": None, "n": 0}
_NET_LOCK = threading.Lock()


def _db_signature(con):
    """(库路径, mtime, 车次数, 停站数) —— 任何一个变了就重建图."""
    try:
        row = con.execute("SELECT COUNT(*) c FROM trains").fetchone()
        n_trains = row[0] if row else -1
        row = con.execute("SELECT COUNT(*) c FROM stops").fetchone()
        n_stops = row[0] if row else -1
    except Exception:
        n_trains = n_stops = -1
    path, mtime = None, 0
    try:
        for r in con.execute("PRAGMA database_list").fetchall():
            if r[1] == "main" and r[2]:
                path = os.path.abspath(r[2])
                try:
                    mtime = os.path.getmtime(path)
                except Exception:
                    mtime = 0
                break
    except Exception:
        pass
    return (path, mtime, n_trains, n_stops)


def cached_network(con):
    """带缓存的 build_network：同一份库反复查询只建一次图（约0.5~1秒/7000趟）。

    爬虫入库后 mtime/行数变化，签名对不上自动重建，不会读到旧图。
    :memory: 等无路径的库不做缓存（每次新建，保证测试互不污染）。
    """
    sig = _db_signature(con)
    if sig[0] is None:
        return build_network(con)
    with _NET_LOCK:
        if _NET_CACHE["key"] == sig:
            return _NET_CACHE["net"], _NET_CACHE["n"]
    net, n = build_network(con)
    with _NET_LOCK:
        _NET_CACHE["key"], _NET_CACHE["net"], _NET_CACHE["n"] = sig, net, n
    return net, n


def invalidate_network_cache():
    """显式丢弃建图缓存（爬完一批想立刻用新图时可调；签名机制通常已自动生效）."""
    with _NET_LOCK:
        _NET_CACHE["key"] = None
        _NET_CACHE["net"] = None
        _NET_CACHE["n"] = 0


def _abs_minutes(d, clock_min):
    """(date, 当日分钟) -> 绝对分钟数（跨天可直接相减，纯整数运算）."""
    return d.toordinal() * 1440 + clock_min


def _from_abs(abs_min):
    """绝对分钟数 -> datetime（只在产出结果时调用，避免每次扩展都建对象）."""
    return datetime.combine(date.fromordinal(abs_min // 1440),
                            dtime(0, 0)) + timedelta(minutes=abs_min % 1440)


def _materialize(path):
    """内部用整数分钟的 path -> 对外带 datetime 的 path（接口不变）."""
    return tuple((tno, code, f, dst, _from_abs(dep), _from_abs(arr))
                 for (tno, code, f, dst, dep, arr) in path)


def _reachable_to(net, targets):
    """反向可达性：返回“能（不限时间地）到达 targets 任意一站”的站集合.

    纯结构剪枝，与时刻无关，因此严格安全：不在集合里的站无论怎么换乘都到不了目标，
    扩展它纯属浪费（冷门长线如 广州->拉萨 45 万次扩展只出 1 条，就是这么白跑的）。
    含 targets 自身；只沿“站->站”的边反向走，不做时间判断。
    """
    rev = defaultdict(set)
    for frm_st, legs in net.items():
        for (_dep_c, _dur, _tno, _tcode, dst) in legs:
            rev[dst].add(frm_st)
    seen = set(targets)
    stack = list(targets)
    while stack:
        cur = stack.pop()
        for prev in rev.get(cur, ()):
            if prev not in seen:
                seen.add(prev)
                stack.append(prev)
    return seen


def graph_search(con, frm, too, dates, min_gap=20, max_gap=720, max_transfers=4,
                 max_total=5 * 1440, limit=300, on_progress=None, _net=None,
                 should_stop=None):
    """时间依赖 Dijkstra 式枚举：按到达最早弹出，全程绝对时间.

    dates: 出发日期串列表（支持前后天）。limit: 最多返回 itineraries 条。
    返回 [path]，path = ((train_no, code, frm, dst, dep_dt, arr_dt), ...)，
    全局按到达时间升序（堆弹出的天然顺序）。

    性能：内部全部用“绝对分钟整数”运算（原来是 datetime + timedelta，
    实测 1150 万次 total_seconds 占了 40% 时间），只在产出结果时转 datetime；
    同城展开的边表按站缓存；入堆前先做分桶剪枝，避免把注定被丢的节点塞进堆。
    """
    pg = on_progress or (lambda f, m: None)
    pg(0.02, "正在建图…")
    net, n_trains = _net if _net is not None else cached_network(con)
    pg(0.15, f"图上{n_trains}趟车，开始扩展…")
    too_group = city_group(too)
    base_dates = [parse_date(d) for d in dates]
    pq = []
    cnt = 0
    # 种子按同城展开：查“广州”必须含广州南/广州白云发的车（对齐在线同城通查）
    for s0 in city_group(frm):
        for d in base_dates:
            base = _abs_minutes(d, 0)
            for (dep_c, dur, tno, tcode, dst) in net.get(s0, []):
                if dur > max_total:
                    continue
                dep_abs = base + dep_c
                arr_abs = dep_abs + dur
                heapq.heappush(pq, (arr_abs, 0, cnt,
                                    ((tno, tcode, s0, dst, dep_abs, arr_abs),),
                                    (s0, dst)))
                cnt += 1
    best = {}
    results, seen_codes = [], set()
    expansions = 0
    exp_cap = max(400000, limit * 1500)  # 上限跟着枚举数走，不卡大数据
    legs_cache = {}  # 站 -> (按发车钟点排序的边, 发车钟点数组)
    # 反向可达性预剪：到不了目标站的节点不再扩展（纯结构判断，不影响结果）
    try:
        can_reach = _reachable_to(net, too_group)
    except Exception:
        can_reach = None

    def expanded(st):
        """同城展开后的边，按发车钟点排序，供二分只取等待窗内的车。

        原来每次扩展都要把该站两三百条边全扫一遍、逐条试 4 天，
        这是 25 秒耗时的主要来源；排序后只扫窗口内的十几条。
        """
        got = legs_cache.get(st)
        if got is None:
            rows = []
            for s0 in city_group(st):
                pen = 0 if s0 == st else 60
                for (dep_c, dur, tno, tcode, dst) in net.get(s0, []):
                    if can_reach is not None and dst not in can_reach:
                        continue  # 结构上到不了目标站，扩展它没有意义
                    rows.append((dep_c, dur, tno, tcode, dst, pen, s0))
            rows.sort(key=lambda x: x[0])  # 稳定排序：同钟点保持原车次顺序
            got = (rows, [r[0] for r in rows])
            legs_cache[st] = got
        return got

    while pq and len(results) < limit and expansions < exp_cap:
        arr_abs, k, _, path, stations = heapq.heappop(pq)
        expansions += 1
        if expansions % 5000 == 0:
            check_stop(should_stop)
        if expansions % 50000 == 0:
            pg(0.15 + 0.55 * min(1, expansions / exp_cap),
               f"已扩展{expansions}个节点，找到{len(results)}条…")
        st = path[-1][3]
        if st in too_group:
            key = (tuple(l[1] for l in path), path[0][4] // 1440)
            if key not in seen_codes:
                seen_codes.add(key)
                results.append(_materialize(path))
            continue
        if k >= max_transfers:
            continue
        # 分桶剪枝：同站同换乘数，按到达（天，小时桶）分别保留最早到达。
        # 桶不能太大：早班车06:31到会把中午13:27到的G1272整个剪掉，
        # 可人家接的是下午的快车（唐山-费县案的教训）。1小时桶是兼顾开销的 sweet spot。
        bucket = (st, k, arr_abs // 1440, (arr_abs % 1440) // 60)
        if best.get(bucket) is not None and arr_abs >= best[bucket]:
            continue
        best[bucket] = arr_abs
        t0 = path[0][4]
        cur_train = path[-1][0]
        legs, clocks = expanded(st)
        # 等待窗 [arr+min_gap+pen, arr+max_gap+pen]（pen=60 给同城不同站）。
        # 窗口最长 1500 分钟，所以跨的“日切片”最多 2 个；每个切片内用二分
        # 取发车钟点落在窗口里的车，再逐条按绝对时间算。
        for pen in (0, 60):
            lo = arr_abs + min_gap + pen
            hi = arr_abs + max_gap + pen
            for dd in range(lo // 1440, hi // 1440 + 1):
                dbase = dd * 1440
                a = max(lo, dbase) - dbase
                b = min(hi, dbase + 1439) - dbase
                if a > b:
                    continue
                i = bisect_left(clocks, a)
                j = bisect_right(clocks, b)
                for idx in range(i, j):
                    dep_c, dur, tno2, tcode2, dst2, pen2, dep_st = legs[idx]
                    if pen2 != pen:
                        continue
                    if tno2 == cur_train:
                        continue  # 同车坐过站不算换乘（直达边已存在，不丢信息）
                    if dst2 in stations:
                        continue
                    dep_abs = dbase + dep_c
                    arr2 = dep_abs + dur
                    if arr2 - t0 > max_total:
                        continue
                    cnt += 1
                    heapq.heappush(pq, (arr2, k + 1, cnt,
                                        path + ((tno2, tcode2, dep_st, dst2, dep_abs, arr2),),
                                        stations + (dst2,)))
    pg(1.0, f"枚举完成：{len(results)}条候选")
    # 直达强制入围：慢直达（如Z14次日到）按到达排序永远进不了前N，
    # 但它们往往是最便宜的，单独补上（才几十条）。
    for d in base_dates:
        base = _abs_minutes(d, 0)
        for s0 in city_group(frm):
            for (dep_c, dur, tno, tcode, dst) in net.get(s0, []):
                if dst not in too_group or dur > max_total:
                    continue
                dep_abs = base + dep_c
                key = ((tcode,), dep_abs // 1440)
                if key not in seen_codes:
                    seen_codes.add(key)
                    results.append(_materialize(((tno, tcode, s0, dst, dep_abs,
                                                  dep_abs + dur),)))
    return results


def pick_verify(combos, top):
    """核验名额按换乘数轮询：直达/1/2/3/4次轮流取，保证便宜慢车也有份."""
    by_k = {}
    for c in combos:
        by_k.setdefault(c["transfers"], []).append(c)
    todo, i = [], 0
    while len(todo) < top:
        added = False
        for k in sorted(by_k):
            if i < len(by_k[k]) and len(todo) < top:
                todo.append(by_k[k][i])
                added = True
        if not added:
            break
        i += 1
    return todo


def path_to_combo(path):
    legs, waits = [], []
    for i, (tno, tcode, frm, dst, dep_dt, arr_dt) in enumerate(path):
        if i:
            waits.append(int((dep_dt - path[i - 1][5]).total_seconds() // 60))
        dur = int((arr_dt - dep_dt).total_seconds() // 60)
        legs.append({
            "train_no": tno, "code": tcode,
            "type_letter": tcode[0] if tcode else "O",
            "train_type": TYPE_MAP.get(tcode[0] if tcode else "", "其他"),
            "from_code": frm, "to_code": dst,
            "start_time": _fmt_clock(dep_dt.hour * 60 + dep_dt.minute),
            "arrive_time": _fmt_clock(arr_dt.hour * 60 + arr_dt.minute),
            "duration": _fmt_dur(dur), "duration_min": dur,
            "from_no": "", "to_no": "", "seat_types": "",
            "seats": {}, "price": None, "price_name": None,
            "_dep": dep_dt, "_arr": arr_dt,
            "dep_date": dep_dt.date().isoformat(),
            "arr_date": arr_dt.date().isoformat(),
        })
    return build_combo(legs, [None] * len(legs), waits)


def verify_combos(combos, on_progress=None, workers=4, should_stop=None):
    """对候选做实时核验：分段查余票找同车次，补席位+票价；对不上就丢掉."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    pg = on_progress or (lambda f, m: None)
    keys, order = {}, []
    for ci, c in enumerate(combos):
        for li, l in enumerate(c["legs"]):
            k = (l["from_code"], l["to_code"], l["dep_date"])
            if k not in keys:
                keys[k] = []
                order.append(k)
            keys[k].append((ci, li, l["train_no"]))

    def one(key):
        f, t, d = key
        try:
            rows, _ = api.query_tickets(f, t, d, retries=1)
        except Exception:
            return key, {}, True
        return key, {r["train_no"]: r for r in rows}, False

    found, n_err, n_dead = {}, 0, 0
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(one, k): k for k in order}
        done = 0
        for fu in consume_ordered(futs, should_stop):
            key, m, err = fu.result()
            found[key] = m
            n_err += 1 if err else 0
            done += 1
            if done % 3 == 0 or done >= len(order):
                pg(done / max(1, len(order)), f"核验 {done}/{len(order)} 段…")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    price_cache, verified = {}, []
    for c in combos:
        check_stop(should_stop)
        ok = True
        for l in c["legs"]:
            row = found.get((l["from_code"], l["to_code"], l["dep_date"]), {}).get(l["train_no"])
            if row is None:
                ok = False
                break
            ck = (l["train_no"], row["from_no"], row["to_no"], l["dep_date"])
            if ck not in price_cache:
                try:
                    data = api.cached_query_price(l["train_no"], row["from_no"], row["to_no"],
                                                  row.get("seat_types", ""), l["dep_date"])
                    prefer = "二等" if row.get("type_letter") in ("G", "C", "D") else "硬座"
                    price_cache[ck] = api.cheapest_price(data, prefer=prefer)
                except Exception:
                    price_cache[ck] = (None, None)
            p, pn = price_cache[ck]
            l["price"], l["price_name"] = p, pn
            l["seats"] = row.get("seats", {})
            l["from_no"], l["to_no"] = row.get("from_no", ""), row.get("to_no", "")
        if not ok:
            n_dead += 1
            continue
        c["total_price"] = (sum(l["price"] for l in c["legs"])
                            if all(l.get("price") is not None for l in c["legs"]) else None)
        c["has_price"] = c["total_price"] is not None
        verified.append(c)
    info = {"checked": len(order), "errors": n_err, "dead": n_dead,
            "error_rate": n_err / max(1, len(order))}
    return verified, info


def run_graph_search(from_kw, to_kw, date, max_transfers=2, min_gap=20, max_gap=720,
                     max_total=5 * 1440, limit=300, verify_top=40, window=True,
                     must_middle="", on_progress=None, db_path=None, should_stop=None):
    """一站式：离线枚举（0→25%）+ 实时核验头部（25%→100%），返回和 search_combos 同形 dict."""
    from . import graphdb
    pg = on_progress or (lambda f, m: None)
    api.reset_req_count()
    from_name, from_code = api.resolve_station(from_kw)
    to_name, to_code = api.resolve_station(to_kw)
    if not from_code or not to_code:
        return {"error": f"未找到车站: {from_kw}->{to_kw}", "combos": []}
    if from_code == to_code:
        return {"error": "出发到达是同一站", "combos": []}
    check_stop(should_stop)
    try:
        api.load_stations()
    except Exception:
        pass
    base = parse_date(date)
    dates = [base.isoformat()]
    if window:
        dates = [(base + timedelta(days=-1)).isoformat(), base.isoformat(),
                 (base + timedelta(days=1)).isoformat()]
    con = graphdb.connect(db_path)
    try:
        paths = graph_search(con, from_code, to_code, dates, min_gap=min_gap,
                             max_gap=max_gap, max_transfers=max_transfers,
                             max_total=max_total, limit=limit,
                             on_progress=lambda f, m: pg(f * 0.2, "枚举：" + m),
                             should_stop=should_stop)
    finally:
        con.close()
    combos = [path_to_combo(p) for p in paths]
    from .search import enrich_names
    # 图里存了站名（stops.name），优先用它补名：站名库降级时也不会显示 TAK/TMK
    try:
        gnames = {}
        con2 = graphdb.connect(db_path)
        try:
            for r in con2.execute("SELECT DISTINCT station, name FROM stops"
                                  " WHERE name IS NOT NULL AND name<>''"):
                gnames[r["station"]] = r["name"]
        finally:
            con2.close()
    except Exception:
        gnames = {}
    enrich_names(combos, {}, graph_names=gnames)
    n_cand = len(combos)
    if must_middle and str(must_middle).strip():
        mname, mcode = api.resolve_station(str(must_middle).strip())
        if mcode:
            combos = [c for c in combos
                      if any(mcode in (l.get("from_code"), l.get("to_code")) or
                             str(must_middle).strip() in (l.get("from_name", "") + l.get("to_name", ""))
                             for l in c["legs"])]
    todo = pick_verify(combos, max(1, verify_top))

    def vpg(f, m):
        pg(0.2 + f * 0.5, m)

    verified, vinfo = verify_combos(todo, on_progress=vpg, should_stop=should_stop)
    # 实时兜底：图里翻出来但核验成活太少（且不是被限流杀的），就跑一轮在线搜索合并。
    # 两种算法找路原理完全不同，并集 strictly 更全——“只有中间段”类投诉的终极保险。
    live_added, live_warn = 0, ""
    if len(verified) < 20 and vinfo["error_rate"] < 0.3:
        try:
            from .search import search_combos as live_search
            pg(0.7, "图里太少，实时补查…")
            live = live_search(from_kw, to_kw, date, max_transfer=min(2, max_transfers),
                               min_gap=min_gap, max_gap=max_gap, limit=30, with_price=True,
                               must_middle=must_middle, breadth="normal",
                               on_progress=lambda f, m: pg(0.7 + f * 0.25, "实时补查：" + m),
                               should_stop=should_stop)
            seen_codes = {c["codes"] for c in verified}
            for c in live.get("combos", []):
                if c["codes"] not in seen_codes:
                    seen_codes.add(c["codes"])
                    verified.append(c)
                    live_added += 1
        except SearchCancelled:
            raise
        except Exception as e:
            live_warn = str(e)[:80]
    verified.sort(key=lambda c: ((c["total_price"] is None), c["total_price"] or 9e9,
                                 c["total_min"]))
    middles, seen_m = [], set()
    for c in verified:
        for l in c["legs"][:-1]:
            code = l.get("to_code")
            if code and code not in seen_m and code not in (from_code, to_code):
                seen_m.add(code)
                middles.append({"name": l.get("to_name") or code, "code": code})
    out = {
        "from": {"name": from_name, "code": from_code},
        "to": {"name": to_name, "code": to_code},
        "date": date,
        "direct_count": sum(1 for c in verified if c["transfers"] == 0),
        "combos": verified,
        "middles": middles,
        "graph": True,
        "candidates": n_cand,
        "verified": len(todo),
        "live_added": live_added,
        "live_warn": live_warn,
        "stats": {"requests": api.get_req_count(), "middles": len(middles),
                  "combos": len(verified)},
    }
    pg(1.0, "完成")
    return out

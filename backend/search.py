"""中转搜索：1-2次换乘枚举 + 时间可行性剪枝 + 票价聚合.

日期模型（隔天修复后）：所有车次都锚定到绝对日期时间。
首段按出行日期 D 查询发车，第二段按 D 与 D+1 查询，用绝对时间做衔接，
总时长 = 末段到达 - 首段出发。不再用钟表时间加减。
"""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, time as dtime
from . import c12306 as api
from . import geo
from .cancel import check as check_stop


def consume_ordered(futs, should_stop):
    """逐个产出完成的 future；收到停止信号时取消未开始的并抛 SearchCancelled."""
    rest = set(futs)
    while rest:
        check_stop(should_stop)
        try:
            for f in as_completed(rest, timeout=1.0):
                rest.discard(f)
                yield f
        except TimeoutError:
            continue

# 同城站分组（换乘惩罚 + 中转候选去重用）
SAME_CITY = [
    {"BJP", "VNP", "BXP", "VAP", "IFP", "FTP"},  # 北京
    {"SHH", "SNH", "AOH", "SXH"},                # 上海
    {"GZQ", "IZQ", "GGQ", "GBA"},              # 广州/南/东/白云（GBA不同站短接要罚60分钟）
    {"CDW", "ICW", "CNW"},                      # 成都
    {"CQW", "CUW", "CQK"},                      # 重庆
    {"XAY", "EAY"},                             # 西安
    {"HZH", "HGH", "HHC"},                      # 杭州
    {"NJH", "NKH"},                             # 南京
    {"CSQ", "CTQ"},                             # 长沙/长沙南（缺失曾导致长沙南被当成中转站）
    {"ZZF", "ZAF"},                             # 郑州/郑州东
    {"WHN", "HKN", "WCN"},                      # 武汉/汉口/武昌
    {"HFH", "ENH"},                             # 合肥/合肥南
    {"JNK", "JGK"},                             # 济南/济南西
    {"TJP", "TIP"},                             # 天津/天津南
    {"TYV", "TNV"},                             # 太原/太原南
    {"SZQ", "IOQ"},                             # 深圳/深圳北
    {"SZH", "OHH"},                             # 苏州/苏州北
    {"WXH", "WGH"},                             # 无锡/无锡东
    {"XCH", "UUH"},                             # 徐州/徐州东
    {"SYT", "SBT"},                             # 沈阳/沈阳北
    {"NCG", "NXG"},                             # 南昌/南昌西
    {"GIW", "KQW"},                             # 贵阳/贵阳北
    {"KMM", "KXM"},                             # 昆明/昆明南
    {"LZJ", "LAJ"},                             # 兰州/兰州西
    {"TMK", "TAK"},                             # 泰安/泰山（唐山-费县案：K 到泰山、G 到泰安）
    {"TSP", "FUP"},                             # 唐山/唐山北（Z14到唐山北，查唐山必须认）
]

def same_city(a, b):
    if a == b:
        return True
    for g in SAME_CITY:
        if a in g and b in g:
            return True
    return False


def city_group(code):
    """站码 -> 同城站码集合（含自己）。图搜用它展开种子/换乘/到达，
    对齐在线查询的同城通查（否则广州南/广州白云发的车永远搜不到）。"""
    for g in SAME_CITY:
        if code in g:
            return set(g)
    return {code}

def hm_to_min(hm):
    try:
        h, m = hm.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


def add_days(date_str, n):
    return (parse_date(date_str) + timedelta(days=n)).isoformat()


def anchor(train, base_date_str):
    """把车次锚定到绝对时间：dep = 查询日期+发车时刻，arr = dep + 历时.

    历时 lishi 是权威跨天依据（如 19:10->08:44 历时 13:34），
    比“到达钟点 < 出发钟点则+1天”更可靠（>24h 行程也能处理）。
    返回携带 _dep/_arr（内部 datetime，不入 JSON）的新 dict。
    """
    t = dict(train)
    base = parse_date(base_date_str)
    try:
        h, m = train["start_time"].split(":")
        dep = datetime.combine(base, dtime(int(h), int(m)))
    except Exception:
        dep = datetime.combine(base, dtime(0, 0))
    dur = train.get("duration_min") or 0
    try:
        dur = int(dur)
    except Exception:
        dur = 0
    arr = dep + timedelta(minutes=dur)
    t["_dep"] = dep
    t["_arr"] = arr
    t["dep_date"] = base.isoformat()
    t["arr_date"] = arr.date().isoformat()
    t["arr_plus"] = (arr.date() - base).days  # 相对本段查询日期的跨天数
    return t


def strip_internal(leg):
    """去掉 _ 开头内部键，保证 JSON 可序列化."""
    return {k: v for k, v in leg.items() if not k.startswith("_")}


def need_next_day(a_legs, max_gap):
    """首段中是否存在晚到车，需要把次段查到 D+1.

    条件：某段到达已跨天，或 到达钟点+max_gap 跨过午夜
    （此时次日早班车仍在换乘窗口内）。
    """
    for a in a_legs:
        try:
            if a["_arr"].date() > a["_dep"].date():
                return True
            if hm_to_min(a["arrive_time"]) + max_gap >= 1440:
                return True
        except Exception:
            continue
    return False

def station_name(code, smap=None):
    """电报码 -> 中文站名（余票接口自带 map 优先，本地 3384 站兜底）."""
    if smap and code in smap:
        return smap[code]
    try:
        s = api._stations_cache.get("by_code", {}).get(code)
        if s:
            return s["name"]
    except Exception:
        pass
    return code


def enrich_names(combos, smap):
    for c in combos:
        for l in c["legs"]:
            l["from_name"] = station_name(l.get("from_code", ""), smap)
            l["to_name"] = station_name(l.get("to_code", ""), smap)


# 搜索广度档位：中转是自己组的，规模越大票越多，但打 12306 次数越多、越慢、越容易限流
BREADTHS = {
    "quick":  {"middles": 8,  "n_each": 2, "per_train": 3, "a_sample": 8,
               "per_a": 3, "cap1": 250, "cap2": 400, "pairs2": 8,
               "always_next": False, "label": "快速"},
    "normal": {"middles": 12, "n_each": 3, "per_train": 4, "a_sample": 10,
               "per_a": 4, "cap1": 400, "cap2": 600, "pairs2": 12,
               "always_next": False, "label": "标准"},
    "deep":   {"middles": 20, "n_each": 5, "per_train": 6, "a_sample": 16,
               "per_a": 6, "cap1": 800, "cap2": 1200, "pairs2": 20,
               "always_next": True, "label": "地毯式"},
}


def leg_price(train, date, cache):
    key = (train["train_no"], train["from_no"], train["to_no"], date)
    if key in cache:
        return cache[key]
    try:
        data = api.cached_query_price(train["train_no"], train["from_no"], train["to_no"], train.get("seat_types", ""), date)
        # 高铁系看二等、普速看硬座：与最低价基本同价，但席别名更符合直觉
        prefer = "二等" if train.get("type_letter") in ("G", "C", "D") else "硬座"
        best, name = api.cheapest_price(data, prefer=prefer)
        cache[key] = (best, name)
        return best, name
    except Exception:
        cache[key] = (None, None)
        return None, None

# 枢纽兜底池：大枢纽（车多稳）+ 地级市（顺路准），走廊排序后取前 N 个试。
# 顺序已不重要（geo.rank_hubs 动态重排），只管覆盖。解析不到站名的会被跳过。
HUBS = ["郑州", "武汉", "长沙", "西安", "合肥", "南京", "徐州", "石家庄", "济南", "泰安", "洛阳", "南昌", "杭州", "天津", "太原", "宝鸡", "襄阳", "怀化", "桂林", "贵阳", "成都", "重庆", "曲阜东", "临沂"]
MID_CITIES = ["廊坊", "秦皇岛", "保定", "沧州", "衡水", "承德", "大同", "朔州", "忻州",
              "阳泉", "晋中", "长治", "临汾", "运城", "渭南", "三门峡", "开封", "濮阳",
              "聊城", "菏泽", "济宁", "枣庄", "兖州", "日照", "淄博", "潍坊", "烟台",
              "商丘", "许昌", "漯河", "驻马店", "周口", "焦作", "信阳", "随州", "荆门",
              "岳阳", "常德", "益阳", "萍乡", "新余", "鹰潭", "宜春", "吉安", "抚州",
              "镇江", "常州", "滁州", "九江", "芜湖", "赣州", "衡阳", "郴州", "安庆",
              "嘉兴", "绍兴", "韶关", "肇庆", "惠州", "佛山", "柳州", "遵义", "绵阳",
              "德阳", "南充", "宜宾", "乐山", "十堰", "安康", "榆林", "延安", "天水",
              "定西", "武威", "张掖", "酒泉", "吴忠", "中卫", "包头", "鄂尔多斯",
              "吉林", "四平", "牡丹江", "佳木斯", "大庆", "齐齐哈尔", "抚顺", "丹东",
              "锦州", "营口", "通辽", "赤峰", "张家口", "宁波", "温州", "泉州"]

def candidate_middles(from_code, to_code, date, direct_trains, limit=12, per_train=4, n_each=3,
                      hub_order=None):
    """候选中转站：直达车经停站交集优先，枢纽按走廊排序兜底.

    按车型分组取源（高铁系/普速系交错）：之前取 direct[:6]，若前 6 趟同质化
    （如京长线全是 K），混搭组合永远出不来——实查 B 组抓到的结构性 bug。
    每源车最多贡献 per_train 站，避免单个车塞满名额。
    名额预留制（广州-唐山案的教训）：经停站再多也只能占一半名额，
    剩下的留给图库和走廊枢纽——否则直达全是慢车时，快线枢纽一个进不来。
    """
    cands = []
    seen = set()
    route_cap = max(3, limit // 2)
    graph_cap = max(2, limit // 4)
    trains = direct_trains or []
    fast = [t for t in trains if t.get("type_letter") in ("G", "C", "D")]
    slow = [t for t in trains if t.get("type_letter") not in ("G", "C", "D")]
    src = []
    for i in range(n_each):
        if i < len(fast):
            src.append(fast[i])
        if i < len(slow):
            src.append(slow[i])
    for t in trains[:n_each * 2]:
        if t not in src:
            src.append(t)
    # 1) 从直达车经停站里找（同一线路天然顺路，不绕路）
    # 注意：czxx/queryByTrainNo 只返 station_name，不返 telecode，需本地解析
    # 每源车最多贡献 per_train 站：否则首个 G 车十几站直接塞满名额，普速源没机会
    # （实查 B2：12 个名额全是X东，K 站一个没进）
    for t in src:
        if len(cands) >= route_cap:
            break
        added = 0
        try:
            route = api.cached_query_route(t["train_no"], t["from_code"], t["to_code"], date)
            stops = route.get("data", []) if isinstance(route, dict) and "data" in route else []
            lst = stops if isinstance(stops, list) else []
            for s in lst:
                if len(cands) >= route_cap or added >= per_train:
                    break
                if not isinstance(s, dict):
                    continue
                # 只取查询区间内的停靠站（isEnabled=True），跳过起终点
                if s.get("isEnabled") is False:
                    continue
                name = (s.get("station_name") or "").strip()
                if not name:
                    continue
                rname, code = api.resolve_station(name)
                if not code or code in (from_code, to_code) or code in seen:
                    continue
                # 跳过与起终点同城的站（如北京 vs 北京南），避免无意义短接
                if same_city(code, from_code) or same_city(code, to_code):
                    continue
                seen.add(code)
                cands.append({"name": rname or name, "code": code})
                added += 1
        except Exception:
            continue
    # 1b) 时刻表图：在“A下游”且在“B上游”的站（爬虫攒的图，有货才上）。
    # 地位：在经停站之后、静态枢纽之前——图里的是真实跑出来的衔接。
    if len(cands) < route_cap + graph_cap:
        try:
            from . import graphdb
            gcon = graphdb.connect()
            try:
                gmids = graphdb.common_middles(gcon, from_code, to_code, limit=graph_cap)
            finally:
                gcon.close()
            for code in gmids:
                if len(cands) >= route_cap + graph_cap or code in seen:
                    continue
                if code in (from_code, to_code):
                    continue
                if same_city(code, from_code) or same_city(code, to_code):
                    continue
                seen.add(code)
                cands.append({"name": station_name(code) or code, "code": code})
        except Exception:
            pass
    # 2) 枢纽兜底（按走廊排序，谁顺路谁先上；无坐标时保持静态顺序）
    for h in (hub_order or HUBS):
        if len(cands) >= limit:
            break
        name, code = api.resolve_station(h)
        if not code or code in (from_code, to_code) or code in seen:
            continue
        if same_city(code, from_code) or same_city(code, to_code):
            continue
        seen.add(code)
        cands.append({"name": name, "code": code})
    return cands

def combine_legs(a, b, min_gap=20, max_gap=240):
    """两段是否可衔接（绝对时间），返回等待分钟数或 None.

    要求 b 必须在 a 到达之后发车。D+1 的次段已作为独立候选查回，
    这里不再做“钟点早则+1440”的猜测——猜是之前隔天 bug 的根源。
    """
    try:
        wait = int((b["_dep"] - a["_arr"]).total_seconds() // 60)
    except Exception:
        return None
    if wait < 0:
        return None
    # 同城不同站惩罚60分钟
    penalty = 0
    if a["to_code"] != b["from_code"]:
        if same_city(a["to_code"], b["from_code"]):
            penalty = 60
        else:
            return None
    wait_eff = wait - penalty
    if wait_eff < min_gap or wait > max_gap + penalty:
        return None
    return wait


# 直达车次中文分类（"直达"车本身就叫直达，直接拼接会成"直达直达"）
DIRECT_KIND = {"高铁": "直达高铁", "城际": "直达城际", "动车": "直达动车",
               "直达": "直达特快", "特快": "直达特快", "快速": "直达快速",
               "其他": "直达"}


def spread_sample(legs, n):
    """全天均匀采样 n 段：只取最早 N 趟会漏掉下午/晚上的衔接（西安-太原案）。"""
    if len(legs) <= n:
        return list(legs)
    s = sorted(legs, key=lambda x: x["_dep"])
    step = len(s) / n
    return [s[int(i * step)] for i in range(n)]


def pair_legs(list_a, list_b, min_gap, max_gap, per_a=4):
    """对每段 a，在其到达之后找最早的若干可行 b（B 按发车排序，等待单调增可早停）."""
    pairs = []
    Bs = sorted(list_b, key=lambda x: x["_dep"])
    for a in list_a:
        take = 0
        try:
            arr = a["_arr"]
        except Exception:
            continue
        for b in Bs:
            try:
                gap = (b["_dep"] - arr).total_seconds() // 60
            except Exception:
                continue
            if gap < 0:
                continue
            if gap > max_gap + 60:
                break
            w = combine_legs(a, b, min_gap, max_gap)
            if w is None:
                continue
            pairs.append((a, b, w))
            take += 1
            if take >= per_a:
                break
    return pairs


def build_combo(legs, prices, wait_list):
    total_price = sum(p for p in prices if p) if all(p for p in prices) else None
    # 总时长：末段到达 - 首段出发（绝对时间，跨天/超24h 正确）
    try:
        total_min = int((legs[-1]["_arr"] - legs[0]["_dep"]).total_seconds() // 60)
    except Exception:
        first, last = legs[0], legs[-1]
        total_min = hm_to_min(last["arrive_time"]) - hm_to_min(first["start_time"])
        if total_min < 0:
            total_min += 1440
    base_day = legs[0]["_dep"].date()
    pub_legs = []
    for l in legs:
        c = strip_internal(l)
        try:
            c["dep_plus"] = (l["_dep"].date() - base_day).days  # 相对行程首日
            c["arr_plus"] = (l["_arr"].date() - base_day).days
        except Exception:
            c["dep_plus"] = 0
            c["arr_plus"] = 0
        pub_legs.append(c)
    codes = "+".join(l["code"] for l in legs)
    kinds = set(l["type_letter"] for l in legs)
    if kinds <= {"G", "C"}:
        combo_kind = "全高铁"
    elif kinds & {"G", "C", "D"} and kinds & {"Z", "T", "K"}:
        combo_kind = "高铁+普速"
    elif kinds <= {"D"} or kinds <= {"G", "C", "D"}:
        combo_kind = "动车系"
    else:
        combo_kind = "全普速"
    return {
        "legs": pub_legs,
        "total_price": total_price,
        "total_min": total_min,
        "waits": wait_list,
        "transfers": len(legs) - 1,
        "codes": codes,
        "combo_kind": combo_kind,
        "has_price": total_price is not None,
        "dep_date": legs[0]["_dep"].date().isoformat(),
        "arr_date": legs[-1]["_arr"].date().isoformat(),
    }

def search_combos(from_kw, to_kw, date, max_transfer=1, min_gap=20, max_gap=240, limit=30, with_price=True,
                  must_middle="", breadth="normal", on_progress=None, should_stop=None):
    cfg = BREADTHS.get(breadth) or BREADTHS["normal"]
    api.reset_req_count()
    check_stop(should_stop)

    def pg(frac, msg):
        if on_progress:
            on_progress(frac, msg)

    from_name, from_code = api.resolve_station(from_kw)
    to_name, to_code = api.resolve_station(to_kw)
    if not from_code or not to_code:
        return {"error": f"未找到车站: {from_kw}->{to_kw}", "combos": [], "direct": []}
    if must_middle and str(must_middle).strip() and max_transfer < 1:
        max_transfer = 1  # 指定了中转站就至少查 1 次换乘
    try:
        direct, smap, tinfo = api.cached_query_tickets(from_code, to_code, date)
    except Exception as e:
        # 失败先服役同条件旧缓存（时刻是准的，余票可能滞后），连旧缓存都没有才给演示数据
        stale = api.stale_tickets(from_code, to_code, date)
        if stale is not None:
            direct, smap, at = stale
            tinfo = {"cached": True, "cached_at": at, "stale": True,
                     "warn": f"{e}（展示缓存时刻，余票可能滞后）"}
        else:
            pg(1.0, "12306限流，展示演示数据")
            return mock_result(from_kw, to_kw, date, str(e))
    pg(0.04, f"直达{len(direct)}趟，正在找顺路中转站…")

    price_cache = {}
    if with_price:
        # 直达前 12 个并行查价（串行约 10s+，并行约 3s）
        ex = ThreadPoolExecutor(max_workers=4)
        try:
            futs = [ex.submit(leg_price, t, date, price_cache) for t in direct[:12]]
            for f in consume_ordered(futs, should_stop):
                try:
                    f.result()
                except Exception:
                    pass
        finally:
            ex.shutdown(wait=False, cancel_futures=True)

    direct_out = []
    for t in direct:
        p, pn = price_cache.get((t["train_no"], t["from_no"], t["to_no"], date), (None, None))
        direct_out.append({**t, "price": p, "price_name": pn})

    combos = []
    # 直达也作为0换乘combo加入对比（锚定到 D，跨天信息照样给出）
    for t in direct_out[:20]:
        a = anchor(t, date)
        leg_pub = strip_internal(a)
        leg_pub["dep_plus"] = 0
        leg_pub["arr_plus"] = (a["_arr"].date() - a["_dep"].date()).days
        combos.append({
            "legs": [leg_pub], "total_price": t.get("price"), "total_min": t["duration_min"],
            "waits": [], "transfers": 0, "codes": t["code"],
            "combo_kind": DIRECT_KIND.get(t["train_type"], "直达"), "has_price": t.get("price") is not None,
            "dep_date": date, "arr_date": a["_arr"].date().isoformat(),
        })

    leg_map = {}
    middles = []
    if max_transfer >= 1:
        try:
            hub_order = geo.rank_hubs(from_name, to_name, HUBS + MID_CITIES, prefer=HUBS)
        except Exception:
            hub_order = None
        middles = candidate_middles(from_code, to_code, date, direct,
                                    limit=cfg["middles"], per_train=cfg["per_train"],
                                    n_each=cfg["n_each"], hub_order=hub_order)
        # 用户指定的中转站优先插到最前（西安-太原案：自动候选排不上太原时兜底）；
        # 同城姐妹站一起带上（用户写"太原"时，太原南的 D/G 大概率才是正解）
        if must_middle and str(must_middle).strip():
            mname, mcode = api.resolve_station(str(must_middle).strip())
            if mcode and mcode not in (from_code, to_code):
                wanted = [(mname, mcode)]
                for g in SAME_CITY:
                    if mcode in g:
                        for c in g:
                            if c not in (mcode, from_code, to_code):
                                n = station_name(c)
                                wanted.append((n, c))
                        break
                have = {m["code"] for m in middles}
                middles = ([{"name": n, "code": c} for n, c in wanted if c not in have] +
                           [m for m in middles if m["code"] not in {c for _, c in wanted}])[:12]
        pg(0.08, f"锁定{len(middles)}个中转站，正在查去程分段…")
        date_next = add_days(date, 1)
        # 第一阶段：A 段全部按 D 查询并锚定
        ex = ThreadPoolExecutor(max_workers=4)
        try:
            futs = {ex.submit(safe_query, from_code, m["code"], date): m for m in middles}
            done_a = 0
            for f in consume_ordered(futs, should_stop):
                m = futs[f]
                try:
                    raw = f.result() or []
                except Exception:
                    raw = []
                try:
                    leg_map[("A", m["code"])] = [anchor(t, date) for t in raw]
                except Exception:
                    leg_map[("A", m["code"])] = []
                done_a += 1
                if done_a % 2 == 0 or done_a == len(futs):
                    n_leg = len(leg_map[( "A", m["code"])])
                    m_name = m["name"]
                    pg(0.08 + 0.22 * done_a / max(1, len(futs)),
                       f"查去程分段 {done_a}/{len(futs)}（{m_name} {n_leg}趟）…")
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        # 第二阶段：B 段按 D 查询；晚到中转站（或地毯式）才加查 D+1（省请求、防封）
        need_next = {}
        for m in middles:
            try:
                need_next[m["code"]] = cfg["always_next"] or need_next_day(leg_map.get(("A", m["code"]), []), max_gap)
            except Exception:
                need_next[m["code"]] = cfg["always_next"]
        with ThreadPoolExecutor(max_workers=4) as ex_b:
            futs = {}
            for m in middles:
                futs[ex_b.submit(safe_query, m["code"], to_code, date)] = ("B0", m)
                if need_next.get(m["code"]):
                    futs[ex_b.submit(safe_query, m["code"], to_code, date_next)] = ("B1", m)
            tmp = {}
            done_b = 0
            try:
                for f in consume_ordered(futs, should_stop):
                    side, m = futs[f]
                    try:
                        raw = f.result() or []
                    except Exception:
                        raw = []
                    qd = date_next if side == "B1" else date
                    try:
                        anchored = [anchor(t, qd) for t in raw]
                    except Exception:
                        anchored = []
                    tmp.setdefault(m["code"], []).extend(anchored)
                    done_b += 1
                    if done_b % 2 == 0 or done_b >= len(futs):
                        pg(0.30 + 0.25 * done_b / max(1, len(futs)),
                           f"查回程分段 {done_b}/{len(futs)}…")
            finally:
                ex_b.shutdown(wait=False, cancel_futures=True)
            for m in middles:
                # B 段不按最早截断：pair_legs 按到达锚定取最早可行，全量传入才找得到
                # 下午/晚上的衔接（西安-太原案：截断到前 10 趟早班车导致 0 配对）
                merged = sorted(tmp.get(m["code"], []), key=lambda x: x["_dep"])[:80]
                leg_map[("B", m["code"])] = merged
        # 先配对、再查价（查价最耗时，进度单独上报）
        pairs1 = []
        for m in middles:
            # A 全天采样，B 全量参与锚定配对（不再硬截最早 N 趟）
            check_stop(should_stop)
            list_a = spread_sample(leg_map.get(("A", m["code"]), []), cfg["a_sample"])
            list_b = leg_map.get(("B", m["code"]), [])
            pairs1.extend(pair_legs(list_a, list_b, min_gap, max_gap, per_a=cfg["per_a"]))
            if len(pairs1) > cfg["cap1"] * 2:
                break
        pg(0.58, f"配出{len(pairs1)}对衔接，正在查分段票价…")
        for i, (a, b, w) in enumerate(pairs1):
            check_stop(should_stop)
            pa, pan = leg_price(a, a["dep_date"], price_cache) if with_price else (None, None)
            pb, pbn = leg_price(b, b["dep_date"], price_cache) if with_price else (None, None)
            aa = dict(a); aa["price"] = pa; aa["price_name"] = pan
            bb = dict(b); bb["price"] = pb; bb["price_name"] = pbn
            combos.append(build_combo([aa, bb], [pa, pb], [w]))
            if len(combos) > cfg["cap1"]:
                break
            if i % 10 == 0 or i + 1 >= len(pairs1):
                pg(0.58 + 0.27 * (i + 1) / max(1, len(pairs1)),
                   f"查票价 {i + 1}/{len(pairs1)}…")

    # 2次中转：Top 中转站两两组合（地毯式放宽对数；6 站最多 30 对，够用）
    if max_transfer >= 2:
        pg(0.86, "正在查2次中转中间段…")
        hubs = middles[:6]
        date_next = add_days(date, 1)
        pairs = []
        for m1 in hubs:
            for m2 in hubs:
                if m1["code"] == m2["code"]:
                    continue
                pairs.append((m1, m2))
                if len(pairs) >= cfg["pairs2"]:
                    break
            if len(pairs) >= cfg["pairs2"]:
                break
        mid_legs = {}
        ex = ThreadPoolExecutor(max_workers=4)
        try:
            futs = {}
            for m1, m2 in pairs:
                futs[ex.submit(safe_query, m1["code"], m2["code"], date)] = (m1, m2, False)
                # 只有 M1 晚到（或地毯式）才加查 D+1
                try:
                    late = cfg["always_next"] or need_next_day(leg_map.get(("A", m1["code"]), []), max_gap)
                except Exception:
                    late = cfg["always_next"]
                if late:
                    futs[ex.submit(safe_query, m1["code"], m2["code"], date_next)] = (m1, m2, True)
            tmp = {}
            for f in consume_ordered(futs, should_stop):
                m1, m2, is_next = futs[f]
                try:
                    raw = f.result() or []
                except Exception:
                    raw = []
                qd = date_next if is_next else date
                try:
                    anchored = [anchor(t, qd) for t in raw]
                except Exception:
                    anchored = []
                tmp.setdefault((m1["code"], m2["code"])).extend(anchored)
            for k, v in tmp.items():
                mid_legs[k] = sorted(v, key=lambda x: x["_dep"])  # 不截断，配对时锚定取
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
        # 用已有 A->M1 与 M2->B 拼接（同样锚定配对，避免早班截断）
        for (c1, c2), mids in mid_legs.items():
            check_stop(should_stop)
            list_a = spread_sample(leg_map.get(("A", c1), []), 6)
            list_b = leg_map.get(("B", c2), [])
            for a, mid, w1 in pair_legs(list_a, mids, min_gap, max_gap, per_a=3):
                for _, b, w2 in pair_legs([mid], list_b, min_gap, max_gap, per_a=3):
                    pa, pan = leg_price(a, a["dep_date"], price_cache) if with_price else (None, None)
                    pm, pmn = leg_price(mid, mid["dep_date"], price_cache) if with_price else (None, None)
                    pb, pbn = leg_price(b, b["dep_date"], price_cache) if with_price else (None, None)
                    aa = dict(a); aa["price"] = pa; aa["price_name"] = pan
                    mm = dict(mid); mm["price"] = pm; mm["price_name"] = pmn
                    bb = dict(b); bb["price"] = pb; bb["price_name"] = pbn
                    combos.append(build_combo([aa, mm, bb], [pa, pm, pb], [w1, w2]))
                    if len(combos) > cfg["cap2"]:
                        break

    enrich_names(combos, smap)
    pg(0.96, "排序汇总…")
    # 排序：有价格按价格，默认按总时长；前端还会二次过滤
    combos_sorted = sorted(combos, key=lambda c: ((c["total_price"] is None), c["total_price"] or 9e9, c["total_min"]))
    out = {
        "from": {"name": from_name, "code": from_code},
        "to": {"name": to_name, "code": to_code},
        "date": date,
        "direct_count": len(direct),
        "combos": combos_sorted[:limit * 5],
        "middles": middles if max_transfer >= 1 else [],
        "breadth": cfg["label"],
        "stats": {"requests": api.get_req_count(), "middles": len(middles) if max_transfer >= 1 else 0,
                  "combos": len(combos)},
    }
    if tinfo.get("cached"):
        out["cached"] = True
        out["cached_at"] = tinfo.get("cached_at")
    if tinfo.get("stale"):
        out["stale"] = True
        out["warn"] = tinfo.get("warn", "")
    pg(1.0, "完成")
    return out


def safe_query(f, t, d):
    try:
        api.sleep_polite(0.3, 0.8)
        # 分段批量查询：单轮快失败（retries=0），避免限流时雪上加霜；
        # 关键的首次直达查询走 cached_query_tickets（自带重试）
        trains, _ = api.query_tickets(f, t, d, retries=0)
        return trains
    except Exception:
        return []

def mock_result(f, t, d, err):
    """12306被限流/无网时返回演示数据，保证图形界面可体验."""
    demo = [
        {"legs": [{"code": "G101", "train_type": "高铁", "type_letter": "G", "start_time": "08:00", "arrive_time": "11:00", "duration": "03:00", "duration_min": 180, "from_code": "BJP", "to_code": "SHH", "seats": {"二等": "有"}, "dep_plus": 0, "arr_plus": 0, "dep_date": d, "arr_date": d}],
         "total_price": 620, "total_min": 180, "waits": [], "transfers": 0, "codes": "G101", "combo_kind": "直达高铁", "has_price": True, "dep_date": d, "arr_date": d},
        {"legs": [{"code": "K101", "train_type": "快速", "type_letter": "K", "start_time": "20:00", "arrive_time": "08:00", "duration": "12:00", "duration_min": 720, "from_code": "BJP", "to_code": "SHH", "seats": {"硬座": "有"}, "dep_plus": 0, "arr_plus": 1, "dep_date": d, "arr_date": d}],
         "total_price": 150, "total_min": 720, "waits": [], "transfers": 0, "codes": "K101", "combo_kind": "直达快速", "has_price": True, "dep_date": d, "arr_date": d},
        {"legs": [{"code": "G101", "train_type": "高铁", "type_letter": "G", "start_time": "08:00", "arrive_time": "10:00", "duration": "02:00", "duration_min": 120, "from_code": "BJP", "to_code": "XCH", "seats": {"二等": "有"}, "dep_plus": 0, "arr_plus": 0, "dep_date": d, "arr_date": d}, {"code": "K202", "train_type": "快速", "type_letter": "K", "start_time": "10:45", "arrive_time": "15:30", "duration": "04:45", "duration_min": 285, "from_code": "XCH", "to_code": "SHH", "seats": {"硬座": "有"}, "dep_plus": 0, "arr_plus": 0, "dep_date": d, "arr_date": d}],
         "total_price": 310, "total_min": 450, "waits": [45], "transfers": 1, "codes": "G101+K202", "combo_kind": "高铁+普速", "has_price": True, "dep_date": d, "arr_date": d},
    ]
    return {"from": {"name": f, "code": ""}, "to": {"name": t, "code": ""}, "date": d, "direct_count": 2, "combos": demo, "middles": [], "mock": True, "warn": err}

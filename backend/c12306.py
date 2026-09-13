"""12306 直连客户端：零登录查询余票/票价/经停站/车站码."""
import re
import time
import random
import threading
import requests
from functools import lru_cache

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://kyfw.12306.cn/otn/leftTicket/init",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}

# UA 轮换池：会话刷新时换一个，降低被 burst 保护盯上的概率
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
]

QUERY_ENDPOINTS = [
    "https://kyfw.12306.cn/otn/leftTicket/queryG",
    "https://kyfw.12306.cn/otn/leftTicket/queryO",
    "https://kyfw.12306.cn/otn/leftTicket/queryZ",
    "https://kyfw.12306.cn/otn/leftTicket/queryA",
    "https://kyfw.12306.cn/otn/leftTicket/query",
]

STATION_JS = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"

# 线程本地会话：搜索时 4 个 worker 并发打 12306，
# requests.Session 的 Cookie/连接池非线程安全，共用一个曾导致偶发空包。
_tls = threading.local()


def get_session():
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        h = dict(HEADERS)
        h["User-Agent"] = random.choice(UA_POOL)
        s.headers.update(h)
        _tls.s = s
        try:
            s.get("https://kyfw.12306.cn/otn/leftTicket/init", timeout=10)
        except Exception:
            pass
    return s


def reset_session():
    """丢弃本线程旧会话（Cookie 可能已失效/被限流），下次重建并换 UA."""
    s = getattr(_tls, "s", None)
    try:
        if s is not None:
            s.close()
    except Exception:
        pass
    _tls.s = None
    return get_session()


# ---------- 轻量 TTL 缓存（无外部依赖，防重复打 12306） ----------
import time as _time

_CACHE = {}
_cache_lock = threading.Lock()
TICKET_TTL = 120   # 余票 2 分钟（同一查询重复点不再打过去）
ROUTE_TTL = 3600   # 经停站时刻表一天内基本不变
PRICE_TTL = 900    # 票价 15 分钟


def _cache_get(key, ttl):
    e = _CACHE.get(key)
    if e and _time.time() - e[0] < ttl:
        return e[1]
    return None


def _cache_set(key, val):
    with _cache_lock:
        _CACHE[key] = (_time.time(), val)
        if len(_CACHE) > 500:  # 防止无限增长，淘汰最旧 100 条
            for k, _ in sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:100]:
                _CACHE.pop(k, None)


def _cache_stale(key):
    """不限 TTL 取旧数据，用于 12306 失败时兜底."""
    e = _CACHE.get(key)
    return e[1] if e else None


def sleep_polite(a=0.6, b=1.4):
    time.sleep(random.uniform(a, b))


# ---------- 车站 ----------
def fetch_stations_raw(timeout=15):
    s = get_session()
    r = s.get(STATION_JS, timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_station_js(js_text):
    """@bjb|北京北|VAP|beijingbei|bjb|0 -> {name, code, pinyin}"""
    stations = []
    for m in re.finditer(r"@([a-z]+)\|([^\|]+)\|([A-Z]+)\|([a-z]+)\|([a-z]+)\|(\d+)", js_text):
        stations.append({
            "abbr": m.group(1),
            "name": m.group(2),
            "code": m.group(3),
            "pinyin": m.group(4),
            "short": m.group(5),
        })
    return stations


_stations_cache = {"list": [], "by_name": {}, "by_code": {}, "ts": 0}

def load_stations(force=False):
    if _stations_cache["list"] and not force:
        return _stations_cache["list"]
    try:
        js = fetch_stations_raw()
        lst = parse_station_js(js)
        if lst:
            _stations_cache["list"] = lst
            _stations_cache["by_name"] = {x["name"]: x for x in lst}
            _stations_cache["by_code"] = {x["code"]: x for x in lst}
            _stations_cache["ts"] = time.time()
            return lst
    except Exception:
        pass
    # 降级：内置核心站，保证离线可用
    fallback = [
        ("北京", "BJP"), ("北京南", "VNP"), ("北京西", "BXP"), ("北京北", "VAP"), ("北京朝阳", "IFP"), ("北京丰台", "FTP"),
        ("上海", "SHH"), ("上海南", "SNH"), ("上海虹桥", "AOH"), ("上海西", "SXH"),
        ("广州", "GZQ"), ("广州南", "IZQ"), ("广州东", "GGQ"), ("深圳", "SZQ"), ("深圳北", "IOQ"),
        ("郑州", "ZZF"), ("郑州东", "ZAF"), ("武汉", "WHN"), ("汉口", "HKN"), ("武昌", "WCN"),
        ("长沙", "CSQ"), ("长沙南", "CTQ"), ("西安", "XAY"), ("西安北", "EAY"),
        ("成都", "CDW"), ("成都东", "ICW"), ("重庆", "CQW"), ("重庆北", "CUW"),
        ("杭州", "HZH"), ("杭州东", "HGH"), ("南京", "NJH"), ("南京南", "NKH"),
        ("合肥", "HFH"), ("合肥南", "ENH"), ("济南", "JNK"), ("济南西", "JGK"),
        ("石家庄", "SJP"), ("太原", "TYV"), ("太原南", "TNV"), ("沈阳", "SYT"), ("沈阳北", "SBT"),
        ("哈尔滨", "HBB"), ("长春", "CCT"), ("大连", "DLT"), ("青岛", "QDK"), ("天津", "TJP"), ("天津南", "TIP"),
        ("徐州", "XCH"), ("徐州东", "UUH"), ("洛阳", "LYF"), ("洛阳龙门", "LLF"), ("南昌", "NCG"), ("南昌西", "NXG"),
        ("福州", "FZS"), ("厦门", "XMS"), ("贵阳", "GIW"), ("贵阳北", "KQW"), ("昆明", "KMM"), ("昆明南", "KXM"),
        ("兰州", "LZJ"), ("兰州西", "LAJ"), ("乌鲁木齐", "WAR"), ("拉萨", "LSO"), ("西宁", "XNO"),
        ("苏州", "SZH"), ("苏州北", "OHH"), ("无锡", "WXH"), ("无锡东", "WGH"), ("常州", "CZH"),
        ("蚌埠", "BBH"), ("阜阳", "FYH"), ("襄阳", "XFN"), ("怀化", "HHQ"), ("桂林", "GLZ"), ("南宁", "NNZ"),
    ]
    lst = [{"name": n, "code": c, "pinyin": "", "abbr": "", "short": ""} for n, c in fallback]
    _stations_cache["list"] = lst
    _stations_cache["by_name"] = {x["name"]: x for x in lst}
    _stations_cache["by_code"] = {x["code"]: x for x in lst}
    return lst


def resolve_station(keyword):
    """中文名/拼音/代码 -> (name, code)，找不到返回 (None,None)"""
    lst = load_stations()
    keyword = (keyword or "").strip()
    if not keyword:
        return None, None
    by_name = _stations_cache["by_name"]
    by_code = _stations_cache["by_code"]
    if keyword in by_name:
        return keyword, by_name[keyword]["code"]
    if keyword.upper() in by_code:
        s = by_code[keyword.upper()]
        return s["name"], s["code"]
    for s in lst:
        if keyword in s["name"] or keyword.lower() in s.get("pinyin", ""):
            return s["name"], s["code"]
    return None, None


def search_stations(q, limit=10):
    lst = load_stations()
    q = (q or "").strip().lower()
    if not q:
        return lst[:limit]
    out = []
    for s in lst:
        if q in s["name"].lower() or q in s.get("pinyin", "") or q in s.get("abbr", "") or q.upper() == s["code"]:
            out.append(s)
        if len(out) >= limit:
            break
    return out


# ---------- 余票解析 ----------
def parse_ticket_row(row):
    """data.result 元素是 | 分隔串，返回结构化车次."""
    p = row.split("|")
    # 补齐防止越界
    while len(p) < 36:
        p.append("")
    def num(x):
        return None if x in ("", "--", "无", "*", "null") else x
    code = p[3]
    t = code[0] if code else "O"
    train_type = {"G": "高铁", "C": "城际", "D": "动车", "Z": "直达", "T": "特快", "K": "快速"}.get(t, "其他")
    return {
        "secret": p[0],
        "can_book_text": p[1],
        "train_no": p[2],
        "code": code,
        "train_type": train_type,
        "type_letter": t,
        "start_code": p[4],
        "end_code": p[5],
        "from_code": p[6],
        "to_code": p[7],
        "start_time": p[8],
        "arrive_time": p[9],
        "duration": p[10],
        "duration_min": to_minutes(p[10]),
        "can_book": p[11] == "Y",
        "start_train_date": p[13],
        "from_no": p[16],
        "to_no": p[17],
        # 席别查询串用 p[35] 短式（如 9MO / 1413）；p[34] 长式（90M0O0W0）传给
        # queryTicketPrice 会只返回商务座价——曾导致短途段标出 ¥1338 的 bug
        "seat_types": (p[35] if len(p) > 35 and p[35] else ""),
        # 席别余票（--/无/数字/有）
        "seats": {
            "商务特等": num(p[32]),
            "一等": num(p[31]),
            "二等": num(p[30]),
            "高级软卧": num(p[21]),
            "软卧": num(p[23]),
            "动卧": num(p[27]) if len(p) > 27 else None,
            "硬卧": num(p[28]),
            "软座": num(p[24]) if len(p) > 24 else None,
            "硬座": num(p[29]),
            "无座": num(p[26]),
        },
        "raw_len": len(p),
    }


def to_minutes(lishi):
    try:
        if ":" in lishi:
            h, m = lishi.split(":")
            return int(h) * 60 + int(m)
        return int(lishi)
    except Exception:
        return 9999


# ---------- 请求计数（一次组合搜索到底打了多少次 12306，前端公示用） ----------
_REQ_COUNT = 0


def reset_req_count():
    global _REQ_COUNT
    _REQ_COUNT = 0


def get_req_count():
    return _REQ_COUNT


def _fetch_json(url, params, timeout, tag):
    """单次 GET 并做响应体检查：把“空包/HTML拦截页”转成可读错误.

    之前这里直接 r.json()，空包时只剩 "Expecting value: line 1 column 1"，
    完全看不出是限流还是故障——这就是本次用户困惑的根源。
    """
    global _REQ_COUNT
    _REQ_COUNT += 1
    s = get_session()
    r = s.get(url, params=params, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"{tag} HTTP {r.status_code}")
    body = r.text.strip() if isinstance(r.text, str) else ""
    if not body:
        raise RuntimeError(f"{tag} 空响应（限流或网络抖动），HTTP {r.status_code}")
    if not body.startswith("{"):
        # 去掉 BOM/不可打印字符再截断：原始头含 \ufeff 会搞崩 GBK 控制台
        head = "".join(ch for ch in body.lstrip("﻿").lstrip("￾")[:80] if ch.isprintable())
        raise RuntimeError(f"{tag} 非JSON响应（疑似限流拦截/维护页），HTTP {r.status_code}，头部:{head!r}")
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"{tag} JSON解析失败: {e}，头部:{body[:80]!r}")


def _query_once(from_code, to_code, date):
    """单轮接口名轮询（queryG/O/Z/A + c_url 自适应），不重试."""
    params = {
        "leftTicketDTO.train_date": date,
        "leftTicketDTO.from_station": from_code,
        "leftTicketDTO.to_station": to_code,
        "purpose_codes": "ADULT",
    }
    last_err = "无可用接口"
    urls = list(QUERY_ENDPOINTS)
    extra = []
    for url in urls:
        try:
            j = _fetch_json(url, params, 12, "余票查询")
            data = j.get("data", {})
            c_url = data.get("c_url") if isinstance(data, dict) else None
            if c_url and c_url != "queryG":
                nxt = "https://kyfw.12306.cn/otn/" + c_url
                if nxt not in urls and nxt not in extra:
                    extra.append(nxt)
            result = data.get("result") if isinstance(data, dict) else None
            if result is not None:
                trains = [parse_ticket_row(x) for x in result]
                station_map = data.get("map", {}) or {}
                return trains, station_map
            last_err = str(j.get("messages") or j.get("status"))
        except Exception as e:
            last_err = str(e)
            continue
    for url in extra:  # c_url 指向的新接口名补查一次
        try:
            j = _fetch_json(url, params, 12, "余票查询")
            data = j.get("data", {})
            result = data.get("result") if isinstance(data, dict) else None
            if result is not None:
                return [parse_ticket_row(x) for x in result], data.get("map", {}) or {}
        except Exception as e:
            last_err = str(e)
    raise RuntimeError(last_err)


def query_tickets(from_code, to_code, date, retries=2):
    """直达余票，失败自动刷新会话+退避重试.

    retries=2 用于关键的首次直达查询；分段批量查询请用 retries=0
    快速失败，避免限流时雪上加霜。
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            return _query_once(from_code, to_code, date)
        except Exception as e:
            last_err = str(e)
            if attempt < retries:
                reset_session()
                time.sleep(1.5 * (attempt + 1) + random.uniform(0, 1))
    raise RuntimeError(f"12306繁忙，已重试{retries + 1}次: {last_err}。稍等30秒再试。")


def cached_query_tickets(from_code, to_code, date):
    """带缓存的余票查询，返回 (trains, map, info).

    info = {"cached": 是否命中缓存, "cached_at": 缓存时间戳}
    """
    key = ("t", from_code, to_code, date)
    hit = _cache_get(key, TICKET_TTL)
    if hit is not None:
        return hit[0], hit[1], {"cached": True, "cached_at": hit[2]}
    trains, smap = query_tickets(from_code, to_code, date, retries=2)
    now = _time.time()
    _cache_set(key, (trains, smap, now))
    return trains, smap, {"cached": False, "cached_at": now}


def stale_tickets(from_code, to_code, date):
    """不限 TTL 取旧余票，(trains, map, cached_at)，无则 None."""
    hit = _cache_stale(("t", from_code, to_code, date))
    if hit is None:
        return None
    return hit[0], hit[1], hit[2]


def query_price(train_no, from_no, to_no, seat_types, date):
    s = get_session()
    url = "https://kyfw.12306.cn/otn/leftTicket/queryTicketPrice"
    j = _fetch_json(url, {
        "train_no": train_no,
        "from_station_no": from_no,
        "to_station_no": to_no,
        "seat_types": seat_types,
        "train_date": date,
    }, 10, "票价查询")
    return j.get("data", {})


def cached_query_price(train_no, from_no, to_no, seat_types, date):
    """票价跨次缓存（15分钟），命中不打网；未命中礼貌等待后查询."""
    key = ("p", train_no, from_no, to_no, seat_types, date)
    hit = _cache_get(key, PRICE_TTL)
    if hit is not None:
        return hit
    sleep_polite(0.2, 0.5)
    try:
        data = query_price(train_no, from_no, to_no, seat_types, date)
    except Exception:
        return {}
    _cache_set(key, data)
    return data


PRICE_KEY_MAP = {
    "9": ("商务特等", 9), "P": ("商务特等", 9), "M": ("一等", 0), "O": ("二等", 0),
    "A4": ("软卧", 0), "A3": ("硬卧", 0), "A2": ("软座", 0), "A1": ("硬座", 0),
    "WZ": ("无座", 0), "A6": ("高级软卧", 0),
}

def cheapest_price(price_data, prefer=None):
    """取代表票价(元).

    prefer 指定优先席别（如 G 优先"二等"、K 优先"硬座"）：命中则用它，
    否则回退最低价。之前一律取最低，在高铁线上永远显示"无座"（虽与二等
    同价但看着怪）——实查 A 组抓到的展示问题。
    """
    if not price_data:
        return None, None
    if prefer:
        for k, v in price_data.items():
            if k in ("train_no", "OT"):
                continue
            name = PRICE_KEY_MAP.get(k, (k, 0))[0] if isinstance(k, str) else str(k)
            if name != prefer:
                continue
            val = _parse_price_val(v)
            if val is not None:
                return val, name
    best = None
    best_name = None
    for k, v in price_data.items():
        if k in ("train_no", "OT"):
            continue
        name = PRICE_KEY_MAP.get(k, (k, 0))[0] if isinstance(k, str) else str(k)
        val = _parse_price_val(v)
        if val is None:
            continue
        if best is None or val < best:
            best = val
            best_name = name
    return best, best_name


def _parse_price_val(v):
    try:
        if isinstance(v, str) and v.startswith("¥"):
            return float(v.replace("¥", ""))
        if isinstance(v, (int, float)):
            return float(v) / 10 if float(v) > 1000 else float(v)
    except Exception:
        pass
    return None


def query_route(train_no, from_code, to_code, date):
    s = get_session()
    url = "https://kyfw.12306.cn/otn/czxx/queryByTrainNo"
    j = _fetch_json(url, {
        "train_no": train_no,
        "from_station_telecode": from_code,
        "to_station_telecode": to_code,
        "depart_date": date,
    }, 10, "经停站查询")
    return j.get("data", {})


def cached_query_route(train_no, from_code, to_code, date):
    """经停站跨次缓存（1小时），时刻表日内不变，重复搜索直接命中."""
    key = ("r", train_no, from_code, to_code, date)
    hit = _cache_get(key, ROUTE_TTL)
    if hit is not None:
        return hit
    sleep_polite(0.2, 0.4)
    try:
        data = query_route(train_no, from_code, to_code, date)
    except Exception:
        return {}
    _cache_set(key, data)
    return data

"""归档回归：爬取落库 + 归档记录 + 4 个查询接口（离线，全打桩）。

背景：
- crawl_runs 原来只在正常跑完时写一条，中途取消/异常就丢失归档；
- 网页版完全没有归档浏览（只有桌面版有），后端也只暴露了 stats/crawl；
- recent_runs 返回 4 元组，加 day 字段后要保证桌面版解包不炸。
"""
import os as _os, sys as _sys, tempfile
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from backend import crawler, graphdb, c12306 as api

TMP = tempfile.mktemp(suffix=".db")
STATIONS = [
    {"name": "唐山", "code": "TSP", "pinyin": "tangshan", "abbr": "tsp", "short": "ts"},
    {"name": "北京", "code": "BJP", "pinyin": "beijing", "abbr": "bjp", "short": "bj"},
    {"name": "上海", "code": "SHH", "pinyin": "shanghai", "abbr": "shh", "short": "sh"},
    {"name": "甲", "code": "AAA", "pinyin": "jia", "abbr": "aaa", "short": "j"},
    {"name": "乙", "code": "BBB", "pinyin": "yi", "abbr": "bbb", "short": "y"},
]

_saved = (api.load_stations, api.query_tickets, api.query_route,
          api.resolve_station, dict(api._stations_cache))
api.load_stations = lambda *a, **k: STATIONS
api._stations_cache.update({
    "list": STATIONS,
    "by_name": {s["name"]: s for s in STATIONS},
    "by_code": {s["code"]: s for s in STATIONS},
    "ts": 0, "fallback": False,
})
api.resolve_station = lambda kw: next(
    ((s["name"], s["code"]) for s in STATIONS if s["name"] == (kw or "").strip()),
    (None, None))

state = {"n": 0}


def fake_tickets(f, t, d, retries=1):
    state["n"] += 1
    return ([{"train_no": "t%d" % state["n"], "code": "G%d" % state["n"],
              "from_code": f, "to_code": t, "from_no": "01", "to_no": "02",
              "type_letter": "G", "seat_types": "9MO", "seats": {}}], {})


api.query_tickets = fake_tickets
api.query_route = lambda *a, **k: {"data": [
    {"station_name": "甲", "isEnable": True},
    {"station_name": "乙", "isEnable": True},
]}

try:
    # ---- 1) 中途取消：已爬的车要入库，归档记录不能丢 ----
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 10

    st = crawler.run(db_path=TMP, max_requests=100, interval=0.001,
                     on_progress=lambda u, m, msg: None,
                     should_stop=stop, priority_from=["唐山"], phase="mesh")
    con = graphdb.connect(TMP)
    try:
        n_trains = con.execute("SELECT COUNT(*) FROM trains").fetchone()[0]
        n_stops = con.execute("SELECT COUNT(*) FROM stops").fetchone()[0]
        runs = graphdb.recent_runs(con, 5)
        assert n_trains >= 1, f"取消后车次没入库：{n_trains}"
        assert n_stops >= 2 * n_trains, (n_trains, n_stops)
        assert runs, "取消后归档记录丢了"
        ts, req, new, prio, day = runs[0]
        assert req == st["requests"], (req, st["requests"])
        assert new == st["new_trains"], (new, st["new_trains"])
        assert prio == "TSP", prio
        assert day, "归档缺数据日期"
        print("1 ok: cancel still archives =", runs[0])

        # ---- 2) recent_runs 是 5 元组（桌面版解包依赖） ----
        assert len(runs[0]) == 5, len(runs[0])
        print("2 ok: recent_runs returns 5-tuple")

        # ---- 3) 归档查询：按车次 / 按车站 / 最近入库 ----
        code = con.execute("SELECT code FROM trains LIMIT 1").fetchone()[0]
        detail = graphdb.train_detail(con, code)
        assert detail and len(detail) >= 2, detail
        assert detail[0][1] == "甲", detail[0]

        at = graphdb.trains_at(con, "AAA", 50)
        assert at, "按车站查不到过路车"
        assert any(c == code for _t, c in at), (code, at)

        recent = graphdb.recent_trains(con, 10)
        assert recent, "最近入库为空"
        # recent_trains 按 rowid 倒序 = 最后入库的在前，所以第一条不是 detail 查的 code，
        # 而是本次最后写入的车次。只断言它是合法车次且在库中。
        assert recent[0][1].startswith("G"), recent[:2]
        assert all(c for _t, c, _u in recent), recent[:3]
        print("3 ok: train_detail / trains_at / recent_trains")

        # ---- 4) stats 带归档摘要（前端顶部那行） ----
        s = graphdb.stats(con)
        assert {"trains", "stations", "ods", "coverage"} <= set(s), s
        print("4 ok: stats =", {k: s[k] for k in ("trains", "stations", "ods")})
    finally:
        con.close()

    # ---- 5) 多次爬取：每次一条归档，按时间倒序 ----
    crawler.run(db_path=TMP, max_requests=6, interval=0.001,
                on_progress=lambda u, m, msg: None,
                should_stop=lambda: False, priority_from=["北京"], phase="mesh")
    con = graphdb.connect(TMP)
    try:
        runs = graphdb.recent_runs(con, 10)
        assert len(runs) >= 2, runs
        assert runs[0][0] >= runs[1][0], "归档没按时间倒序"
        print("5 ok: multiple runs archived =", len(runs))
    finally:
        con.close()
finally:
    (api.load_stations, api.query_tickets, api.query_route,
     api.resolve_station, _cache) = _saved
    api._stations_cache.clear()
    api._stations_cache.update(_cache)
    try:
        _os.remove(TMP)
    except Exception:
        pass

print("ARCHIVE CHECKS PASSED")

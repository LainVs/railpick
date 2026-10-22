"""限流识别回归：接口返回 HTML 拦截页时必须立刻停，不能继续打。

实测（2026-10）：间隔 0.6s 就开始被拦，0.35s 全拦；触发后余票接口
封 25 分钟以上仍未解封，而 init 页/车站列表正常——是接口级封禁。
所以爬虫必须把「HTML 拦截」与普通网络错误分开，遇到就停。
"""
import os as _os, sys as _sys, tempfile
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from backend import crawler, graphdb, c12306 as api

TMP = tempfile.mktemp(suffix=".db")
STATIONS = [
    {"name": "唐山", "code": "TSP", "pinyin": "t", "abbr": "t", "short": "t"},
    {"name": "北京", "code": "BJP", "pinyin": "b", "abbr": "b", "short": "b"},
    {"name": "上海", "code": "SHH", "pinyin": "s", "abbr": "s", "short": "s"},
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

try:
    # ---- 1) 限流错误必须被识别成 Throttled 并立刻停 ----
    calls = {"n": 0}

    def throttled_tickets(f, t, d, retries=1):
        calls["n"] += 1
        raise RuntimeError("余票查询 非JSON响应（疑似限流拦截/维护页），"
                           "HTTP 200，头部:'\\ufeff<!DOCTYPE html'")

    api.query_tickets = throttled_tickets
    st = crawler.run(db_path=TMP, max_requests=100, interval=0.001,
                     on_progress=lambda u, m, msg: None,
                     priority_from=["唐山"], phase="mesh")
    assert st.get("throttled") is True, st
    assert "限流" in (st.get("throttled_msg") or ""), st.get("throttled_msg")
    # 只打了一次就该停（不是跑满 100 次）
    assert calls["n"] == 1, f"限流后应立刻停，实际打了 {calls['n']} 次"
    print("1 ok: throttle detected, stopped after 1 request")

    # ---- 2) 限流时归档仍然要写（已爬数据保留） ----
    con = graphdb.connect(TMP)
    try:
        runs = graphdb.recent_runs(con, 3)
        assert runs, "限流中断后归档丢了"
        print("2 ok: archive kept on throttle =", runs[0])
    finally:
        con.close()

    # ---- 3) 普通网络错误不该误判成限流 ----
    TMP2 = tempfile.mktemp(suffix=".db")

    def plain_err(f, t, d, retries=1):
        raise RuntimeError("连接超时")

    api.query_tickets = plain_err
    st2 = crawler.run(db_path=TMP2, max_requests=6, interval=0.001,
                      on_progress=lambda u, m, msg: None,
                      priority_from=["唐山"], phase="mesh")
    assert not st2.get("throttled"), f"普通超时被误判成限流: {st2.get('throttled_msg')}"
    print("3 ok: plain network error not treated as throttle")

    # ---- 4) RateController 下限是 1.5s（实测边界 1.0~0.6s，保守取值） ----
    rc = crawler.RateController(cur=0.1)
    assert rc.cur == 1.5, rc.cur
    for _ in range(20):
        rc.ok()
    assert rc.cur == 1.5, f"加速不能突破下限: {rc.cur}"
    rc.fail()
    assert rc.cur == 3.0, rc.cur
    rc.fail()
    assert rc.cur == 6.0, rc.cur
    for _ in range(10):
        rc.fail()
    assert rc.cur == 30.0, f"退避不能超过上限: {rc.cur}"
    print("4 ok: rate controller bounds (1.5s floor / 30s ceiling)")

    try:
        _os.remove(TMP2)
    except Exception:
        pass
finally:
    (api.load_stations, api.query_tickets, api.query_route,
     api.resolve_station, _cache) = _saved
    api._stations_cache.clear()
    api._stations_cache.update(_cache)
    try:
        _os.remove(TMP)
    except Exception:
        pass

print("THROTTLE CHECKS PASSED")

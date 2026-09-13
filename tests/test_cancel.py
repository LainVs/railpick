"""协作取消回归：signal 语义 + 线程池快关 + 全链路透传（API全打桩，零网络）。

背景：以前点取消只是丢弃结果，后台照打 12306；且最长等待填 99999 会组合爆炸。
现在各环节都认 should_stop，取消秒停。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from backend import cancel as cancel_mod
from backend.cancel import SearchCancelled
from backend import search as search_mod
from backend import graphsearch as graph_mod
from backend import c12306 as api

# ---- 1) signal 语义 ----
assert cancel_mod.stopped(None) is False
assert cancel_mod.stopped(lambda: False) is False
assert cancel_mod.stopped(lambda: True) is True
ev = threading.Event()
assert cancel_mod.stopped(ev.is_set) is False
ev.set()
assert cancel_mod.stopped(ev.is_set) is True
try:
    cancel_mod.check(ev.is_set)
    raise AssertionError("check should raise")
except SearchCancelled:
    pass
print("1 ok: cancel signal")

# ---- 2) consume_ordered：正常排空 ----
ex = ThreadPoolExecutor(max_workers=2)
try:
    futs = [ex.submit(lambda i=i: i) for i in range(5)]
    got = sorted(f.result() for f in search_mod.consume_ordered(futs, None))
    assert got == [0, 1, 2, 3, 4], got
finally:
    ex.shutdown(wait=False, cancel_futures=True)
print("2 ok: consume drains normally")

# ---- 3) consume_ordered：取消快返（不等慢任务跑完） ----
ex2 = ThreadPoolExecutor(max_workers=1)
try:
    futs2 = [ex2.submit(time.sleep, 2) for _ in range(2)]  # 排空要 4s+
    t0 = time.time()
    try:
        for _ in search_mod.consume_ordered(futs2, lambda: True):
            pass
        raise AssertionError("should raise")
    except SearchCancelled:
        pass
    assert time.time() - t0 < 3, "cancel must not wait for slow tasks"
finally:
    ex2.shutdown(wait=False, cancel_futures=True)
print("3 ok: consume aborts fast")

# ---- 4) search_combos：入口即停（零网络） ----
try:
    search_mod.search_combos("北京", "上海", "2026-09-10", should_stop=lambda: True)
    raise AssertionError("should raise")
except SearchCancelled:
    pass
print("4 ok: search_combos immediate stop")

# ---- 5) search_combos：跑起来以后也能停（全打桩，A 阶段即停） ----
_r1 = api.resolve_station
_r2 = api.cached_query_tickets
_r3 = search_mod.safe_query
FAKE_TRAIN = {"train_no": "t1", "from_no": "01", "to_no": "02", "seat_types": "9MO",
              "type_letter": "G", "code": "G1", "start_time": "08:00",
              "arrive_time": "09:00", "duration_min": 60,
              "from_code": "BJP", "to_code": "ZZF"}
api.resolve_station = lambda kw: (kw.strip(), "C_" + kw.strip())
api.cached_query_tickets = lambda *a, **k: ([], {}, {})
search_mod.safe_query = lambda *a: [dict(FAKE_TRAIN)]
calls = {"n": 0}


def _stop_after():
    calls["n"] += 1
    return calls["n"] > 1


try:
    search_mod.search_combos("北京", "上海", "2026-09-10", max_transfer=1,
                             with_price=False, breadth="quick",
                             should_stop=_stop_after)
    raise AssertionError("should raise mid-pipeline")
except SearchCancelled:
    pass
finally:
    api.resolve_station, api.cached_query_tickets, search_mod.safe_query = _r1, _r2, _r3
print("5 ok: search_combos mid-pipeline stop")

# ---- 6) verify_combos：核验中取消 ----
combos = [{"legs": [{"from_code": "A", "to_code": "B%d" % i, "dep_date": "2026-09-10",
                     "train_no": "t1"}]} for i in range(6)]
_r4 = api.query_tickets


def _slow_q(*a, **k):
    time.sleep(0.05)
    return [], {}


api.query_tickets = _slow_q
calls2 = {"n": 0}


def _stop_after2():
    calls2["n"] += 1
    return calls2["n"] > 0


try:
    graph_mod.verify_combos(combos, should_stop=_stop_after2)
    raise AssertionError("should raise")
except SearchCancelled:
    pass
finally:
    api.query_tickets = _r4
print("6 ok: verify_combos stop")

# ---- 7) graph_search：大图枚举中取消（纯内存无网络） ----
legs = [(480 + (i % 60), 60, "t%d" % i, "K1", "B") for i in range(6000)]
net = {"A": legs}
try:
    graph_mod.graph_search(None, "A", "B", ["2026-09-10"], max_transfers=0,
                           limit=100000, _net=(net, 6000), should_stop=lambda: True)
    raise AssertionError("should raise")
except SearchCancelled:
    pass
print("7 ok: graph_search enumeration stop")

# ---- 8) run_graph_search：入口即停（打桩 resolve/load） ----
_r5 = api.resolve_station
_r6 = api.load_stations
api.resolve_station = lambda kw: (kw.strip(), "C_" + kw.strip())
api.load_stations = lambda *a, **k: []
try:
    graph_mod.run_graph_search("北京", "上海", "2026-09-10",
                               db_path=":memory:", should_stop=lambda: True)
    raise AssertionError("should raise")
except SearchCancelled:
    pass
finally:
    api.resolve_station, api.load_stations = _r5, _r6
print("8 ok: run_graph_search entry stop")

print("CANCEL CHECKS PASSED")

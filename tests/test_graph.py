"""图数据库 + 爬虫 + 图候选融合（纯离线，API全打桩）。"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from backend import graphdb
from backend import crawler as C

# ---- 1) 图存取与相遇查询 ----
con = graphdb.connect(":memory:")
graphdb.upsert_train(con, "t1", "G1",
                     [("A", "甲", "--", "08:00"), ("M", "中", "10:00", "10:05"), ("B", "乙", "12:00", "--")], "d")
graphdb.upsert_train(con, "t2", "K2",
                     [("A", "甲", "--", "09:00"), ("M", "中", "14:00", "14:10"), ("C", "丙", "18:00", "--")], "d")
assert graphdb.has_train(con, "t1") and not graphdb.has_train(con, "t9")
assert graphdb.common_middles(con, "A", "B") == ["M"]
assert graphdb.common_middles(con, "A", "C") == ["M"]
assert graphdb.common_middles(con, "B", "A") == []
assert graphdb.common_middles(con, "X", "Y") == []
st = graphdb.stats(con)
assert (st["trains"], st["stations"], st["ods"]) == (2, 4, 0), st
assert st["coverage"] == round(2 / graphdb.EST_TOTAL * 100, 1) and st["total_est"] == 12174, st
print("1 ok: graphdb")

# ---- 2) 爬虫：API打桩，零等待跑完 ----
calls = {"q": 0, "r": 0}
C.polite_sleep = lambda interval: None


def fake_tickets(f, t, d, retries=2):
    calls["q"] += 1
    return ([{"train_no": "t9", "code": "G9", "from_code": f, "to_code": t,
              "from_no": "01", "to_no": "03", "seat_types": "9MO"}], {})


def fake_route(tno, f, t, d):
    calls["r"] += 1
    return {"data": [{"station_name": "北京南", "arrive_time": "----", "start_time": "08:00"},
                     {"station_name": "南京南", "arrive_time": "10:00", "start_time": "10:02"},
                     {"station_name": "上海虹桥", "arrive_time": "12:00", "start_time": "----"}]}


C.api.query_tickets = fake_tickets
C.api.query_route = fake_route
C.api.resolve_station = lambda n: {"北京南": ("北京南", "VNP"), "南京南": ("南京南", "NKH"),
                                   "上海虹桥": ("上海虹桥", "AOH")}.get(n, (None, None))
C.SEED = ["VNP", "AOH"]
msgs = []
stat = C.run(":memory:", max_requests=10, interval=0,
             on_progress=lambda u, m, msg: msgs.append(msg))
assert stat["new_trains"] >= 1, stat
assert calls["q"] >= 1 and calls["r"] >= 1
assert any("累计" in m and "新入库" in m for m in msgs), msgs
print("2 ok: crawler", stat)

# ---- 3) 图候选融进 candidate_middles ----
import backend.search as S
import backend.graphdb as GDB
GDB.common_middles = lambda con, f, t, limit=8: ["NKH", "ZZF"]
S.api.cached_query_route = lambda *a: {}
S.api.resolve_station = lambda kw: (None, None)  # 枢纽全跳过，只看图候选
S.api._stations_cache = {"list": [], "by_name": {}, "by_code": {}}
m = S.candidate_middles("VNP", "AOH", "2026-09-10", [], limit=6)
names = [x["code"] for x in m]
assert "NKH" in names, names  # 图候选进了前6（经停0+图4+枢纽2）
print("3 ok: fusion =", [(x["name"], x["code"]) for x in m])

# ---- 4) 自适应调速：成功加速到下限，失败退避到上限 ----
from backend.crawler import RateController
rc = RateController(cur=4.0)
for _ in range(100):
    rc.ok()
assert rc.cur == 1.5, rc.cur
assert rc.backoffs == 0
rc.fail()
assert rc.cur == 3.0, rc.cur
for _ in range(30):
    rc.fail()
assert rc.cur == 30.0 and rc.backoffs == 31, (rc.cur, rc.backoffs)
rc2 = RateController(cur=99)
assert rc2.cur == 30.0  # 初始化钳制
print("4 ok: rate controller")

# ---- 5) 心跳：OD 内每 beat_every 个请求上报一次 ----
# 注意：test 3 把 resolve_station 打桩成了全 None，这里恢复成 test 2 的版本
C.api.resolve_station = lambda n: {"北京南": ("北京南", "VNP"), "南京南": ("南京南", "NKH"),
                                   "上海虹桥": ("上海虹桥", "AOH")}.get(n, (None, None))
from backend.crawler import crawl_step, RateController as RC2
beats = []
con5 = graphdb.connect(":memory:")
rc5 = RC2(cur=1.5)  # 最小间隔，测试不睡大觉（wait 照睡 cur+抖动，约2秒×2次）
r5 = crawl_step(con5, "VNP", "AOH", "d", rc5, 100, on_beat=beats.append, beat_every=1)
assert beats == [2], (beats, r5)  # 心跳只在拉路线时触发（OD查询那一下不算）
assert r5[1] == 1 and r5[4] == ["G9"], r5
print("5 ok: heartbeat", beats)

# ---- 6) 图查询接口：车次详情/过路车/最近入库 ----
d6 = graphdb.train_detail(con, "G1")
assert [n for _, n, _, _ in d6] == ["甲", "中", "乙"], d6
assert graphdb.train_detail(con, "不存在") == []
at = graphdb.trains_at(con, "M")
assert ("t1", "G1") in at and ("t2", "K2") in at, at
rec = graphdb.recent_trains(con, 1)
assert rec[0][0] == "t2", rec  # 后入库的在前
print("6 ok: graph queries")

# ---- 7) 优先站排序：优先站的进出对排最前，去重保序 ----
from backend.crawler import build_ods, resolve_codes
o = build_ods(["TSP"], seed=["BJP", "TSP", "SHH"])
assert o[0] == ("TSP", "BJP") and o[1] == ("BJP", "TSP"), o[:4]
assert o[2] == ("TSP", "SHH") and o[3] == ("SHH", "TSP"), o[:4]
assert len(o) == len(set(o)) == 6, o
assert build_ods(None, seed=["A", "B"]) == [("A", "B"), ("B", "A")]
assert resolve_codes(["BJP", "  "]) == ["BJP"]  # 代码直通（名字解析走网络，这里不测）
print("7 ok: priority ods")

# ---- 8) 防重：同车重入整车替换不翻倍；爬取记录可查 ----
graphdb.upsert_train(con, "t1", "G1",
                     [("A", "甲", "--", "08:00"), ("M", "中", "10:00", "10:05"), ("B", "乙", "12:00", "--")], "d2")
st8 = graphdb.stats(con)
assert st8["trains"] == 2, st8  # 还是2趟，没翻倍
n_stops = con.execute("SELECT COUNT(*) c FROM stops WHERE train_no='t1'").fetchone()["c"]
assert n_stops == 3, n_stops  # 经停也是替换而非追加
graphdb.log_run(con, 10, 2, ["TSP"])
graphdb.log_run(con, 5, 0, [])
runs = graphdb.recent_runs(con, 5)
assert len(runs) == 2 and runs[0][1] == 5 and runs[0][2] == 0, runs
assert runs[1][3] == "TSP", runs
print("8 ok: idempotent + runs log")

# ---- 9) 限次0=无限：should_stop能刹住车，不会死循环 ----
calls9 = {"n": 0}


def stopper():
    calls9["n"] += 1
    return calls9["n"] > 3


stat9 = C.run(":memory:", max_requests=0, interval=0, on_progress=lambda *a: None,
             should_stop=stopper)
assert stat9["requests"] > 0, stat9  # 跑起来了，又被刹住了
print("9 ok: unlimited + stop", stat9)

# ---- 10) 地级散射：最近枢纽+配对结构+产量排序（纯函数，离线） ----
from backend.crawler import nearest_hubs, build_fanout, order_ods
nh = nearest_hubs("唐山", ["广州", "北京", "郑州"], k=2)
assert nh[0] == "北京", nh  # 天津不在列表里，北京最近
fo = build_fanout([("唐山", "TSP")], [("北京", "BJP"), ("广州", "GZQ")], per_origin=1)
assert fo == [("TSP", "BJP"), ("BJP", "TSP")], fo
meta = {("A", "B"): (0, 5), ("C", "D"): (0, 20), ("E", "F"): (1, 99)}
assert order_ods([("A", "B"), ("C", "D"), ("E", "F"), ("G", "H")], meta) == \
    [("G", "H"), ("C", "D"), ("A", "B")]  # 没啃过优先，中断过按产量降序，做完的丢掉
print("10 ok: fanout + yield order")

# ---- 11) 名额预留制：6车×10站、limit=12，经停最多占6席 ----
def fake_seq(train_no, f, t, d):
    i = int("".join(filter(str.isdigit, train_no)) or 0)
    return {"data": [{"station_name": f"测试站{i:02d}{j:02d}", "isEnabled": True} for j in range(10)]}


def fake_resolve(name):
    if name.startswith("测试站"):
        return name, "T" + name[-4:]
    if name == "南京南":
        return "南京南", "NKH"
    return None, None


S.api.cached_query_route = fake_seq
S.api.resolve_station = fake_resolve
S.api._stations_cache = {"list": [], "by_name": {},
                         "by_code": {"TNJ": {"name": "南京南"}}}
import backend.graphdb as _GDB
_GDB.common_middles = lambda con, f, t, limit=8: []  # 恢复test3的打桩，本测只看经停+枢纽分槽
trains11 = [{"code": f"K{i}", "type_letter": "K", "train_no": f"k{i}",
             "from_code": "A", "to_code": "B"} for i in range(6)]
m11 = S.candidate_middles("A", "B", "2026-09-10", trains11, limit=12, hub_order=["南京南"])
got11 = [x["name"] for x in m11]
assert len(m11) == 7 and got11[-1] == "南京南", got11  # 经停6席+枢纽1席
assert got11[:6] == ["测试站0000", "测试站0001", "测试站0002", "测试站0003",
                     "测试站0100", "测试站0101"], got11
print("11 ok: slot budget")
print("GRAPH CHECKS PASSED")

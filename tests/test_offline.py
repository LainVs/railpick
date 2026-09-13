"""离线图搜回归：建图时长（跨天/超24h）+ 枚举正确性 + 核验过滤（API全打桩）。"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import inspect
from datetime import date
from backend import graphdb
from backend import graphsearch as GS
from backend import c12306 as api

con = graphdb.connect(":memory:")
# T1: A08:00 -> M10:00/10:05 -> B12:00（当日）
graphdb.upsert_train(con, "t1", "G1",
                     [("A", "甲", "----", "08:00"), ("M", "中", "10:00", "10:05"),
                      ("B", "乙", "12:00", "----")], "d")
# T2: M15:00 -> B18:00（接驳用）
graphdb.upsert_train(con, "t2", "K2",
                     [("M", "中", "----", "15:00"), ("B", "乙", "18:00", "----")], "d")
# T3: X20:00 -> Y次日08:00 -> Z第三日02:00（总30h，逐跳<24h）
graphdb.upsert_train(con, "t3", "K3",
                     [("X", "西", "----", "20:00"), ("Y", "南", "08:00", "08:10"),
                      ("Z", "终", "02:00", "----")], "d")
# T4: B13:00 -> A15:00（回头车，测无环）
graphdb.upsert_train(con, "t4", "K4",
                     [("B", "乙", "----", "13:00"), ("A", "甲", "15:00", "----")], "d")

# ---- 1) 建图时长精确（含跨天/超24h） ----
net, n = GS.build_network(con)
assert n == 4, n
xz = [l for l in net["X"] if l[4] == "Z"][0]
assert xz[0] == 20 * 60 and xz[1] == 1800, xz  # 20:00出发，整30h
xy = [l for l in net["X"] if l[4] == "Y"][0]
assert xy[1] == 720, xy  # 按到达钟算（下车10:00整，不吃停站时间）
print("1 ok: durations incl 30h")

# ---- 2) 枚举：直达+中转都有，无环，换乘上限生效 ----
D = "2026-09-10"
paths = GS.graph_search(con, "A", "B", [D], max_transfers=1)
kinds = sorted((len(p) - 1, tuple(l[1] for l in p)) for p in paths)
assert (0, ("G1",)) in kinds and (1, ("G1", "K2")) in kinds, kinds
for p in paths:
    sts = [p[0][2]] + [l[3] for l in p]
    assert len(sts) == len(set(sts)), sts  # 站不重复
direct_only = GS.graph_search(con, "A", "B", [D], max_transfers=0)
assert all(len(p) == 1 for p in direct_only) and direct_only, direct_only
print("2 ok: enumerate + acyclic + transfer cap")

# ---- 3) 日期窗口：D/D+1 都有出发 ----
paths2 = GS.graph_search(con, "A", "B", [D, "2026-09-11"], max_transfers=0)
depdates = {l[4].date().isoformat() for p in paths2 for l in p}
assert depdates == {D, "2026-09-11"}, depdates
print("3 ok: date window")

# ---- 4) 核验：对得上留下，对不上丢掉，价格补上 ----
_real_q, _real_p = api.query_tickets, api.cached_query_price


def fake_q(f, t, d, retries=1):
    rows = []
    if (f, t) == ("A", "M"):
        rows = [{"train_no": "t1", "from_no": "01", "to_no": "02", "seat_types": "9MO",
                 "type_letter": "G", "seats": {"二等": "有"}}]
    if (f, t) == ("A", "B"):
        rows = [{"train_no": "t1", "from_no": "01", "to_no": "03", "seat_types": "9MO",
                 "type_letter": "G", "seats": {"二等": "有"}}]
    if (f, t) == ("M", "B"):
        rows = [{"train_no": "t2", "from_no": "02", "to_no": "03", "seat_types": "1413",
                 "type_letter": "K", "seats": {"硬座": "有"}}]
    return rows, {}


api.query_tickets = fake_q
api.cached_query_price = lambda *a: {"O": "¥100.0", "A1": "¥50.0", "OT": [], "train_no": "x"}
try:
    combos = [GS.path_to_combo(p) for p in
              GS.graph_search(con, "A", "B", [D], max_transfers=1)]
    assert len(combos) >= 2
    ver, info = GS.verify_combos(combos)
    assert info["checked"] > 0 and info["error_rate"] == 0, info
    codes = sorted(c["codes"] for c in ver)
    assert codes == ["G1", "G1+K2"], codes  # 直达G1也在图里且能核验上
    c2 = [c for c in ver if c["codes"] == "G1+K2"][0]
    assert c2["total_price"] == 150.0 and c2["legs"][1]["price_name"] == "硬座", c2
    #  stations cache 为空时 enrich 不崩
    from backend.search import enrich_names
    enrich_names(ver, {})
finally:
    api.query_tickets, api.cached_query_price = _real_q, _real_p
print("4 ok: verify keep/drop + pricing")

# ---- 5) run_graph_search 端到端（打桩 resolve，临时文件库） ----
import tempfile
tmp = tempfile.mktemp(suffix=".db")
c2 = graphdb.connect(tmp)
graphdb.upsert_train(c2, "t1", "G1",
                     [("VNP", "北京南", "----", "08:00"), ("NKH", "南京南", "10:00", "10:02"),
                      ("AOH", "上海虹桥", "12:00", "----")], "d")
c2.close()
_real_r = api.resolve_station
api.resolve_station = lambda kw: {"北京": ("北京", "VNP"), "上海": ("上海", "AOH")}.get(
    (kw or "").strip(), (None, None))
api.query_tickets = lambda f, t, d, retries=1: (
    [{"train_no": "t1", "from_no": "01", "to_no": "03", "seat_types": "9MO",
      "type_letter": "G", "seats": {"二等": "有"}}]
    if (f, t) == ("VNP", "AOH") else [], {})
api.cached_query_price = lambda *a: {"O": "¥500.0", "OT": [], "train_no": "x"}
api._stations_cache = {"list": [], "by_name": {},
                       "by_code": {"VNP": {"name": "北京南"}, "NKH": {"name": "南京南"},
                                   "AOH": {"name": "上海虹桥"}}}
try:
    evs = []
    r = GS.run_graph_search("北京", "上海", D, max_transfers=1, verify_top=10,
                            window=False, db_path=tmp,
                            on_progress=lambda f, m: evs.append(round(f, 2)))
    assert r["combos"] and r["combos"][0]["codes"] == "G1", r.get("combos")
    assert r["combos"][0]["total_price"] == 500.0
    assert evs and evs[-1] == 1.0 and evs == sorted(evs), evs
    assert set(r.keys()) >= {"combos", "middles", "stats", "graph", "verified"}, r.keys()
finally:
    api.resolve_station = _real_r
    api.query_tickets, api.cached_query_price = _real_q, _real_p
    try:
        _os.remove(tmp)
    except Exception:
        pass
print("5 ok: run_graph_search e2e")
print("OFFLINE CHECKS PASSED")

# ---- 6) 同城扩展：兄弟站出发/换乘/到达（回归：广州南/广州白云发的车搜不到）
import backend.search as _S
_S.SAME_CITY.append({"A", "A2"})
_S.SAME_CITY.append({"S1", "S2"})
_S.SAME_CITY.append({"B", "B2"})
try:
    con6 = graphdb.connect(":memory:")
    graphdb.upsert_train(con6, "t1", "G1",
                         [("A2", "甲二", "----", "08:00"), ("B", "乙", "12:00", "----")], "d")
    graphdb.upsert_train(con6, "t2", "K2",
                         [("A", "甲", "----", "09:00"), ("S1", "中一", "10:00", "10:05")], "d")
    graphdb.upsert_train(con6, "t3", "K3",
                         [("S2", "中二", "----", "11:30"), ("B", "乙", "14:00", "----")], "d")
    graphdb.upsert_train(con6, "t4", "K4",
                         [("S2", "中二", "----", "10:30"), ("B", "乙", "13:00", "----")], "d")
    graphdb.upsert_train(con6, "t5", "D5",
                         [("A", "甲", "----", "07:00"), ("B2", "乙二", "09:00", "----")], "d")
    p6 = GS.graph_search(con6, "A", "B", [D], min_gap=20, max_transfers=1)
    k6 = sorted((len(p) - 1, tuple(l[1] for l in p)) for p in p6)
    assert (0, ("G1",)) in k6, k6  # 兄弟站出发
    assert (0, ("D5",)) in k6, k6  # 兄弟站到达
    t3 = [p for p in p6 if tuple(l[1] for l in p) == ("K2", "K3")]
    assert len(t3) == 1, k6  # 惩罚后仍可行（等90分钟）
    w3 = int((t3[0][1][4] - t3[0][0][5]).total_seconds() // 60)
    assert w3 == 90, w3
    assert not [p for p in p6 if tuple(l[1] for l in p) == ("K2", "K4")], k6  # 惩罚后不够被拒
    print("6 ok: same-city seed/transfer/arrive =", k6)
finally:
    _S.SAME_CITY[:] = [g for g in _S.SAME_CITY if g not in ({"A", "A2"}, {"S1", "S2"}, {"B", "B2"})]
print("OFFLINE2 CHECKS PASSED")

# ---- 6) 接口契约：桌面/app 传的关键字参数必须真实存在
# （回归：_work_graph 传 max_gap 而定义叫 max_wait，线上直接 TypeError 弹窗）
from backend import graphsearch as _GS
from backend import search as _S
_gsig = inspect.signature(_GS.run_graph_search)
for _k in ("max_transfers", "min_gap", "max_gap", "verify_top", "window",
           "must_middle", "on_progress", "db_path"):
    assert _k in _gsig.parameters, _k
_ssig = inspect.signature(_S.search_combos)
for _k in ("max_transfer", "min_gap", "max_gap", "limit", "with_price",
           "must_middle", "breadth", "on_progress"):
    assert _k in _ssig.parameters, _k
print("6 ok: api contract")

# ---- 7) 慢直达强制入围 + 核验名额轮询（回归：Z14类次日到的便宜车被到达排序砍掉）
con7 = graphdb.connect(":memory:")
graphdb.upsert_train(con7, "d0", "Z0",
                     [("A", "甲", "----", "06:00"), ("B", "乙", "23:00", "----")], "d")
for i in range(5):
    graphdb.upsert_train(con7, f"m{i}", f"M{i}",
                         [("A", "甲", "----", f"0{i}:00"), ("X", "中", f"1{i}:00", f"1{i}:05")], "d")
    graphdb.upsert_train(con7, f"n{i}", f"N{i}",
                         [("X", "中", "----", f"1{i}:30"), ("B", "乙", f"1{i}:50", "----")], "d")
p7 = GS.graph_search(con7, "A", "B", [D], max_transfers=1, limit=3)
assert any(len(p) == 1 for p in p7), [(len(p), tuple(l[1] for l in p)) for p in p7]
print("7 ok: slow direct injected =", sorted({tuple(l[1] for l in p) for p in p7}))
# 轮询：直达/1次/2次各取一份
mk = lambda codes, k: {"codes": "+".join(codes), "transfers": k}
pool7 = [mk(["A"], 0), mk(["B"], 0)] + [mk([f"C{i}"], 1) for i in range(5)] + [mk(["D"], 2)]
got7 = GS.pick_verify(pool7, 4)
assert [c["transfers"] for c in got7] == [0, 1, 2, 0], [c["transfers"] for c in got7]
assert sorted(c["codes"] for c in GS.pick_verify(pool7, 100)) == sorted(c["codes"] for c in pool7)  # 不够分就全要（顺序轮询过）
print("7b ok: round-robin verify")
print("OFFLINE4 CHECKS PASSED")

# ---- 8) 分桶剪枝：晚到但接快车的不能被早到剪掉；同车过站不算换乘 ----
con8 = graphdb.connect(":memory:")
graphdb.upsert_train(con8, "e1", "E1",
                     [("A", "甲", "----", "00:05"), ("M", "中", "06:00", "06:05")], "d")
graphdb.upsert_train(con8, "e2", "E2",
                     [("M", "中", "----", "06:30"), ("B", "乙", "09:00", "----")], "d")
graphdb.upsert_train(con8, "l1", "L1",
                     [("A", "甲", "----", "10:00"), ("M", "中", "13:00", "13:05")], "d")
graphdb.upsert_train(con8, "l2", "L2",
                     [("M", "中", "----", "13:30"), ("B", "乙", "15:00", "----")], "d")
graphdb.upsert_train(con8, "s1", "S1",
                     [("A", "甲", "----", "07:00"), ("M", "中", "08:00", "08:02"),
                      ("B", "乙", "09:30", "----")], "d")
p8 = GS.graph_search(con8, "A", "B", [D], min_gap=20, max_transfers=1, limit=50)
k8 = sorted({tuple(l[1] for l in p) for p in p8})
assert ("L1", "L2") in k8, k8  # 晚到但自成体系，必须保留
assert ("E1", "E2") in k8, k8
assert ("S1",) in k8, k8  # 直达（S1的A->B边）
for p in p8:  # 无同车连续两段
    tns = [l[0] for l in p]
    assert all(a != b for a, b in zip(tns, tns[1:])), k8
print("8 ok: bucket pruning + same-train skip =", k8)
print("OFFLINE5 CHECKS PASSED")

# ---- 9) 实时兜底：图里太少就补，限流时不补 ----
import tempfile as _tf
import backend.search as _SL
tmp9 = _tf.mktemp(suffix=".db")
c9 = graphdb.connect(tmp9)
graphdb.upsert_train(c9, "t1", "G1",
                     [("VNP", "北京南", "----", "08:00"), ("AOH", "上海虹桥", "12:00", "----")], "d")
c9.close()
_r9r, _r9q, _r9p = api.resolve_station, api.query_tickets, api.cached_query_price
_real_live = _SL.search_combos
called9 = {"n": 0}


def _live_stub(*a, **k):
    called9["n"] += 1
    return {"combos": [{"codes": "LX", "total_price": 100, "total_min": 300, "transfers": 1,
                        "legs": [{"to_code": "M1", "to_name": "中转站"},
                                 {"to_code": "AOH", "to_name": "上海虹桥"}]}]}


api.resolve_station = lambda kw: {"北京": ("北京", "VNP"), "上海": ("上海", "AOH")}.get(
    (kw or "").strip(), (None, None))
api.query_tickets = lambda f, t, d, retries=1: ([], {})
api.cached_query_price = lambda *a: {}
_SL.search_combos = _live_stub
try:
    r9 = GS.run_graph_search("北京", "上海", D, max_transfers=1, verify_top=10,
                             window=False, db_path=tmp9)
    assert r9["live_added"] == 1 and called9["n"] == 1, (r9.get("live_added"), called9)
    assert any(c["codes"] == "LX" for c in r9["combos"]), [c["codes"] for c in r9["combos"]]
    print("9 ok: supplement when thin =", [(c["codes"], c["total_price"]) for c in r9["combos"]])
    # 限流（查询全抛错）时不补，避免雪上加霜
    called9["n"] = 0

    def _boom(f, t, d, retries=1):
        raise RuntimeError("限流")

    api.query_tickets = _boom
    r9b = GS.run_graph_search("北京", "上海", D, max_transfers=1, verify_top=10,
                              window=False, db_path=tmp9)
    assert called9["n"] == 0 and r9b["live_added"] == 0, (called9, r9b.get("live_added"))
    print("9b ok: no supplement under throttle")
finally:
    api.resolve_station, api.query_tickets, api.cached_query_price = _real_r, _real_q, _real_p
    _SL.search_combos = _real_live
    try:
        _os.remove(tmp9)
    except Exception:
        pass
print("OFFLINE6 CHECKS PASSED")

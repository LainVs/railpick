"""P0 回归：2 次中转枚举 + 建图缓存（上次提交引入的两处致命 bug）。

bug1  tmp.setdefault((m1,m2)).extend(...)  单参数版返回 None
      -> 只要 max_transfer>=2 且真有中转段可查，search_combos 100% AttributeError
bug2  2 次中转的配对循环缩进丢失，跑到了 for 外面
      -> 只用最后一对中转站，且 list_a/list_b 可能未定义（UnboundLocalError）
bug3  build_network 每次搜索重建整张图（7000 趟 / 55 万条边），
      现在 cached_network 按库签名缓存，库变了自动失效
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import inspect
import time
from backend import search as S
from backend import graphsearch as G
from backend import graphdb
from backend import c12306 as api

# ---- 1) 2 次中转：两对中转站都要出结果（覆盖 bug1 + bug2） ----
def T(f, t, no, code, st, du):
    return {"train_no": no, "code": code, "type_letter": "G", "train_type": "高铁",
            "from_code": f, "to_code": t, "from_no": "01", "to_no": "02",
            "start_time": st, "arrive_time": "00:00", "duration": "01:00",
            "duration_min": du, "seats": {"二等": "有"}, "seat_types": "9MO"}


ROWS = {
    ("BJP", "C_太原"): [T("BJP", "C_太原", "x1", "G1", "06:00", 120)],
    ("BJP", "C_郑州"): [T("BJP", "C_郑州", "x2", "G2", "06:30", 180)],
    ("C_太原", "C_郑州"): [T("C_太原", "C_郑州", "x3", "G3", "10:00", 120)],
    ("C_郑州", "C_太原"): [T("C_郑州", "C_太原", "x4", "G4", "10:00", 120)],
    ("C_太原", "SHH"): [T("C_太原", "SHH", "x5", "G5", "14:00", 120)],
    ("C_郑州", "SHH"): [T("C_郑州", "SHH", "x6", "G6", "14:00", 120)],
}

_r = (api.resolve_station, api.load_stations, api.query_tickets,
      api.cached_query_tickets, api.cached_query_price, S.candidate_middles)
api.resolve_station = lambda kw: (kw, {"北京": "BJP", "上海": "SHH"}.get(kw, "C_" + kw))
api.load_stations = lambda *a, **k: []
api.query_tickets = lambda f, t, d, retries=1: (ROWS.get((f, t), []), {})
api.cached_query_tickets = lambda f, t, d: (ROWS.get((f, t), []), {}, {})
api.cached_query_price = lambda *a: {"O": "¥100.0", "OT": [], "train_no": "x"}
S.candidate_middles = lambda *a, **k: [{"name": "太原", "code": "C_太原"},
                                       {"name": "郑州", "code": "C_郑州"}]
try:
    r = S.search_combos("北京", "上海", "2026-09-15", max_transfer=2,
                        with_price=True, breadth="quick")
    two = [c for c in r["combos"] if c["transfers"] == 2]
    assert len(two) == 2, [c["codes"] for c in two]   # 两条路径各一条
    codes = sorted(c["codes"] for c in two)
    assert codes == ["G1+G3+G6", "G2+G4+G5"], codes   # 两对中转站都枚举到了
    for c in two:
        assert c["total_price"] == 300.0, c["total_price"]
        assert c["total_min"] > 0
    print("1 ok: 2-transfer enumerates both middle pairs =", codes)
finally:
    (api.resolve_station, api.load_stations, api.query_tickets,
     api.cached_query_tickets, api.cached_query_price, S.candidate_middles) = _r
print("2TRANS CHECKS PASSED")

# ---- 2) setdefault 单参数是空值（bug1 的最小复现，防止有人改回去） ----
d = {}
try:
    d.setdefault(("a", "b")).extend([1])
    raise AssertionError("setdefault(k) 应当返回 None")
except AttributeError:
    pass
print("2 ok: setdefault needs default")

# ---- 3) 建图缓存：同库第二次命中，库变了自动重建 ----
import tempfile
tmp = tempfile.mktemp(suffix=".db")
try:
    con = graphdb.connect(tmp)
    graphdb.upsert_train(con, "t1", "G1",
                         [("A", "甲", "----", "08:00"), ("B", "乙", "12:00", "----")], "d")
    net1, n1 = G.cached_network(con)
    assert n1 == 1 and "A" in net1, (n1, list(net1))
    t0 = time.time()
    net2, n2 = G.cached_network(con)
    hit_ms = (time.time() - t0) * 1000
    assert net2 is net1, "第二次应命中同一份缓存对象"
    assert hit_ms < 50, f"缓存命中应 <50ms，实际 {hit_ms:.0f}ms"
    print(f"3 ok: network cache hit in {hit_ms:.1f}ms")

    # 入库新车 -> 签名变化 -> 自动重建（不会读到旧图）
    graphdb.upsert_train(con, "t2", "K2",
                         [("A", "甲", "----", "09:00"), ("C", "丙", "11:00", "----")], "d")
    net3, n3 = G.cached_network(con)
    assert n3 == 2 and net3 is not net1, (n3, net3 is net1)
    assert any(leg[4] == "C" for leg in net3.get("A", [])), net3.get("A")
    print("3b ok: cache invalidated after crawl")
    con.close()
finally:
    try:
        _os.remove(tmp)
    except Exception:
        pass

# ---- 4) 内存库不做缓存（测试互不污染） ----
mc = graphdb.connect(":memory:")
graphdb.upsert_train(mc, "m1", "G1",
                     [("A", "甲", "----", "08:00"), ("B", "乙", "12:00", "----")], "d")
mnet, mn = G.cached_network(mc)
mc2 = graphdb.connect(":memory:")
graphdb.upsert_train(mc2, "m2", "G2",
                     [("X", "西", "----", "08:00"), ("Y", "东", "12:00", "----")], "d")
mnet2, mn2 = G.cached_network(mc2)
assert "A" in mnet and "X" in mnet2, "内存库不能共用缓存"
print("4 ok: memory db not cached")

# ---- 5) 整数时间轴与 datetime 结果一致（对外接口不变） ----
net, n = G.build_network(mc)
res = G.graph_search(mc, "A", "B", ["2026-09-15"], max_transfers=0, _net=(net, n))
assert res and len(res[0]) == 1
tno, code, frm, dst, dep, arr = res[0][0]
from datetime import datetime
assert isinstance(dep, datetime) and isinstance(arr, datetime), (type(dep), type(arr))
assert dep.isoformat().startswith("2026-09-15T08:00"), dep.isoformat()
assert arr.isoformat().startswith("2026-09-15T12:00"), arr.isoformat()
print("5 ok: datetime contract kept =", dep.isoformat(), "->", arr.isoformat())

# ---- 6) 接口契约：graph_search 参数名不变（桌面/app 都按名传） ----
sig = inspect.signature(G.graph_search)
for k in ("dates", "min_gap", "max_gap", "max_transfers", "max_total",
          "limit", "on_progress", "should_stop"):
    assert k in sig.parameters, k
print("6 ok: graph_search signature")
print("P0 CHECKS PASSED")

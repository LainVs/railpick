"""P0 回归：候选站按车型分组 + 代表席别价（纯离线，路由函数打桩）。"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from backend import search as S
from backend import c12306 as api

# ---- 1) 分组取源：前 6 趟全是 K 时，高铁站也必须出现 ----
def T(code, tl, no):
    return {"code": code, "type_letter": tl, "train_no": no,
            "from_code": "BJP", "to_code": "CSQ"}

trains = [T(f"K10{i}", "K", f"kn{i}") for i in range(6)] + \
         [T(f"G10{i}", "G", f"gn{i}") for i in range(4)]

def fake_route(train_no, f, t, d):
    if train_no.startswith("gn"):
        return {"data": [{"station_name": "南京南", "isEnabled": True}]}
    return {"data": [{"station_name": "保定", "isEnabled": True}]}

api.cached_query_route = fake_route
m = S.candidate_middles("BJP", "CSQ", "2026-09-10", trains)
names = [x["name"] for x in m]
assert "南京南" in names, names
assert "保定" in names, names
print("1 ok: mixed middles =", names)

# ---- 2) 代表席别价 ----
d = {"O": "¥584.0", "WZ": "¥584.0", "M": "¥969.0", "A9": "¥2060.0", "OT": [], "train_no": "x"}
assert api.cheapest_price(d, prefer="二等") == (584.0, "二等")
assert api.cheapest_price(d, prefer="硬座")[0] == 584.0  # 缺席别回退最低
assert api.cheapest_price({}, prefer="二等") == (None, None)
print("2 ok: prefer pricing")

# ---- 3) 旧快照：全慢车输入不崩 ----
m2 = S.candidate_middles("BJP", "CSQ", "2026-09-10", trains[:6])
assert isinstance(m2, list)
print("3 ok: slow-only input =", [x["name"] for x in m2])

# ---- 4) 单源限流：一个车 10 站也只取 4 站 ----
def fake_many(train_no, f, t, d):
    return {"data": [{"station_name": n, "isEnabled": True}
                     for n in ["保定", "定州", "石家庄", "邢台", "邯郸",
                               "安阳", "鹤壁", "新乡", "郑州", "许昌"]]}
api.cached_query_route = fake_many
m3 = S.candidate_middles("BJP", "CSQ", "2026-09-10", trains[:1], limit=4)
got3 = [x["name"] for x in m3]
assert got3[:3] == ["保定", "定州", "石家庄"] and "邢台" not in got3, got3  # 经停占3席，剩下让给图库/枢纽
print("4 ok: per-train cap + slot budget =", got3)

# ---- 5) 同城去重：长沙南不再当选中转站 ----
assert not S.same_city("CTQ", "CSQ") is False
assert S.same_city("TMK", "TAK") is True  # 唐山-费县案：K到泰山、G到泰安
assert S.same_city("TSP", "FUP") is True  # 广州-唐山案：Z14到唐山北
assert "泰安" in S.HUBS
print("5 ok: same-city Changsha+Taian")

# ---- 6) 西安-太原案：B 段前 10 趟全在 A 到达之前发走，也必须找到下午的配对 ----
def L(code, st, dur, fc="AAA", tc="BBB"):
    h, m = map(int, st.split(":"))
    from datetime import datetime, timedelta, time as dtime, date as ddate
    dep = datetime.combine(ddate(2026, 9, 10), dtime(h, m))
    return {"code": code, "start_time": st, "duration_min": dur, "from_code": fc,
            "to_code": tc, "_dep": dep, "_arr": dep + timedelta(minutes=dur)}
early_b = [L(f"B{i}", f"0{i}:00", 60, fc="MID") for i in range(4, 8)]
late_b = [L("BGOOD", "15:00", 120, fc="MID")]
a = L("A1", "07:45", 206, fc="AAA", tc="MID")  # 11:11 到达 MID
pairs = S.pair_legs([a], early_b + late_b, 20, 240, per_a=4)
assert any(b["code"] == "BGOOD" for _, b, _ in pairs), pairs
print("6 ok: afternoon pairing, waits =", [w for _, _, w in pairs])

# ---- 7) 广度档位：quick < normal < deep，且计数器可用 ----
q, n, d = S.BREADTHS["quick"], S.BREADTHS["normal"], S.BREADTHS["deep"]
assert q["middles"] < n["middles"] < d["middles"]
assert q["per_a"] <= n["per_a"] <= d["per_a"]
assert q["cap1"] < n["cap1"] < d["cap1"]
assert d["always_next"] is True and n["always_next"] is False
api.reset_req_count()
assert api.get_req_count() == 0
print("7 ok: breadth presets + req counter")

# ---- 8) 走廊排序：唐山->临沂，泰安/济南/天津必须排在郑州/武汉/广州前面 ----
from backend import geo as G
assert len(G.CITY) > 200, len(G.CITY)
assert G.coords_for("泰安") == (36.20, 117.10)
assert G.coords_for("费县北") == G.coords_for("临沂")
assert G.coords_for("泰山") == G.coords_for("泰安")
assert G.coords_for("火星") is None
ranked = G.rank_hubs("唐山", "费县北", ["广州", "武汉", "郑州", "泰安", "济南", "天津", "徐州"])
pos = {h: i for i, h in enumerate(ranked)}
assert pos["泰安"] < pos["郑州"] and pos["济南"] < pos["武汉"] and pos["天津"] < pos["广州"], ranked
# 无坐标回退静态顺序
assert G.rank_hubs("火星", "费县北", ["广州", "泰安"]) == ["广州", "泰安"]
print("8 ok: corridor rank =", ranked)

# ---- 8b) 第二走廊：北京->广州，京广线城市靠前，东北/西北沉底 ----
r2 = G.rank_hubs("北京", "广州", ["哈尔滨", "乌鲁木齐", "石家庄", "郑州", "长沙", "上海"])
p2 = {h: i for i, h in enumerate(r2)}
assert p2["石家庄"] < p2["哈尔滨"] and p2["郑州"] < p2["乌鲁木齐"] and p2["长沙"] < p2["上海"], r2
# 大枢纽加成：顺位接近时优先
r3 = G.rank_hubs("唐山", "费县北", ["廊坊", "济南"], prefer={"济南"})
assert r3[0] == "济南", r3
print("8b ok:", r2, r3)

# ---- 8c) 长线自适应加成：广州->唐山（1900km），天津必须进前6 ----
r4 = G.rank_hubs("广州", "唐山", ["兖州", "枣庄", "沧州", "天津", "北京", "石家庄",
                                 "曲阜东", "泰安", "济南", "徐州", "武汉", "长沙"],
                 prefer={"天津", "北京", "石家庄", "济南", "徐州", "武汉", "长沙"})
p4 = {h: i for i, h in enumerate(r4)}
assert p4["天津"] < 6, r4
assert p4["北京"] < p4["石家庄"], r4  # 同为trunk，北京比石家庄顺
print("8c ok:", r4[:8])

# ---- 9) 候选接受走廊排序（direct 为空纯走枢纽） ----
import backend.graphdb as _G
_G.common_middles = lambda con, f, t, limit=8: []  # 图置空，只看枢纽顺序
m = S.candidate_middles("TSP", "FBK", "2026-09-15", [], limit=5,
                        hub_order=["泰安", "济南", "郑州"])
assert [x["name"] for x in m] == ["泰安", "济南", "郑州"], [x["name"] for x in m]
print("9 ok: hub_order passthrough")
print("GROUP CHECKS PASSED")

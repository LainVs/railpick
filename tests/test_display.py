"""展示层回归：同城异站换乘必须标清楚 + 站名降级兜底。

背景（真实数据暴露的）：
- 唐山->费县最顺的方案是 T244 到「泰山」，再 G5556 从「泰安」发。
  泰山(TAK) 和泰安(TMK) 是两个站，同城换乘要 +60 分钟惩罚。
  之前详情只写「泰山换乘 94 分钟」，看着像行程断了，用户根本不知道要跑去另一个站。
- path_to_combo 不产出站名，全靠 enrich_names；站名库降级（网络抖动只剩 75 站）时
  直接显示 TAK/TMK。图库 stops.name 里明明有名字却没被用上。
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from backend import c12306 as api
from backend import search as S
from backend import graphsearch as G
from backend import graphdb

# ---- 1) enrich_names 优先级：smap > 图库名字 > 站名库 > 站码 ----
api.load_stations()
combo = {"legs": [{"from_code": "TAK", "to_code": "TMK"}]}
S.enrich_names([combo], {"TAK": "接口给的泰山"})
assert combo["legs"][0]["from_name"] == "接口给的泰山", combo
assert combo["legs"][0]["to_name"] == "泰安", combo  # 站名库兜底

combo2 = {"legs": [{"from_code": "TAK", "to_code": "TMK"}]}
S.enrich_names([combo2], {}, graph_names={"TAK": "泰山", "TMK": "泰安"})
assert combo2["legs"][0]["from_name"] == "泰山", combo2
assert combo2["legs"][0]["to_name"] == "泰安", combo2

# 站名库降级时，图库名字仍能救回来
saved = dict(api._stations_cache)
try:
    api._stations_cache.update({"list": [], "by_name": {}, "by_code": {}, "ts": 0,
                                "fallback": True})
    combo3 = {"legs": [{"from_code": "TAK", "to_code": "TMK"}]}
    S.enrich_names([combo3], {}, graph_names={"TAK": "泰山", "TMK": "泰安"})
    assert combo3["legs"][0]["from_name"] == "泰山", combo3
    assert combo3["legs"][0]["to_name"] == "泰安", combo3
    print("1 ok: enrich_names priority + degraded fallback")
finally:
    api._stations_cache.update(saved)

# ---- 2) run_graph_search 会用图库名字补名（不依赖在线站名库） ----
saved2 = dict(api._stations_cache)
import tempfile as _tf
_tmpdb = _tf.mktemp(suffix=".db")   # 必须用临时库：写真实 timetable.db 会污染用户数据
try:
    api._stations_cache.update({"list": [], "by_name": {}, "by_code": {}, "ts": 0,
                                "fallback": True})
    con = graphdb.connect(_tmpdb)
    try:
        graphdb.upsert_train(con, "t1", "T244",
                             [("TSP", "唐山", "----", "01:04"), ("TAK", "泰山", "06:31", "06:34")], "d")
        graphdb.upsert_train(con, "t2", "G5556",
                             [("TMK", "泰安", "----", "08:05"), ("FBK", "费县北", "09:04", "----")], "d")
        net, n = G.build_network(con)
        paths = G.graph_search(con, "TSP", "FBK", ["2026-10-23"], max_transfers=1,
                               min_gap=20, max_gap=240, _net=(net, n))
        assert paths, "泰山->泰安 同城换乘应能配上"
        combos = [G.path_to_combo(p) for p in paths]
        gnames = {r["station"]: r["name"]
                  for r in con.execute("SELECT DISTINCT station,name FROM stops")}
        S.enrich_names(combos, {}, graph_names=gnames)
        names = {(l["from_name"], l["to_name"]) for c in combos for l in c["legs"]}
        assert ("唐山", "泰山") in names, names
        assert ("泰安", "费县北") in names, names
        print("2 ok: graph search names from db =", sorted(names))
    finally:
        con.close()
finally:
    api._stations_cache.update(saved2)
    try:
        _os.remove(_tmpdb)
    except Exception:
        pass

# ---- 3) 桌面详情/清单要标出同城异站 ----
try:
    import tkinter as tk
    from desktop import App
except Exception as e:
    print("SKIP: tkinter 不可用（%s）" % e)
    print("DISPLAY CHECKS PASSED")
    raise SystemExit(0)

root = tk.Tk()
root.withdraw()
try:
    app = App(root)
    c = {
        "codes": "T244+G5556", "total_price": 161, "total_min": 480, "transfers": 1,
        "combo_kind": "全普速", "waits": [94], "dep_date": "2026-10-23",
        "arr_date": "2026-10-23",
        "legs": [
            {"code": "T244", "type_letter": "T", "train_type": "特快",
             "start_time": "01:04", "arrive_time": "06:31", "duration": "05:27",
             "duration_min": 327, "from_code": "TSP", "to_code": "TAK",
             "from_name": "唐山", "to_name": "泰山", "dep_plus": 0, "arr_plus": 0,
             "price": 60, "price_name": "硬座", "seats": {"硬座": "有"}},
            {"code": "G5556", "type_letter": "G", "train_type": "高铁",
             "start_time": "08:05", "arrive_time": "09:04", "duration": "00:59",
             "duration_min": 59, "from_code": "TMK", "to_code": "FBK",
             "from_name": "泰安", "to_name": "费县北", "dep_plus": 0, "arr_plus": 0,
             "price": 101, "price_name": "二等", "seats": {"二等": "有"}},
        ],
    }
    detail = "\n".join(t for t, _ in app.combo_detail_parts(c))
    assert "同城异站" in detail, "详情没标同城异站：" + detail
    assert "泰山" in detail and "泰安" in detail, detail
    buy = app.combo_buy_text(c)
    assert "同城异站" in buy, "购票清单没标同城异站：" + buy

    # 同站换乘不该出现“同城异站”
    c2 = {"codes": "A+B", "total_price": 100, "total_min": 200, "transfers": 1,
          "combo_kind": "全高铁", "waits": [40], "dep_date": "2026-10-23",
          "arr_date": "2026-10-23",
          "legs": [
              {"code": "G1", "type_letter": "G", "train_type": "高铁",
               "start_time": "08:00", "arrive_time": "09:00", "duration": "01:00",
               "duration_min": 60, "from_code": "A", "to_code": "M",
               "from_name": "甲", "to_name": "中转", "dep_plus": 0, "arr_plus": 0,
               "price": 50, "price_name": "二等", "seats": {"二等": "有"}},
              {"code": "G2", "type_letter": "G", "train_type": "高铁",
               "start_time": "09:40", "arrive_time": "11:00", "duration": "01:20",
               "duration_min": 80, "from_code": "M", "to_code": "B",
               "from_name": "中转", "to_name": "乙", "dep_plus": 0, "arr_plus": 0,
               "price": 50, "price_name": "二等", "seats": {"二等": "有"}},
          ]}
    d2 = "\n".join(t for t, _ in app.combo_detail_parts(c2))
    assert "同城异站" not in d2, "同站换乘误报同城异站：" + d2
    print("3 ok: desktop marks same-city-different-station")
finally:
    root.destroy()
print("DISPLAY CHECKS PASSED")

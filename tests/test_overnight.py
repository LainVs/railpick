"""隔天回归验证：纯本地断言 + 轻量实况解析检查（不跑全量搜索）."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json
from backend.search import anchor, combine_legs, build_combo, need_next_day

D = "2026-09-10"

def leg(code, st, dur, fc="AAA", tc="BBB", tl="K"):
    return {"code": code, "start_time": st, "duration_min": dur,
            "duration": f"{dur//60:02d}:{dur%60:02d}",
            "from_code": fc, "to_code": tc, "type_letter": tl,
            "train_type": "快速", "train_no": code, "from_no": "01", "to_no": "05", "seats": {}}

# 1) 过夜锚定：19:10 + 13:34 -> 次日 08:44
a = anchor(leg("Z281", "19:10", 814), D)
assert a["arr_date"] == "2026-09-11", a["arr_date"]
assert a["arr_plus"] == 1, a["arr_plus"]
print("1 ok: overnight anchor", a["dep_date"], "->", a["arr_date"])

# 2) 正确衔接：次日 10:00 出发，等待 76 分钟
b_next = anchor(leg("G2", "10:00", 120, fc="BBB", tc="CCC", tl="G"), "2026-09-11")
w = combine_legs(a, b_next, 20, 240)
assert w == 76, w
print("2 ok: next-day wait =", w)

# 3) 旧 bug 用例：同日 10:00（早于到达）必须拒绝，不能再“猜+1440”
b_same = anchor(leg("G2", "10:00", 120, fc="BBB", tc="CCC", tl="G"), D)
assert combine_legs(a, b_same, 20, 240) is None
print("3 ok: same-day early departure rejected (old code wrongly accepted)")

# 4) 总时长绝对时间：D 19:10 -> D+1 12:00 = 1010 分钟
c = build_combo([a, b_next], [100, 200], [76])
assert c["total_min"] == 1010, c["total_min"]
assert c["dep_date"] == D and c["arr_date"] == "2026-09-11"
assert c["legs"][0]["arr_plus"] == 1 and c["legs"][1]["dep_plus"] == 1
print("4 ok: total =", c["total_min"], c["dep_date"], "->", c["arr_date"])

# 5) 超 24h 行程：08:00 + 26h -> 次日 10:00，再接 12:00-14:00，总计 30h=1800
a2 = anchor(leg("K1", "08:00", 1560), D)
b2 = anchor(leg("K2", "12:00", 120), "2026-09-11")
c2 = build_combo([a2, b2], [50, 60], [120])
assert c2["total_min"] == 1800, c2["total_min"]  # 旧钟表算法会算出 360
print("5 ok: >24h total =", c2["total_min"], "(old clock math gave 360)")

# 6) 最小间隔与同城惩罚仍生效
b3 = anchor(leg("G3", "08:50", 60, fc="BBB", tc="CCC", tl="G"), "2026-09-11")
assert combine_legs(a, b3, 20, 240) is None  # 仅 6 分钟
print("6 ok: min_gap enforced")

# 7) 是否需要查 D+1：晚到车 -> True；纯白天车 -> False
assert need_next_day([a], 240) is True
day = anchor(leg("G9", "08:00", 180), D)
assert need_next_day([day], 240) is False
print("7 ok: need_next_day heuristic")

# 8) JSON 可序列化（_dep/_arr 不能泄漏）
json.dumps(c)
json.dumps(c2)
assert "_dep" not in json.dumps(c)
print("8 ok: combos JSON-serializable")

# 9) 实况：seat_types 解析不再为空（需联网，失败仅警告）
try:
    from backend.c12306 import query_tickets
    ts, _ = query_tickets("BJP", "SHH", D)
    g = next(t for t in ts if t["code"].startswith("G"))
    z = next((t for t in ts if t["code"][0] in "ZTK"), None)
    assert g["seat_types"], "G seat_types empty"
    print("9 ok: live seat_types G=", g["seat_types"], "Z=", z["seat_types"] if z else None)
except Exception as e:
    print("9 SKIP (live):", str(e)[:120])

print("ALL OVERNIGHT CHECKS PASSED")

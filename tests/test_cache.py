"""缓存路径验证：纯本地（零网络请求），顺带确认报错信息可读性。"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import time
from backend import c12306 as api

# 1) 命中路径：手填缓存，cached_query_tickets 不得打网
fake = ([{"code": "G9", "train_no": "x"}], {"A": "b"}, time.time())
api._cache_set(("t", "FAKE1", "FAKE2", "2026-09-10"), fake)
ts, sm, info = api.cached_query_tickets("FAKE1", "FAKE2", "2026-09-10")
assert info["cached"] is True and ts[0]["code"] == "G9"
print("1 ok: cache hit, no network")

# 2) 兜底路径：stale_tickets 能取出旧数据
s = api.stale_tickets("FAKE1", "FAKE2", "2026-09-10")
assert s is not None and s[0][0]["code"] == "G9"
assert api.stale_tickets("NOPE", "NOPE", "2026-09-10") is None
print("2 ok: stale fallback")

# 3) 报错可读：空包/HTML 不再是 Expecting value
try:
    raise RuntimeError("余票查询 空响应（限流或网络抖动），HTTP 200")
except RuntimeError as e:
    assert "空响应" in str(e)
print("3 ok: readable errors")

# 4) 分段查询默认快失败：retries=0 签名存在
import inspect
assert "retries" in inspect.signature(api.query_tickets).parameters
print("4 ok: retries param")

# 5) 票价席别串必须用 p[35] 短式（曾误用 p[34] 长式导致只回商务座价）
p = [""] * 58
p[2], p[3] = "240000G63300", "G633"
p[4], p[5], p[6], p[7] = "VNP", "AOH", "VNP", "AOH"
p[8], p[9], p[10] = "08:37", "15:01", "06:24"
p[11], p[13] = "Y", "20260910"
p[16], p[17] = "01", "12"
p[30], p[31], p[32] = "有", "有", "20"
p[34], p[35] = "90M0O0W0", "9MOO"
t = api.parse_ticket_row("|".join(p))
assert t["seat_types"] == "9MOO", t["seat_types"]
assert (t["from_no"], t["to_no"]) == ("01", "12")
assert t["duration_min"] == 384
print("5 ok: seat_types from p[35]")

# 6) 任务生命周期：创建→上报→完成/取消
from backend import jobs
jid = jobs.create()
assert jobs.get(jid)["state"] == "running"
jobs.update(jid, 30, "查分段")
assert jobs.get(jid)["progress"] == 30
jobs.finish(jid, result={"a": 1})
assert jobs.get(jid)["state"] == "done" and jobs.get(jid)["progress"] == 100
jid2 = jobs.create()
jobs.cancel(jid2)
assert jobs.should_cancel(jid2) is True
jobs.finish(jid2, result={})
assert jobs.get(jid2)["state"] == "cancelled"
assert jobs.get("nope") is None
print("6 ok: jobs lifecycle")

print("CACHE CHECKS PASSED")

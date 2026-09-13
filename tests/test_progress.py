"""进度上报验证：仅直达查询（约1个请求），断言进度事件单调到100。"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
from backend.search import search_combos

events = []
r = search_combos("北京", "上海", "2026-09-10", max_transfer=0, limit=5,
                  with_price=False,
                  on_progress=lambda f, m: events.append((round(f, 3), m)))
assert r.get("combos"), r
fracs = [f for f, _ in events]
assert fracs == sorted(fracs), fracs
assert fracs[-1] == 1.0, fracs
assert any("直达" in m for _, m in events), events
print("progress events:", [(f, m) for f, m in events])
print("PROGRESS OK")

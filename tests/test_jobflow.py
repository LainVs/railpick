"""HTTP任务流验证：提交→轮询进度→完成取结果（仅直达，约1个请求）。"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8001"


def post(path):
    req = urllib.request.Request(BASE + path, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=30))


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


s = post("/api/search?from=%E5%8C%97%E4%BA%AC&to=%E4%B8%8A%E6%B5%B7&date=2026-09-10"
         "&max_transfer=0&limit=5&with_price=0")
jid = s["job_id"]
print("job:", jid)
seen = []
for _ in range(30):
    st = get(f"/api/search/{jid}")
    seen.append((st["progress"], st["stage"]))
    if st["state"] in ("done", "error", "cancelled"):
        break
    time.sleep(1)
print("final:", st["state"], "combos:", len((st.get("result") or {}).get("combos", [])))
progs = [p for p, _ in seen]
assert progs == sorted(progs) and progs[-1] == 100, seen
assert st["state"] == "done" and st["result"]["combos"]
# 取消接口（对已完成任务应返回False，不崩）
print("cancel-done:", post(f"/api/search/{jid}/cancel"))
print("cancel-fake:", post("/api/search/doesnotexist/cancel"))
print("JOBFLOW OK")

"""爬取接口接线验证：启动即取消（零请求消耗），只验通道。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8001"


def post(path):
    req = urllib.request.Request(BASE + path, method="POST")
    return json.load(urllib.request.urlopen(req, timeout=30))


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


s = post("/api/graph/crawl?max_requests=10")
assert s.get("started") and s.get("job_id"), s
jid = s["job_id"]
print("crawl job:", jid)
c = post(f"/api/search/{jid}/cancel")
print("cancel:", c)
st = None
for _ in range(25):
    time.sleep(1)
    st = get(f"/api/search/{jid}")
    if st["state"] in ("done", "error", "cancelled"):
        break
assert st["state"] == "cancelled", st
print("state:", st["state"])
g = get("/api/graph/stats")
assert "trains" in g and "stations" in g, g
print("graph stats:", g)
# 重复启动互斥
s2 = post("/api/graph/crawl?max_requests=10")
print("second start:", s2)
if s2.get("started"):
    post(f"/api/search/{s2['job_id']}/cancel")
print("CRAWL WIRING OK")

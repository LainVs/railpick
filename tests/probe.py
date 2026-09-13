"""实查探针：跑一次完整搜索，把关键指标打印成文本报告存文件。"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import json, sys, time
from backend.search import search_combos

def main(frm, to, date, mt, wp, limit, out, breadth="normal"):
    t0 = time.time()
    r = search_combos(frm, to, date, max_transfer=mt, limit=limit, with_price=wp, breadth=breadth)
    dt = time.time() - t0
    L = []
    L.append(f"query {frm}->{to} {date} mt={mt} price={wp} time={dt:.1f}s breadth={breadth}")
    L.append(f"direct={r.get('direct_count')} combos={len(r.get('combos', []))} "
             f"cached={r.get('cached')} stale={r.get('stale')} mock={r.get('mock')} warn={r.get('warn')}")
    L.append(f"stats={r.get('stats')}")
    L.append("middles=" + ",".join(f"{m['name']}" for m in r.get("middles", [])))
    for c in r.get("combos", [])[:25]:
        segs = []
        for l in c["legs"]:
            segs.append(f"{l['code']}[{l.get('from_name','?')}{l['start_time']}+{l.get('dep_plus',0)}"
                        f"->{l.get('to_name','?')}{l['arrive_time']}+{l.get('arr_plus',0)}"
                        f" {l['duration_min']}m ¥{l.get('price')}/{l.get('price_name')}]")
        L.append(f"{c['codes']} T={c['transfers']} total={c['total_min']}m ¥{c['total_price']} "
                 f"waits={c['waits']} kind={c['combo_kind']} {c.get('dep_date')}->{c.get('arr_date')} | " + " + ".join(segs))
    # 异常扫描
    bad = []
    for c in r.get("combos", []):
        if c["total_min"] <= 0:
            bad.append(("total<=0", c["codes"]))
        if any(w < 0 for w in c.get("waits", [])):
            bad.append(("wait<0", c["codes"]))
        if c["transfers"] > 0:
            expect = sum(l["duration_min"] for l in c["legs"]) + sum(c["waits"])
            if abs(expect - c["total_min"]) > 2:
                bad.append((f"total mismatch {expect}!={c['total_min']}", c["codes"]))
        for l in c["legs"]:
            if not l.get("from_name") or l["from_name"] == l.get("from_code"):
                bad.append(("name unresolved", f"{c['codes']}:{l.get('from_code')}->{l.get('to_code')}"))
                break
    L.append(f"ANOMALIES={len(bad)}")
    for b in bad[:20]:
        L.append("  !" + str(b))
    open(out, "w", encoding="utf-8").write("\n".join(L))
    safe = "\n".join(L[:8]).replace("¥", "CNY")
    print(safe)
    print("...saved", out, f"anomalies={len(bad)}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]),
         bool(int(sys.argv[5])), int(sys.argv[6]), sys.argv[7],
         sys.argv[8] if len(sys.argv) > 8 else "normal")

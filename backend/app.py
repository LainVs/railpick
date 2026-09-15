from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os
import threading as _th
from . import c12306 as api
from . import jobs
from .search import search_combos
from .cancel import SearchCancelled

app = FastAPI(title="铁行择优 RailPick")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONT = os.path.join(BASE, "frontend")

@app.get("/api/health")
def health():
    return {"ok": True}

@app.get("/api/stations")
def stations(q: str = Query(default=""), limit: int = 10):
    try:
        return {"stations": api.search_stations(q, limit)}
    except Exception as e:
        return {"stations": [], "error": str(e)}

@app.get("/api/direct")
def direct(from_: str = Query(alias="from"), to: str = Query(alias="to"), date: str = Query(...)):
    fn, fc = api.resolve_station(from_)
    tn, tc = api.resolve_station(to)
    if not fc or not tc:
        return JSONResponse({"error": "未找到车站", "combos": []}, status_code=400)
    try:
        trains, smap, tinfo = api.cached_query_tickets(fc, tc, date)
        out = {"from": fn, "to": tn, "date": date, "count": len(trains), "trains": trains[:50]}
        if tinfo.get("cached"):
            out["cached"] = True
        return out
    except Exception as e:
        return JSONResponse({"error": str(e), "trains": []}, status_code=502)

@app.get("/api/combo")
def combo(
    from_: str = Query(alias="from"),
    to: str = Query(alias="to"),
    date: str = Query(...),
    max_transfer: int = Query(default=1, ge=0, le=4),
    min_gap: int = Query(default=20, ge=0, le=300),
    max_gap: int = Query(default=240, ge=30, le=1440),
    limit: int = Query(default=30, ge=5, le=100),
    with_price: int = Query(default=1),
    must_middle: str = Query(default=""),
    breadth: str = Query(default="normal"),
):
    try:
        res = search_combos(from_, to, date, max_transfer, min_gap, max_gap, limit, bool(with_price),
                            must_middle=must_middle, breadth=breadth)
        return res
    except Exception as e:
        return JSONResponse({"error": str(e), "combos": []}, status_code=502)


def _job_params(from_, to, date, max_transfer, min_gap, max_gap, limit, with_price,
                must_middle, breadth):
    return dict(from_=from_, to=to, date=date, max_transfer=max_transfer,
                min_gap=min_gap, max_gap=max_gap, limit=limit,
                with_price=with_price, must_middle=must_middle, breadth=breadth)


@app.post("/api/search")
def search_start(
    from_: str = Query(alias="from"),
    to: str = Query(alias="to"),
    date: str = Query(...),
    max_transfer: int = Query(default=1, ge=0, le=4),
    min_gap: int = Query(default=20, ge=0, le=300),
    max_gap: int = Query(default=240, ge=30, le=1440),
    limit: int = Query(default=30, ge=5, le=100),
    with_price: int = Query(default=1),
    must_middle: str = Query(default=""),
    breadth: str = Query(default="normal"),
):
    """异步搜索：立刻返回 job_id，前端轮询 /api/search/{id} 看进度。"""
    params = _job_params(from_, to, date, max_transfer, min_gap, max_gap, limit,
                         with_price, must_middle, breadth)
    jid = jobs.create()

    def run():
        def cb(frac, msg):
            if jobs.should_cancel(jid):
                raise jobs.Cancelled()
            jobs.update(jid, frac * 100, msg)
        try:
            res = search_combos(params["from_"], params["to"], params["date"],
                                params["max_transfer"], params["min_gap"],
                                params["max_gap"], params["limit"],
                                bool(params["with_price"]),
                                must_middle=params["must_middle"],
                                breadth=params["breadth"], on_progress=cb,
                                should_stop=lambda: jobs.should_cancel(jid))
            jobs.finish(jid, result=res)
        except (jobs.Cancelled, SearchCancelled):
            jobs.finish(jid, cancelled=True)
        except Exception as e:
            jobs.finish(jid, error=str(e))

    _th.Thread(target=run, daemon=True).start()
    return {"job_id": jid}


@app.get("/api/search/{jid}")
def search_status(jid: str):
    j = jobs.get(jid)
    if not j:
        return JSONResponse({"error": "任务不存在或已过期"}, status_code=404)
    out = {"state": j["state"], "progress": j["progress"], "stage": j["stage"]}
    if j["state"] == "done":
        out["result"] = j["result"]
    if j["state"] == "error":
        out["error"] = j["error"]
    return out


@app.post("/api/search/{jid}/cancel")
def search_cancel(jid: str):
    return {"cancelled": jobs.cancel(jid)}


@app.post("/api/gsearch")
def gsearch_start(
    from_: str = Query(alias="from"),
    to: str = Query(alias="to"),
    date: str = Query(...),
    max_transfers: int = Query(default=2, ge=1, le=4),
    min_gap: int = Query(default=20, ge=0, le=300),
    max_gap: int = Query(default=240, ge=30, le=1440),
    limit: int = Query(default=300, ge=10, le=2000),
    verify_top: int = Query(default=40, ge=5, le=1000),
    window: int = Query(default=1, ge=0, le=1),
    must_middle: str = Query(default=""),
):
    """图库深挖：离线无上限枚举 + 实时核验头部，进度走同一任务接口。"""
    jid = jobs.create()

    def run():
        def cb(frac, msg):
            if jobs.should_cancel(jid):
                raise jobs.Cancelled()
            jobs.update(jid, frac * 100, msg)
        try:
            from . import graphsearch as GS
            res = GS.run_graph_search(from_, to, date, max_transfers=max_transfers,
                                      min_gap=min_gap, max_gap=max_gap, limit=limit,
                                      verify_top=verify_top, window=bool(window),
                                      must_middle=must_middle, on_progress=cb,
                                      should_stop=lambda: jobs.should_cancel(jid))
            jobs.finish(jid, result=res)
        except (jobs.Cancelled, SearchCancelled):
            jobs.finish(jid, cancelled=True)
        except Exception as e:
            jobs.finish(jid, error=str(e))

    _th.Thread(target=run, daemon=True).start()
    return {"job_id": jid}


@app.get("/api/graph/stats")
def graph_stats():
    from . import graphdb
    try:
        con = graphdb.connect()
        try:
            return graphdb.stats(con)
        finally:
            con.close()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


_crawl_running = {"on": False}
_crawl_lock = _th.Lock()


@app.post("/api/graph/crawl")
def graph_crawl(max_requests: int = Query(default=1000, ge=0, le=5000),
                from_stations: str = Query(default=""),
                phase: str = Query(default="auto")):
    """一键爬取：后台礼貌爬时刻表，复用任务进度接口轮询。from_stations 逗号分隔优先站。"""
    with _crawl_lock:
        if _crawl_running["on"]:
            return {"started": False, "reason": "已有爬取在跑"}
        _crawl_running["on"] = True
    jid = jobs.create()
    import re
    prio = [x for x in re.split(r"[,，]", from_stations) if x.strip()]

    def run():
        stop = lambda: jobs.should_cancel(jid)

        def cb(u, m, msg):
            if stop():
                raise jobs.Cancelled()
            jobs.update(jid, (u / m * 100) if m else 0, msg)
        try:
            from . import crawler
            st = crawler.run(max_requests=max_requests, on_progress=cb, should_stop=stop,
                             priority_from=prio, phase=phase if phase in ("auto", "mesh", "fanout") else "auto")
            jobs.finish(jid, result=st)
        except Exception as e:
            jobs.finish(jid, error=str(e))
        finally:
            _crawl_running["on"] = False

    _th.Thread(target=run, daemon=True).start()
    return {"started": True, "job_id": jid}

@app.get("/api/route")
def route(train_no: str, from_: str = Query(alias="from"), to: str = Query(alias="to"), date: str = Query(...)):
    try:
        return api.query_route(train_no, from_, to, date)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

@app.get("/")
def index():
    return FileResponse(os.path.join(FRONT, "index.html"))

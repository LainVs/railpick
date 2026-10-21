"""时刻表图数据库（SQLite，标准库，无依赖）.

存什么：每趟车的完整经停站序列。1 次 czxx 查询 = 一整趟车的站，
比站站对查余票便宜两个数量级——这就是主动爬能跑通的原因。

查什么：common_middles(A, B) = 既在“A下游”又在“B上游”的站，
按车次频度排序。结果只做候选，最终以实时余票校验为准。
"""
import os
import sqlite3
import threading

_lock = threading.Lock()
_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "timetable.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trains(
  train_no TEXT PRIMARY KEY, code TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS stops(
  train_no TEXT, seq INTEGER, station TEXT, name TEXT,
  arrive TEXT, depart TEXT,
  PRIMARY KEY(train_no, seq));
CREATE INDEX IF NOT EXISTS idx_stops_station ON stops(station);
CREATE TABLE IF NOT EXISTS crawl_od(
  frm TEXT, too TEXT, day TEXT, done INTEGER DEFAULT 0, fresh INTEGER DEFAULT 0,
  PRIMARY KEY(frm, too));
CREATE TABLE IF NOT EXISTS crawl_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, requests INTEGER,
  new_trains INTEGER, priority TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path=None):
    con = sqlite3.connect(path or _DEFAULT, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    with _lock:
        con.execute("PRAGMA busy_timeout=30000")
        # WAL：读写不互斥。默认的 rollback journal 下，爬虫写库会把并发查询的读卡住，
        # 甚至两边都写时直接 "database is locked"（并发压测必现）。
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        except Exception:
            pass
        con.executescript(SCHEMA)
        # 老库迁移：crawl_od 可能没有 fresh 列
        cols = [r[1] for r in con.execute("PRAGMA table_info(crawl_od)").fetchall()]
        if "fresh" not in cols:
            con.execute("ALTER TABLE crawl_od ADD COLUMN fresh INTEGER DEFAULT 0")
            con.commit()
    return con


# 全国图定旅客列车数（2026年7月调图：12174列），覆盖率分母
EST_TOTAL = 12174


def upsert_train(con, train_no, code, stops, day=""):
    """stops: [(station_code, name, arrive, depart)] 按顺序。整车替换，保证新鲜。"""
    with _lock:
        con.execute("INSERT OR REPLACE INTO trains(train_no, code, updated) VALUES(?,?,?)",
                    (train_no, code, day))
        con.execute("DELETE FROM stops WHERE train_no=?", (train_no,))
        con.executemany(
            "INSERT INTO stops(train_no, seq, station, name, arrive, depart)"
            " VALUES(?,?,?,?,?,?)",
            [(train_no, i, s, n, a, d) for i, (s, n, a, d) in enumerate(stops)])
        con.commit()


def has_train(con, train_no):
    with _lock:
        r = con.execute("SELECT 1 FROM trains WHERE train_no=?", (train_no,)).fetchone()
    return r is not None


def downstream(con, station, limit=200):
    """从 station 出发 1 跳可达的站 -> {站码: 车次数}."""
    with _lock:
        rows = con.execute(
            "SELECT s2.station AS st, COUNT(*) AS c FROM stops s1"
            " JOIN stops s2 ON s1.train_no=s2.train_no AND s1.seq < s2.seq"
            " WHERE s1.station=? GROUP BY s2.station ORDER BY c DESC LIMIT ?",
            (station, limit)).fetchall()
    return {r["st"]: r["c"] for r in rows}


def upstream(con, station, limit=200):
    """能 1 跳到达 station 的站 -> {站码: 车次数}."""
    with _lock:
        rows = con.execute(
            "SELECT s1.station AS st, COUNT(*) AS c FROM stops s1"
            " JOIN stops s2 ON s1.train_no=s2.train_no AND s1.seq < s2.seq"
            " WHERE s2.station=? GROUP BY s1.station ORDER BY c DESC LIMIT ?",
            (station, limit)).fetchall()
    return {r["st"]: r["c"] for r in rows}


def common_middles(con, frm, too, limit=8):
    """A->M->B 的 M 候选，按“瓶颈频度”排序.

    瓶颈频度 = min(从A到M的车次数, 从M到B的车次数)：
    中转站真正的约束是两边**较少**的那一头。原来用 down+up 求和，
    会选出“A侧一堆车、B侧只有1趟”的假枢纽（唐山->费县案：济南西 down=17/up=2
    排第一，实际只有 2 趟车能接上）。求和只作为同分时的次序。
    """
    down = downstream(con, frm)
    if not down:
        return []
    up = upstream(con, too)
    if not up:
        return []
    scored = []
    for m in down:
        if m not in up or m in (frm, too):
            continue
        d, u = down[m], up[m]
        scored.append((min(d, u), d + u, m))
    scored.sort(reverse=True)
    return [m for _, _, m in scored[:limit]]


def mark_od(con, frm, too, day, fresh=0):
    with _lock:
        con.execute("INSERT OR REPLACE INTO crawl_od(frm, too, day, done, fresh)"
                    " VALUES(?,?,?,1,?)", (frm, too, day, fresh))
        con.commit()


def od_done(con, frm, too):
    with _lock:
        r = con.execute("SELECT 1 FROM crawl_od WHERE frm=? AND too=? AND done=1",
                        (frm, too)).fetchone()
    return r is not None


def od_table(con):
    """全量OD状态 {(frm, too): (done, fresh)}，供按产量排序。"""
    with _lock:
        rows = con.execute("SELECT frm, too, done, fresh FROM crawl_od").fetchall()
    return {(r["frm"], r["too"]): (r["done"], r["fresh"] or 0) for r in rows}


def reset_ods(con):
    with _lock:
        con.execute("DELETE FROM crawl_od")
        con.commit()


def log_run(con, requests, new_trains, priority=""):
    with _lock:
        import datetime as _dt
        con.execute("INSERT INTO crawl_runs(ts, requests, new_trains, priority) VALUES(?,?,?,?)",
                    (_dt.datetime.now().strftime("%m-%d %H:%M"), requests, new_trains,
                     ",".join(priority) if isinstance(priority, list) else str(priority or "")))
        con.commit()


def recent_runs(con, limit=10):
    with _lock:
        rows = con.execute(
            "SELECT ts, requests, new_trains, priority FROM crawl_runs"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [(r["ts"], r["requests"], r["new_trains"], r["priority"]) for r in rows]


def meta_get(con, k, default=None):
    with _lock:
        r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(con, k, v):
    with _lock:
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?,?)", (k, str(v)))
        con.commit()


def stats(con):
    with _lock:
        t = con.execute("SELECT COUNT(*) c FROM trains").fetchone()["c"]
        s = con.execute("SELECT COUNT(DISTINCT station) c FROM stops").fetchone()["c"]
        o = con.execute("SELECT COUNT(*) c FROM crawl_od WHERE done=1").fetchone()["c"]
    cov = round(t / EST_TOTAL * 100, 1) if EST_TOTAL else 0
    return {"trains": t, "stations": s, "ods": o, "coverage": cov, "total_est": EST_TOTAL}


def train_detail(con, code_or_no):
    """按车次号/车次代码查全程经停 -> [(站码, 站名, 到, 发)]，无则 []。"""
    with _lock:
        r = con.execute(
            "SELECT train_no FROM trains WHERE train_no=? OR code=?",
            (code_or_no, code_or_no.upper())).fetchone()
        if not r:
            return []
        rows = con.execute(
            "SELECT station, name, arrive, depart FROM stops WHERE train_no=? ORDER BY seq",
            (r["train_no"],)).fetchall()
    return [(x["station"], x["name"], x["arrive"], x["depart"]) for x in rows]


def trains_at(con, station_code, limit=50):
    """经过某站的车 -> [(车次, via描述)]，via描述如"甲08:00→乙12:00"过于重，只给车次+首末站。"""
    with _lock:
        rows = con.execute(
            "SELECT s.train_no AS tno, t.code AS code FROM stops s"
            " LEFT JOIN trains t ON t.train_no=s.train_no"
            " WHERE s.station=? GROUP BY s.train_no ORDER BY s.train_no LIMIT ?",
            (station_code, limit)).fetchall()
    return [(r["tno"], r["code"] or "") for r in rows]


def recent_trains(con, limit=30):
    """最近入库的车（rowid倒序即入库序）。"""
    with _lock:
        rows = con.execute(
            "SELECT train_no, code, updated FROM trains ORDER BY rowid DESC LIMIT ?",
            (limit,)).fetchall()
    return [(r["train_no"], r["code"], r["updated"]) for r in rows]

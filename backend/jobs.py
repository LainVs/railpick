"""后台查询任务 + 进度（轮询式，零新依赖，线程安全）."""
import threading
import time
import uuid

_jobs = {}
_lock = threading.Lock()
TTL = 900  # 任务保留 15 分钟


class Cancelled(Exception):
    """on_progress 回调发现任务被取消时抛出，中断搜索线程。"""


def _purge():
    now = time.time()
    for jid, j in list(_jobs.items()):
        if now - j["ts"] > TTL:
            _jobs.pop(jid, None)


def create():
    with _lock:
        _purge()
        jid = uuid.uuid4().hex[:12]
        _jobs[jid] = {"state": "running", "progress": 0, "stage": "排队中…",
                      "cancel": False, "result": None, "error": None,
                      "ts": time.time()}
        return jid


def update(jid, progress, stage):
    with _lock:
        j = _jobs.get(jid)
        if j and j["state"] == "running":
            j["progress"] = max(j["progress"], min(100, int(progress)))
            j["stage"] = stage
            j["ts"] = time.time()


def should_cancel(jid):
    with _lock:
        j = _jobs.get(jid)
        return bool(j and j["cancel"])


def cancel(jid):
    with _lock:
        j = _jobs.get(jid)
        if j and j["state"] == "running":
            j["cancel"] = True
            return True
        return False


def finish(jid, result=None, error=None, cancelled=False):
    with _lock:
        j = _jobs.get(jid)
        if not j:
            return
        if cancelled or j["cancel"]:
            j["state"] = "cancelled"
        elif error is not None:
            j["state"] = "error"
            j["error"] = str(error)
        else:
            j["state"] = "done"
            j["result"] = result
            j["progress"] = 100
            j["stage"] = "完成"
        j["ts"] = time.time()


def get(jid):
    with _lock:
        j = _jobs.get(jid)
        return dict(j) if j else None

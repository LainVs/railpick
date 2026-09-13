"""协作取消：桌面取消按钮 / Web 取消接口共用的停止信号。

约定：长时间函数接受 should_stop=None 参数（None 或无参可调用，
返回 True 表示调用方要求停止）。检查点里调 check()，该停就抛
SearchCancelled，逐层退出；调用方负责把线程池里还没开始的任务取消掉。
"""


class SearchCancelled(Exception):
    """调用方要求停掉本次搜索/核验，工作线程里抛出来逐层退出。"""


def stopped(should_stop):
    if should_stop is None:
        return False
    try:
        return bool(should_stop())
    except Exception:
        return False


def check(should_stop):
    if stopped(should_stop):
        raise SearchCancelled()

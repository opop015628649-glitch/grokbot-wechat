"""Rolling conversation history. Each webhook run is a fresh agent turn."""
import fcntl, os, time
from datetime import datetime
from zoneinfo import ZoneInfo
from grokbot_wechat import config

MAX_LINES, KEEP_LINES = 2000, 1000


def history_path():
    return os.path.join(config.data_dir(), "history.log")


def clean(text):
    return " / ".join(part.strip() for part in str(text or "").replace("\r\n", "\n").split("\n") if part.strip())


def stamp(ts=None):
    return datetime.fromtimestamp(ts or time.time(), ZoneInfo(config.history_tz())).strftime("%Y-%m-%d %H:%M")


def item_text(item):
    kind = item.get("type")
    if kind == 1:
        return clean(item.get("text"))
    saved = item.get("file")
    if kind == 2:
        return "[图片 %s]" % saved if saved else "[图片 下载失败]"
    if kind == 4:
        name = clean((item.get("meta") or {}).get("file_name") or "")
        return "[文件 %s %s]" % (name, saved) if saved else "[文件 %s 下载失败]" % name
    if kind == 3:
        if item.get("text"):
            return "[语音: %s]" % clean(item.get("text"))
        return "[语音 %s]" % saved if saved else "[语音 下载失败]"
    if kind == 5:
        return "[视频 %s]" % saved if saved else "[视频 下载失败]"
    return "[未知类型 %s]" % kind


def user_line(payloads):
    parts = [item_text(item) for payload in payloads for item in payload.get("items", [])]
    ts = (payloads[0].get("create_time_ms") or 0) / 1000 or None
    return "[%s] %s：%s" % (stamp(ts), config.owner_name(), " ".join(part for part in parts if part))


def grok_line(text, ts=None):
    return "[%s] %s：%s" % (stamp(ts), config.bot_name(), clean(text))


def append(line, path=None):
    path = path or history_path()
    with config.open_private(path, read_write=True, append=True) as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(line.replace("\n", " / ") + "\n")
        handle.flush()
        handle.seek(0)
        lines = handle.readlines()
        if len(lines) > MAX_LINES:
            handle.seek(0)
            handle.truncate()
            handle.writelines(lines[-KEEP_LINES:])


def tail(n=40, path=None):
    path = path or history_path()
    try:
        with open(path, encoding="utf-8") as handle:
            return "".join(handle.readlines()[-n:])
    except FileNotFoundError:
        return ""

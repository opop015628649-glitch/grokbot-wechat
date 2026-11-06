"""Opt-in outbox for WeChat sends and the keep-alive nudge.

Inbound-only mode (the default) returns before any send, queue write, or nudge state change.
"""
import json, os, time, logging
from datetime import datetime
from zoneinfo import ZoneInfo
from grokbot_wechat import config
from grokbot_wechat.ilink import load_state, save_state, send_text

NUDGE_AFTER_S = 18 * 3600
NUDGE_BEFORE_S = 23 * 3600
NUDGE_EARLIEST_S = 12 * 3600
NUDGE_MARGIN_S = 30 * 60
log = logging.getLogger("forward")


def outbox_path():
    return os.path.join(config.data_dir(), "outbox.json")


def _load():
    try:
        with open(outbox_path(), encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return []


def _save(queue):
    config.atomic_write_private(outbox_path(), json.dumps(queue, ensure_ascii=False, indent=1).encode("utf-8"))


def is_ok(resp):
    try:
        parsed = json.loads(resp) if str(resp).strip() else {}
        return not (isinstance(parsed, dict) and (parsed.get("ret") or parsed.get("errcode")))
    except (ValueError, AttributeError):
        return False


def enqueue(text, to=None):
    if not config.outbound_enabled():
        return
    queue = _load()
    queue.append({"text": text, "to": to, "queued_at": time.time()})
    _save(queue)


def send_or_queue(text, to=None, context_token=None):
    """Try to send. On failure, queue only when outbound mode is explicitly on."""
    if not config.outbound_enabled():
        return False, "outbound disabled"
    try:
        resp, _ = send_text(text, to=to, context_token=context_token)
    except Exception as exc:
        resp = "%s: %s" % (type(exc).__name__, exc)
    if is_ok(resp):
        return True, resp
    enqueue(text, to)
    return False, resp


def flush():
    """Send queued messages in order. Inbound-only mode leaves the file untouched."""
    if not config.outbound_enabled():
        return 0
    queue = _load()
    sent = 0
    while queue:
        item = queue[0]
        try:
            resp, _ = send_text(item["text"], to=item.get("to"), context_token="")
        except Exception as exc:
            resp = "%s: %s" % (type(exc).__name__, exc)
        if not is_ok(resp):
            log.warning("outbox flush stopped")
            break
        queue.pop(0)
        sent += 1
        _save(queue)
        time.sleep(1.5)
    if sent:
        log.info("outbox flushed %d message(s), %d left", sent, len(queue))
    return sent


def last_inbound_s(state=None):
    state = load_state() if state is None else state
    if state.get("last_inbound_ms"):
        return state["last_inbound_ms"] / 1000
    stamps = [m.get("create_time_ms") for m in state.get("raw_log", []) if m.get("message_type") == 1 and m.get("create_time_ms")]
    return max(stamps) / 1000 if stamps else None


def latest_ok_s(last):
    zone = ZoneInfo(config.quiet_tz())
    quiet = config.quiet_hours()
    moment = last + NUDGE_BEFORE_S
    for _ in range(48 * 4):
        if datetime.fromtimestamp(moment, zone).hour not in quiet:
            return moment
        moment -= 15 * 60
    return moment


def maybe_nudge(now=None):
    if not config.outbound_enabled():
        return False
    now = now or time.time()
    state = load_state()
    last = last_inbound_s(state)
    if not last:
        return False
    age = now - last
    if state.get("nudged_for") == last:
        return False
    if not (NUDGE_EARLIEST_S <= age < NUDGE_BEFORE_S):
        return False
    zone = ZoneInfo(config.quiet_tz())
    if datetime.fromtimestamp(now, zone).hour in config.quiet_hours():
        return False
    if age < NUDGE_AFTER_S and now < latest_ok_s(last) - NUDGE_MARGIN_S:
        return False
    try:
        resp, _ = send_text(config.nudge_text(), context_token="")
    except Exception as exc:
        resp = "%s: %s" % (type(exc).__name__, exc)
    state = load_state()
    state["nudged_for"] = last
    save_state(state)
    log.info("keepalive nudge sent=%s age=%.1fh", is_ok(resp), age / 3600)
    return is_ok(resp)

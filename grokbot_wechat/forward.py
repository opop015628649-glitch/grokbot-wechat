#!/usr/bin/env python3
"""Forward WeChat ClawBot user messages (message_type==1) to a Grok Bot webhook.

Default WECHAT_MODE is inbound: getupdates and the webhook still run, and this
process does not start the outbox thread or call sendmessage, sendtyping,
getuploadurl, nudge, or outbox flush.
"""
import json, os, socket, sys, threading, time, logging, urllib.request, urllib.error
from grokbot_wechat import config, media, history, outbox
from grokbot_wechat.ilink import post, load_state, save_state
from grokbot_wechat.redact import redact_text

log = logging.getLogger("forward")
MEDIA_TYPES = (2, 3, 4, 5)
KEEPALIVE_TOKENS = {"1", "１", "续", "续一下", "+1", "11", "111"}
RETRY_S = 60


def setup_logging():
    path = os.path.join(config.data_dir(), "forward.log")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        if os.fstat(fd).st_mode & 0o077:
            os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    logger = logging.getLogger("forward")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not any(getattr(handler, "_gwb_path", None) == path for handler in logger.handlers):
        handler = logging.FileHandler(path, encoding="utf-8")
        handler._gwb_path = path
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def post_webhook(payload, tries=5):
    url = config.webhook_url()
    key = config.webhook_key()
    if not url:
        log.error("WECHAT_WEBHOOK_URL not set; cannot forward")
        return None
    if not key:
        log.error("WECHAT_WEBHOOK_KEY unavailable; cannot forward")
        return None
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, data=data, method="POST",
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
            )
            with urllib.request.urlopen(req, timeout=20) as handle:
                body = handle.read().decode(errors="replace")
                log.info("webhook OK status=%s body=%s", handle.status, redact_text(body[:300]))
                return handle.status, body
        except urllib.error.HTTPError as exc:
            body = redact_text(exc.read().decode(errors="replace")[:300])
            log.warning("webhook HTTP %s (try %d): %s", exc.code, attempt + 1, body)
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                return exc.code, body
        except Exception as exc:
            log.warning("webhook error (try %d): %s", attempt + 1, redact_text(exc))
        time.sleep(min(2 ** attempt * 2, 60))
    log.error("webhook gave up after %d tries", tries)
    return None


def seen_path():
    return os.path.join(config.data_dir(), "forward_seen.json")


def load_seen():
    try:
        with open(seen_path(), encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return []


def fetch():
    state = load_state()
    try:
        raw = post("ilink/bot/getupdates", {"get_updates_buf": state.get("get_updates_buf", "")}, timeout=40)
    except (socket.timeout, TimeoutError):
        return True, []
    try:
        parsed = json.loads(raw)
    except ValueError:
        log.warning("getupdates response was not json")
        return False, []
    if parsed.get("ret", 0) != 0:
        log.warning("getupdates ret=%s", parsed.get("ret"))
        return False, []
    state = load_state()
    if parsed.get("get_updates_buf"):
        state["get_updates_buf"] = parsed["get_updates_buf"]
    msgs = parsed.get("msgs") or []
    for msg in msgs:
        if msg.get("message_type") == 1 and msg.get("from_user_id"):
            state["last_from_user_id"] = msg["from_user_id"]
            if msg.get("context_token"):
                state.setdefault("context_tokens", {})[msg["from_user_id"]] = msg["context_token"]
            msg["_prev_inbound_ms"] = state.get("last_inbound_ms")
            state["last_inbound_ms"] = max(
                state.get("last_inbound_ms") or 0,
                msg.get("create_time_ms") or int(time.time() * 1000),
            )
    if msgs:
        state["raw_log"] = (state.get("raw_log", []) + msgs)[-20:]
    save_state(state)
    return True, msgs


def build_payload(msg, attach_dir=None, fetch_media=True):
    items = msg.get("item_list") or []
    payload = {
        "source": "wechat-clawbot",
        "from_user_id": msg.get("from_user_id"),
        "texts": [text for text in (media.summarize(item).get("text") for item in items) if text],
        "item_types": [item.get("type") for item in items],
        "items": [media.summarize(item) for item in items],
        "create_time_ms": msg.get("create_time_ms"),
        "message_id": msg.get("message_id"),
    }
    media_items = [item for item in items if item.get("type") in MEDIA_TYPES]
    if media_items:
        payload["has_media"] = True
        files, errors, raw = [], [], []
        ts = (msg.get("create_time_ms") or 0) / 1000 or None
        folder = attach_dir or config.attach_dir()
        for idx, item in enumerate(items):
            if item.get("type") not in MEDIA_TYPES or not fetch_media:
                continue
            try:
                path = media.fetch_item(item, folder, ts)
                files.append(path)
                payload["items"][idx]["file"] = path
            except Exception as exc:
                err = "%s: %s: %s" % (media.TYPE_NAMES.get(item.get("type")), type(exc).__name__, redact_text(exc))
                errors.append(err)
                raw.append(item)
                payload["items"][idx]["media_error"] = err
        payload["files"] = files
        if errors:
            payload["media_error"] = "; ".join(errors)
            payload["raw_items"] = raw
            log.warning("media error message_id=%s", msg.get("message_id"))
    return payload


def pending_path():
    return os.path.join(config.data_dir(), "pending.json")


class Batcher:
    """Thread-safe, disk-persisted pending buffer, flushed by a background thread."""

    def __init__(self, path=None, quiet=None, cap=None, poster=None, clock=time.time, history_path=None):
        self.path = path or pending_path()
        self.quiet = config.batch_quiet_s() if quiet is None else quiet
        self.cap = config.batch_cap_s() if cap is None else cap
        self.clock = clock
        self.history_path = history_path or history.history_path()
        self.poster = poster or post_webhook
        self.lock = threading.Lock()
        self.next_try = 0
        stored = {}
        try:
            with open(self.path, encoding="utf-8") as handle:
                stored = json.load(handle)
        except Exception:
            pass
        self.msgs = stored.get("messages", [])
        self.oldest = stored.get("oldest")
        self.last = self.clock() if self.msgs else None
        if self.msgs:
            self.oldest = self.last
            log.info("restored %d pending message(s) from disk", len(self.msgs))

    def _save(self):
        config.atomic_write_private(
            self.path,
            json.dumps({"messages": self.msgs, "oldest": self.oldest, "last": self.last}, ensure_ascii=False).encode("utf-8"),
        )

    def add(self, payload):
        with self.lock:
            now = self.clock()
            if not self.msgs:
                self.oldest = now
            self.msgs.append(payload)
            self.last = now
            self._save()
            log.info("buffered message_id=%s (pending=%d)", payload.get("message_id"), len(self.msgs))

    def due(self):
        now = self.clock()
        return bool(self.msgs) and now >= self.next_try and (
            now - self.last >= self.quiet or now - self.oldest >= self.cap)

    def maybe_flush(self):
        with self.lock:
            if not self.due():
                return False
            batch = list(self.msgs)
            reason = "quiet" if self.clock() - self.last >= self.quiet else "cap"
        trigger_ms = max([item.get("create_time_ms") or 0 for item in batch] or [0])
        if config.outbound_enabled():
            reply_rule = (
                "Pass --reply-to-ms %d to every send.py call. Exit code 4 (SUPERSEDED) means %s "
                "sent something newer: stop immediately, send nothing, file nothing; the newer run "
                "handles it." % (trigger_ms, config.owner_name())
            )
        else:
            reply_rule = (
                "Inbound-only mode is on. Do not call send.py. Do not send, queue, or nudge any "
                "WeChat message. trigger_ms=%d." % trigger_ms
            )
        body = {
            "source": "wechat-clawbot",
            "batch": True,
            "messages": batch,
            "count": len(batch),
            "trigger_ms": trigger_ms,
            "reply_rule": reply_rule,
            "history": history.tail(40, self.history_path),
        }
        if config.outbound_enabled():
            hist_lines = [line for line in body["history"].splitlines() if line.strip()]
            after_nudge = bool(hist_lines) and config.nudge_text() in hist_lines[-1]
            first = batch[0]
            gap_h = 0
            if first.get("prev_inbound_ms"):
                gap_h = ((first.get("create_time_ms") or 0) - (first.get("prev_inbound_ms") or 0)) / 3.6e6
            bare_ping = all("".join(item.get("texts") or []).strip() in KEEPALIVE_TOKENS for item in batch)
            if not after_nudge and bare_ping and gap_h >= 12:
                body["context_note"] = (
                    "%s sent a bare '%s' after %.0fh of silence. That's him renewing the ~24h WeChat push "
                    "window (the keepalive nudge may have been skipped or missed). Reply with a very short "
                    "natural ack like '续上了', and if any queued pushes went out, say so briefly. Don't ask "
                    "what it means." % (config.owner_name(), "".join(first.get("texts") or []).strip(), gap_h)
                )
            if after_nudge:
                body["context_note"] = (
                    "Your last WeChat message to %s was the keepalive nudge (see the end of history), "
                    "so this batch is his reply to it. A bare '1' or similar just renews the window: reply "
                    "with a very short natural ack (e.g. '续上了'), don't ask what's up. If he says more, "
                    "answer that normally with the nudge as context." % config.owner_name()
                )
        log.info("flushing %d message(s) (%s) ids=%s", len(batch), reason, [item.get("message_id") for item in batch])
        result = self.poster(body)
        ok = bool(result) and 200 <= result[0] < 300
        with self.lock:
            if ok:
                try:
                    history.append(history.user_line(batch), self.history_path)
                except Exception as exc:
                    log.warning("history append failed: %s", redact_text(exc))
                self.msgs = self.msgs[len(batch):]
                self.oldest = self.clock() if self.msgs else None
                self.next_try = 0
            else:
                self.next_try = self.clock() + RETRY_S
                log.error("flush failed; keeping %d pending, retry in %ds", len(self.msgs), RETRY_S)
            self._save()
        return ok

    def run(self, interval=1.0, stop_event=None):
        while stop_event is None or not stop_event.is_set():
            try:
                self.maybe_flush()
            except Exception as exc:
                log.warning("flusher error: %s", redact_text(exc))
            if stop_event is None:
                time.sleep(interval)
            else:
                stop_event.wait(interval)


def handle(msgs, batcher):
    seen = load_seen()
    for msg in msgs:
        if msg.get("message_type") != 1:
            continue
        mid = msg.get("message_id")
        if mid is not None and mid in seen:
            continue
        payload = build_payload(msg)
        payload["prev_inbound_ms"] = msg.get("_prev_inbound_ms")
        log.info(
            "received message_id=%s types=%s files=%s",
            mid, payload["item_types"], payload.get("files"),
        )
        batcher.add(payload)
        if mid is not None:
            seen = (seen + [mid])[-500:]
            config.atomic_write_private(seen_path(), json.dumps(seen).encode("utf-8"))


def outbox_loop(stop_event=None):
    seen_last = None
    while stop_event is None or not stop_event.is_set():
        if not config.outbound_enabled():
            return
        try:
            last = outbox.last_inbound_s()
            if last and last != seen_last:
                seen_last = last
                time.sleep(3)
                outbox.flush()
            outbox.maybe_nudge()
        except Exception as exc:
            log.warning("outbox loop error: %s", redact_text(exc))
        if stop_event is None:
            time.sleep(15)
        else:
            stop_event.wait(15)


def _notify(started):
    try:
        raw = outbox_notify(started)
    except Exception as exc:
        log.warning("lifecycle notify failed: %s", redact_text(exc))
        return
    try:
        parsed = json.loads(raw) if raw else {}
    except ValueError:
        log.warning("lifecycle notify returned non-json")
        return
    if isinstance(parsed, dict) and parsed.get("ret"):
        log.warning("lifecycle notify ret=%s", parsed.get("ret"))


def outbox_notify(started):
    from grokbot_wechat.ilink import notify_lifecycle
    return notify_lifecycle(started)


def start_workers(batcher, stop_event=None):
    threads = []
    flusher = threading.Thread(target=batcher.run, kwargs={"stop_event": stop_event}, daemon=True, name="flusher")
    flusher.start()
    threads.append(flusher)
    if config.outbound_enabled():
        worker = threading.Thread(target=outbox_loop, kwargs={"stop_event": stop_event}, daemon=True, name="outbox")
        worker.start()
        threads.append(worker)
    else:
        log.info("inbound-only mode: WeChat send, nudge, typing, and outbox flush are off")
    return threads


def run_cycle(batcher, fails=0):
    heartbeat = os.path.join(config.data_dir(), "forward.heartbeat")
    try:
        config.atomic_write_private(heartbeat, str(int(time.time())).encode("utf-8"))
    except OSError:
        pass
    try:
        ok, msgs = fetch()
        if ok:
            if msgs:
                handle(msgs, batcher)
            return 0
        time.sleep(60 if fails >= 3 else 10)
        return fails + 1
    except Exception as exc:
        log.warning("poll error: %s", redact_text(exc))
        time.sleep(10)
        return fails + 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print("grokbot-wechat-forward — 默认只入站。收取微信消息并 POST 到 WECHAT_WEBHOOK_URL。\n"
              "不会发送微信消息，除非进程环境里 WECHAT_MODE=bidirectional。\n"
              "--selftest 只向 Webhook 发一条测试，不连微信。")
        return 0
    setup_logging()
    if "--selftest" in argv:
        print(post_webhook({"source": "wechat-clawbot", "test": True, "texts": ["(forwarder self-test, ignore)"]}, tries=1))
        return 0
    _notify(True)
    batcher = Batcher()
    log.info(
        "forwarder start pid=%s inbound_only=%s quiet=%s cap=%s url_set=%s key_present=%s",
        os.getpid(), not config.outbound_enabled(), batcher.quiet, batcher.cap,
        bool(config.webhook_url()), bool(config.webhook_key()),
    )
    start_workers(batcher)
    fails = 0
    while True:
        fails = run_cycle(batcher, fails)


def cli():
    sys.exit(main())


if __name__ == "__main__":
    cli()

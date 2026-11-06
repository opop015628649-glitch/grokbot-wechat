#!/usr/bin/env python3
"""Debug tool: fetch updates once. Do not run this while forward is running.

context_token is stored in state.json and is not printed.
"""
import json, sys, socket
from grokbot_wechat.ilink import post, load_state, save_state


def once():
    state = load_state()
    try:
        raw = post("ilink/bot/getupdates", {"get_updates_buf": state.get("get_updates_buf", "")}, timeout=40)
    except (socket.timeout, TimeoutError):
        print("(timeout, no messages)")
        return []
    parsed = json.loads(raw)
    if parsed.get("ret", 0) != 0:
        print("ERROR: getupdates ret=%s" % parsed.get("ret"))
        return []
    if parsed.get("get_updates_buf"):
        state["get_updates_buf"] = parsed["get_updates_buf"]
    msgs = parsed.get("msgs") or []
    for msg in msgs:
        texts = [(item.get("text_item") or item.get("voice_item") or {}).get("text") for item in msg.get("item_list") or []]
        print(json.dumps({
            "from_user_id": msg.get("from_user_id"),
            "message_type": msg.get("message_type"),
            "texts": texts,
            "has_context_token": bool(msg.get("context_token")),
            "create_time_ms": msg.get("create_time_ms"),
        }, ensure_ascii=False))
        if msg.get("message_type") == 1 and msg.get("from_user_id"):
            state["last_from_user_id"] = msg["from_user_id"]
            if msg.get("context_token"):
                state.setdefault("context_tokens", {})[msg["from_user_id"]] = msg["context_token"]
    state.setdefault("raw_log", [])
    state["raw_log"] = (state["raw_log"] + msgs)[-20:]
    save_state(state)
    if not msgs:
        print("(no messages)")
    return msgs


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv:
        print("grokbot-wechat poll — one getupdates. Do not run beside the forwarder.")
        return 0
    if "--loop" in argv:
        while True:
            once()
        return 0
    once()
    return 0


def cli():
    sys.exit(main())


if __name__ == "__main__":
    cli()

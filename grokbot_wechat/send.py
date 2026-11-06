#!/usr/bin/env python3
"""Send a text to the bound WeChat user. Disabled unless WECHAT_MODE=bidirectional."""
import argparse, sys
from grokbot_wechat import config, outbox


def main(argv=None):
    parser = argparse.ArgumentParser(description="Send a WeChat message. Refuses in the default inbound-only mode.")
    parser.add_argument("text")
    parser.add_argument("--to")
    parser.add_argument("--no-context", action="store_true")
    parser.add_argument("--app", action="store_true", help="prefix APP_PREFIX for sends not triggered by the WeChat webhook")
    parser.add_argument("--reply-to-ms", type=int)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if not config.outbound_enabled():
        print("OUTBOUND_DISABLED: WECHAT_MODE is inbound. Refusing to send a WeChat message.")
        return 5
    if args.reply_to_ms is not None:
        last = outbox.last_inbound_s()
        if last and last * 1000 > args.reply_to_ms + 500:
            print("SUPERSEDED: %s sent a newer message; not sending. Stop this run without filing anything." % config.owner_name())
            return 4
    text = (config.app_prefix() + args.text) if args.app else args.text
    sent, resp = outbox.send_or_queue(text, to=args.to, context_token="" if args.no_context else None)
    print("sent:" if sent else "QUEUED (will auto-send when the user next writes):", resp)
    return 0 if sent else 3


def cli():
    sys.exit(main())


if __name__ == "__main__":
    cli()

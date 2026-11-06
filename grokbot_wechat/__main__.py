"""python -m grokbot_wechat prints the installed commands and does not touch the network."""

HELP = """\
grokbot-wechat 已安装。默认只入站，不会给微信发消息。

  grokbot-wechat-bind      扫码绑定。等待期间数据目录里会出现 qr.png
  grokbot-wechat-forward   收取微信消息并交给 Webhook
  grokbot-wechat-send      只有 WECHAT_MODE=bidirectional 时才会发送

加上 --help 可以看单个命令的说明，不会联网。
"""


def main():
    print(HELP, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

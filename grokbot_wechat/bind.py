#!/usr/bin/env python3
"""Scan-to-bind a WeChat ClawBot account.

Writes qr.png at mode 0600 inside the 0700 data directory and waits until the
phone confirms. The login URL and token stay in memory and inside that image.
They are not printed and are not written to a separate link file.
"""
import io, json, os, sys, time, datetime
import qrcode
from grokbot_wechat import config, ilink
from grokbot_wechat.redact import redact_text

HELP = """\
grokbot-wechat-bind — 只做微信扫码绑定（默认不会发消息）

不带参数运行时，程序会连接腾讯公开的 iLink 登录接口，在数据目录写出 qr.png，
然后一直等待手机确认。--help 不会联网，也不会生成二维码。

二维码文件（仅在等待扫码期间存在）：
  $BRIDGE_DATA_DIR/qr.png
未设置 BRIDGE_DATA_DIR 时，目录是 ~/.grokbot-wechat

终端不会打印扫码链接或 token。不要把终端输出或 qr.png 发给别人。
手机上如果出现数字，把数字单独写入同一目录的 verify_code.txt。
确认成功或失败退出后，qr.png 会被删除。凭证写在 credentials.json。
"""


def log(*parts):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe = [redact_text(part) for part in parts]
    print(stamp, *safe, flush=True)


def qr_path():
    return os.path.join(config.data_dir(), "qr.png")


def render_qr_png(content):
    code = qrcode.QRCode(box_size=10, border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(content)
    code.make(fit=True)
    buf = io.BytesIO()
    code.make_image().save(buf, format="PNG")
    return buf.getvalue()


def write_qr_png(content):
    config.atomic_write_private(qr_path(), render_qr_png(content))


def clear_login_files():
    root = config.data_dir()
    for name in ("qr.png", "qr.tmp.png", "qr_url.txt"):
        try:
            os.unlink(os.path.join(root, name))
        except FileNotFoundError:
            pass


def save_credentials(status):
    saved = {}
    for key in ("status", "bot_token", "ilink_bot_id", "ilink_user_id", "baseurl", "ret"):
        if key in status and status[key] is not None:
            saved[key] = status[key]
    if not saved.get("bot_token") or not saved.get("ilink_bot_id"):
        raise ilink.LoginTransportError("confirmed login missing credential fields")
    path = os.path.join(config.data_dir(), "credentials.json")
    config.atomic_write_private(path, json.dumps(saved, ensure_ascii=False, indent=2).encode("utf-8"))
    return path


def _safe_status(status):
    if isinstance(status, str) and status.isidentifier() and len(status) <= 40:
        return status
    return "unknown"


def read_verify_code():
    path = os.path.join(config.data_dir(), "verify_code.txt")
    try:
        with open(path, encoding="utf-8") as handle:
            code = handle.read().strip()
    except FileNotFoundError:
        return None
    if not code:
        return None
    try:
        os.unlink(path)
    except OSError:
        pass
    return code


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(HELP, end="")
        return 0
    if argv:
        print("不认识的参数。只运行 grokbot-wechat-bind，或加上 --help。", flush=True)
        return 2
    config.data_dir()
    clear_login_files()
    base = ilink.LOGIN_BASE
    qr = None
    refreshes = 0
    verify = None
    last = None
    limit = int(os.environ.get("MAX_REFRESH", "10"))
    try:
        while True:
            if qr is None:
                try:
                    qr, content = ilink.fetch_login_qr()
                except Exception as exc:
                    log("获取二维码失败", exc)
                    return 1
                write_qr_png(content)
                log("二维码已写入", qr_path(), "请在手机确认前使用这个文件，终端里没有扫码链接")
            try:
                status = ilink.poll_login(base, qr, verify)
            except Exception as exc:
                log("查询扫码状态失败", exc)
                time.sleep(1)
                continue
            name = _safe_status(status.get("status"))
            if name != last:
                log("status:", name)
                last = name
            if name == "confirmed":
                try:
                    path = save_credentials(status)
                except Exception as exc:
                    log("保存凭证失败", exc)
                    return 1
                log("绑定成功，凭证已保存", path)
                return 0
            if name == "binded_redirect":
                log("这个微信已经绑定过，没有写入新凭证")
                return 0
            if name == "scaned_but_redirect":
                redirected = ilink.login_base_from_host(status.get("redirect_host"))
                if redirected:
                    base = redirected
                    log("扫码状态改到新的登录主机继续等待")
                else:
                    log("扫码状态要求切换主机，但主机名不可用，继续使用当前主机")
            if name == "scaned":
                verify = None
            if name == "need_verifycode":
                path = os.path.join(config.data_dir(), "verify_code.txt")
                log("手机上出现数字时，把数字写进", path)
                code = None
                while not code:
                    time.sleep(2)
                    code = read_verify_code()
                verify = code
                continue
            if name in ("expired", "verify_code_blocked"):
                refreshes += 1
                if refreshes > limit:
                    log("二维码刷新次数用完，已停止")
                    return 1
                qr = None
                base = ilink.LOGIN_BASE
                verify = None
            time.sleep(1)
    finally:
        clear_login_files()


def cli():
    sys.exit(main())


if __name__ == "__main__":
    cli()

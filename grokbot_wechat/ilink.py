"""WeChat iLink client following the published @tencent-weixin/openclaw-weixin 2.4.10 protocol.

Public sources, read for this implementation (not a private protocol):
- package.json version 2.4.10 and ilink_appid "bot"
  https://github.com/Tencent/openclaw-weixin/blob/main/package.json
- client version encoder buildClientVersion (0x00MMNNPP) in src/api/api.ts
- header split in docs/protocol.md: QR POST uses JSON POST headers including
  AuthorizationType and X-WECHAT-UIN but omits Authorization and base_info;
  QR status GET sends only iLink-App-Id, iLink-App-ClientVersion, and optional SKRouteTag.

Live acceptance by ilinkai.weixin.qq.com is not proven here. This module does not call it
unless a caller asks, and the inbound-only default refuses user-visible send endpoints.
"""
import base64, json, os, random, urllib.request
from grokbot_wechat import config
from grokbot_wechat.redact import redact_text

LOGIN_BASE = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.10"
ILINK_APP_ID = "bot"
BOT_AGENT = "OpenClaw"
OUTBOUND_ENDPOINTS = {
    "ilink/bot/sendmessage",
    "ilink/bot/sendtyping",
    "ilink/bot/getuploadurl",
}


class OutboundDisabled(RuntimeError):
    """Raised before any network call when a user-visible WeChat send is attempted."""


class LoginTransportError(RuntimeError):
    """Transport or parse failure whose message has already been redacted."""


def client_version(version=None):
    """Same encoder as openclaw-weixin src/api/api.ts buildClientVersion."""
    version = CHANNEL_VERSION if version is None else version
    parts = []
    for piece in str(version).split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[:3]
    return ((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF)


def _uin_header():
    # Documented: random uint32, decimal string, then base64. Not an account id.
    return base64.b64encode(str(random.getrandbits(32)).encode()).decode()


def common_headers():
    return {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(client_version()),
    }


def post_headers(token=None):
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _uin_header(),
    }
    headers.update(common_headers())
    if token and str(token).strip():
        headers["Authorization"] = "Bearer " + str(token).strip()
    return headers


def _endpoint_name(endpoint):
    return endpoint.split("?", 1)[0].lstrip("/")


def cred_path():
    return os.path.join(config.data_dir(), "credentials.json")


def state_path():
    return os.path.join(config.data_dir(), "state.json")


def creds():
    with open(cred_path(), encoding="utf-8") as f:
        return json.load(f)


def load_state():
    path = state_path()
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    config.atomic_write_private(
        state_path(),
        json.dumps(state, indent=2, ensure_ascii=False).encode("utf-8"),
    )


def post(endpoint, body, timeout=15):
    name = _endpoint_name(endpoint)
    if name in OUTBOUND_ENDPOINTS and not config.outbound_enabled():
        raise OutboundDisabled(name)
    account = creds()
    headers = post_headers(account.get("bot_token"))
    payload = dict(body)
    payload["base_info"] = {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}
    base = (account.get("baseurl") or LOGIN_BASE).rstrip("/")
    url = base + "/" + endpoint.lstrip("/")
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return f.read().decode()


def request_json(method, url, headers, data=None, timeout=40):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:
            raw = f.read()
    except Exception as e:
        raise LoginTransportError(redact_text(e)) from None
    try:
        parsed = json.loads(raw.decode())
    except ValueError:
        raise LoginTransportError("response was not valid json") from None
    if not isinstance(parsed, dict):
        raise LoginTransportError("response was not a json object")
    return parsed


def fetch_login_qr(timeout=40):
    """POST the documented login-QR call. Returns (qrcode, qrcode_img_content) and does not log them."""
    url = LOGIN_BASE + "/ilink/bot/get_bot_qrcode?bot_type=3"
    body = json.dumps({"local_token_list": []}).encode("utf-8")
    payload = request_json("POST", url, post_headers(token=None), data=body, timeout=timeout)
    qr = payload.get("qrcode")
    content = payload.get("qrcode_img_content")
    if not isinstance(qr, str) or not qr or not isinstance(content, str) or not content:
        raise LoginTransportError("login qr response missing fields")
    return qr, content


def login_base_from_host(host):
    if not isinstance(host, str):
        return None
    if not host or len(host) > 253:
        return None
    if host.startswith(".") or host.endswith(".") or ".." in host:
        return None
    for ch in host:
        if not (ch.isalnum() or ch in ".-"):
            return None
    return "https://" + host


def poll_login(base, qrcode, verify_code=None, timeout=40):
    from urllib.parse import quote
    url = base.rstrip("/") + "/ilink/bot/get_qrcode_status?qrcode=" + quote(qrcode, safe="")
    if verify_code:
        url += "&verify_code=" + quote(str(verify_code), safe="")
    return request_json("GET", url, common_headers(), data=None, timeout=timeout)


def notify_lifecycle(started, timeout=10):
    endpoint = "ilink/bot/msg/notifystart" if started else "ilink/bot/msg/notifystop"
    return post(endpoint, {}, timeout=timeout)


def send_text(text, to=None, context_token=None):
    """Send one text. Refuses, before any socket, while inbound-only mode is on."""
    if not config.outbound_enabled():
        raise OutboundDisabled("ilink/bot/sendmessage")
    state = load_state()
    account = creds()
    to = to or state.get("last_from_user_id") or account["ilink_user_id"]
    if context_token is None:
        context_token = state.get("context_tokens", {}).get(to)
    import secrets
    msg = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": "openclaw-weixin-" + secrets.token_hex(8),
        "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": text}}],
    }
    if context_token:
        msg["context_token"] = context_token
    resp = post("ilink/bot/sendmessage", {"msg": msg})
    try:
        parsed = json.loads(resp) if resp.strip() else {}
        ok = not (isinstance(parsed, dict) and (parsed.get("ret") or parsed.get("errcode")))
    except ValueError:
        ok = False
    if ok:
        try:
            from grokbot_wechat import history
            history.append(history.grok_line(text))
        except Exception:
            pass
    return resp, bool(context_token)

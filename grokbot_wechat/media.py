"""WeChat iLink media: summarise items, download from the documented CDN and AES-128-ECB decrypt."""
import base64, binascii, os, time, urllib.parse, urllib.request
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from grokbot_wechat import config

CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
TYPE_NAMES = {1: "text", 2: "image", 3: "voice", 4: "file", 5: "video"}
ITEM_KEYS = {1: "text_item", 2: "image_item", 3: "voice_item", 4: "file_item", 5: "video_item"}


def _strip_long(value, maxlen=120):
    if isinstance(value, dict):
        return {k: _strip_long(v, maxlen) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_long(v, maxlen) for v in value]
    if isinstance(value, str) and len(value) > maxlen:
        return value[:maxlen] + "...(%d chars)" % len(value)
    return value


def summarize(item):
    kind = item.get("type")
    name = TYPE_NAMES.get(kind, "unknown")
    summary = {"type": kind, "type_name": name}
    if kind == 1:
        summary["text"] = (item.get("text_item") or {}).get("text")
    else:
        sub = item.get(ITEM_KEYS.get(kind, ""), {}) or {}
        meta = {k: v for k, v in sub.items() if k not in ("media", "thumb_media", "aeskey")}
        if kind == 3 and sub.get("text"):
            summary["text"] = sub["text"]
        summary["meta"] = _strip_long(meta)
    return summary


def parse_key(item_sub):
    """Return a 16-byte AES key. aeskey is hex; media.aes_key is base64 of raw 16 bytes or of the hex string."""
    hx = item_sub.get("aeskey")
    if hx:
        return binascii.unhexlify(hx)
    b64 = (item_sub.get("media") or {}).get("aes_key")
    if not b64:
        raise ValueError("no aes key in item")
    raw = base64.b64decode(b64)
    if len(raw) == 16:
        return raw
    if len(raw) == 32:
        return binascii.unhexlify(raw.decode())
    raise ValueError("unexpected aes_key length %d" % len(raw))


def media_url(media):
    if media.get("full_url"):
        return media["full_url"]
    query = media.get("encrypt_query_param")
    if not query:
        raise ValueError("no media url / encrypt_query_param")
    return CDN_BASE + "/download?encrypted_query_param=" + urllib.parse.quote(query, safe="")


def download(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=timeout) as handle:
        return handle.read()


def decrypt(data, key):
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    out = dec.update(data) + dec.finalize()
    pad = out[-1] if out else 0
    if 1 <= pad <= 16 and out.endswith(bytes([pad]) * pad):
        out = out[:-pad]
    return out


MAGIC = [
    (b"\xff\xd8\xff", "jpg"), (b"\x89PNG", "png"), (b"GIF8", "gif"), (b"RIFF", "webp"),
    (b"%PDF", "pdf"), (b"PK\x03\x04", "zip"), (b"#!AMR", "amr"), (b"\x02#!SILK", "silk"),
    (b"#!SILK", "silk"),
]


def sniff_ext(data, default="bin"):
    for magic, ext in MAGIC:
        if data.startswith(magic):
            return ext
    if data[4:8] == b"ftyp":
        return "mp4"
    return default


def fetch_item(item, outdir, ts=None):
    kind = item.get("type")
    name = TYPE_NAMES.get(kind, "media")
    sub = item.get(ITEM_KEYS.get(kind, ""), {}) or {}
    media = sub.get("media") or {}
    plain = decrypt(download(media_url(media)), parse_key(sub))
    ext = None
    if kind == 4 and sub.get("file_name") and "." in sub["file_name"]:
        ext = sub["file_name"].rsplit(".", 1)[1][:8]
    ext = ext or sniff_ext(plain)
    outdir = config.ensure_private_dir(outdir)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts or time.time()))
    path = os.path.join(outdir, "%s-%s.%s" % (stamp, name, ext))
    n = 1
    while os.path.exists(path):
        path = os.path.join(outdir, "%s-%s-%d.%s" % (stamp, name, n, ext))
        n += 1
    config.atomic_write_private(path, plain)
    return os.path.abspath(path)

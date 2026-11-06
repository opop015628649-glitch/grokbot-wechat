"""Central configuration. Secrets come from the environment or a local file, never from this repo."""
import json, os, logging

CODE_DIR = os.path.dirname(os.path.abspath(__file__))
_MODE_WARNED = False


def _load_dotenv(path):
    """KEY=VALUE loader. Never overrides variables already in the environment."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                    v = v[1:-1]
                os.environ.setdefault(k, v)
    except (FileNotFoundError, OSError):
        pass


def load_dotenv_files():
    explicit = os.environ.get("WECHAT_ENV_FILE", "").strip()
    if explicit:
        _load_dotenv(os.path.expanduser(explicit))
    data = os.environ.get("BRIDGE_DATA_DIR", "").strip()
    if data:
        _load_dotenv(os.path.join(os.path.abspath(os.path.expanduser(data)), ".env"))
    else:
        _load_dotenv(os.path.expanduser("~/.grokbot-wechat/.env"))
    _load_dotenv(os.path.join(os.getcwd(), ".env"))


load_dotenv_files()


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def ensure_private_dir(path):
    """Create a directory at 0700. A new directory is never created world-accessible.

    An already existing directory that other users can enter is tightened to 0700
    before secrets are written. That is a leftover, not the creation path.
    """
    path = os.path.abspath(path)
    if os.path.isdir(path):
        if os.stat(path).st_mode & 0o077:
            os.chmod(path, 0o700)
        return path
    missing = []
    cur = path
    while not os.path.isdir(cur):
        missing.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            if not os.path.isdir(directory):
                raise
    return path


def data_dir():
    raw = _env("BRIDGE_DATA_DIR")
    default = os.path.join(os.path.expanduser("~"), ".grokbot-wechat")
    return ensure_private_dir(os.path.abspath(os.path.expanduser(raw or default)))


def attach_dir():
    raw = _env("WECHAT_ATTACH_DIR")
    path = os.path.abspath(os.path.expanduser(raw or os.path.join(data_dir(), "attachments")))
    return ensure_private_dir(path)


def outbound_enabled():
    """WeChat sends, nudges, typing, and outbox flushes are off unless WECHAT_MODE=bidirectional."""
    global _MODE_WARNED
    raw = os.environ.get("WECHAT_MODE")
    if raw is None or raw.strip() == "" or raw.strip().lower() == "inbound":
        return False
    if raw.strip().lower() == "bidirectional":
        return True
    if not _MODE_WARNED:
        _MODE_WARNED = True
        logging.getLogger("forward").warning("unrecognized WECHAT_MODE; staying inbound-only")
    return False


def webhook_url():
    return _env("WECHAT_WEBHOOK_URL")


def secrets_file():
    return _env("WECHAT_SECRETS_FILE")


def webhook_key():
    """Bearer key. WECHAT_WEBHOOK_KEY wins; else WECHAT_SECRETS_FILE JSON, top level or one level down."""
    k = _env("WECHAT_WEBHOOK_KEY")
    path = secrets_file()
    if k or not path:
        return k
    try:
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return ""
    if isinstance(d, dict) and d.get("WECHAT_WEBHOOK_KEY"):
        return str(d["WECHAT_WEBHOOK_KEY"]).strip()
    for sect in (d.values() if isinstance(d, dict) else []):
        if isinstance(sect, dict) and sect.get("WECHAT_WEBHOOK_KEY"):
            return str(sect["WECHAT_WEBHOOK_KEY"]).strip()
    return ""


def batch_quiet_s():
    return float(_env("BATCH_QUIET_S", "3"))


def batch_cap_s():
    return float(_env("BATCH_CAP_S", "60"))


def owner_name():
    return _env("OWNER_NAME", "User")


def bot_name():
    return _env("BOT_NAME", "grok")


def app_prefix():
    return _env("APP_PREFIX", "[grok bot] ")


def nudge_text():
    return _env("NUDGE_TEXT", app_prefix() + "离你上次发消息快一天了，回个 1 续一下，免得之后的提醒发不过来。")


def quiet_tz():
    return _env("QUIET_TZ", "Asia/Shanghai")


def quiet_start():
    return int(_env("QUIET_START", "0"))


def quiet_end():
    return int(_env("QUIET_END", "8"))


def history_tz():
    return _env("HISTORY_TZ", "Asia/Shanghai")


def quiet_hours():
    start, end = quiet_start(), quiet_end()
    if start <= end:
        return set(range(start, end))
    return set(range(start, 24)) | set(range(0, end))


def _tighten_fd(fd):
    if os.fstat(fd).st_mode & 0o077:
        os.fchmod(fd, 0o600)


def open_private(path, binary=False, append=False, read_write=False):
    """Open a file created at 0600. umask cannot widen that mode."""
    ensure_private_dir(os.path.dirname(os.path.abspath(path)))
    flags = os.O_CREAT | (os.O_RDWR if read_write else os.O_WRONLY)
    if append or read_write:
        if append:
            flags |= os.O_APPEND
    else:
        flags |= os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    _tighten_fd(fd)
    if binary:
        mode = "ab" if append else "wb"
        return os.fdopen(fd, mode)
    if read_write:
        mode = "a+"
    elif append:
        mode = "a"
    else:
        mode = "w"
    return os.fdopen(fd, mode, encoding="utf-8")


def atomic_write_private(path, data):
    """Write bytes via a 0600 temp file in the same directory, then rename into place."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    path = os.path.abspath(path)
    ensure_private_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        _tighten_fd(fd)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)

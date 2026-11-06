"""Offline checks for inbound-only mode, private bind files, and redacted login errors.

No network. Tokens below are fake fixtures and must not be copied into artifacts.
"""
import io, json, os, stat, sys, tempfile, threading, time, unittest
from datetime import datetime
from unittest import mock
from urllib.error import URLError
from zoneinfo import ZoneInfo

TMP = tempfile.mkdtemp(prefix="gwb-sec-")
os.environ["BRIDGE_DATA_DIR"] = TMP
os.environ["WECHAT_MODE"] = "inbound"
os.environ.pop("WECHAT_WEBHOOK_URL", None)
os.environ.pop("WECHAT_WEBHOOK_KEY", None)
os.environ.pop("WECHAT_SECRETS_FILE", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from grokbot_wechat import bind, config, forward, ilink, outbox, send  # noqa: E402

FAKE_QR = "fake-qr-token"
FAKE_URL = "https://login.invalid/q/" + FAKE_QR
FAKE_BOT = "fake-bot-token"
FAKE_CONTEXT = "fake-context-token"
BJ = ZoneInfo("Asia/Shanghai")


def _bj(*parts):
    return datetime(*parts, tzinfo=BJ).timestamp()


class Resp:
    def __init__(self, payload, status=200):
        self.status = status
        if isinstance(payload, bytes):
            self._data = payload
        elif isinstance(payload, str):
            self._data = payload.encode()
        else:
            self._data = json.dumps(payload).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _refuse_chmod(*_args, **_kwargs):
    raise AssertionError("chmod was used while creating a new private file or directory")


class InboundModeTest(unittest.TestCase):
    def setUp(self):
        self._env = {
            "BRIDGE_DATA_DIR": os.environ.get("BRIDGE_DATA_DIR"),
            "WECHAT_MODE": os.environ.get("WECHAT_MODE"),
            "WECHAT_WEBHOOK_URL": os.environ.get("WECHAT_WEBHOOK_URL"),
            "WECHAT_WEBHOOK_KEY": os.environ.get("WECHAT_WEBHOOK_KEY"),
        }
        self.dir = tempfile.mkdtemp(prefix="gwb-in-")
        os.environ["BRIDGE_DATA_DIR"] = self.dir
        os.environ["WECHAT_MODE"] = "inbound"
        os.environ["WECHAT_WEBHOOK_URL"] = "http://webhook.test/inbound"
        os.environ["WECHAT_WEBHOOK_KEY"] = "fake-webhook-key"
        self.urls = []

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _urlopen(self, req, timeout=None):
        path = req.full_url.split("?", 1)[0]
        self.urls.append(path)
        if path.endswith("/ilink/bot/getupdates"):
            return Resp({
                "ret": 0,
                "get_updates_buf": "cursor-1",
                "msgs": [{
                    "message_type": 1,
                    "from_user_id": "fake-user",
                    "message_id": 7,
                    "create_time_ms": 1700000000000,
                    "context_token": FAKE_CONTEXT,
                    "item_list": [{"type": 1, "text_item": {"text": "ping"}}],
                }],
            })
        if req.full_url.startswith("http://webhook.test/"):
            self.webhook = json.loads(req.data.decode())
            return Resp("ok")
        if path.endswith("/ilink/bot/msg/notifystart") or path.endswith("/ilink/bot/msg/notifystop"):
            return Resp({"ret": 0})
        raise AssertionError("unexpected request")

    def test_sendmessage_is_refused_before_credentials_or_network(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            for endpoint in ("ilink/bot/sendmessage", "ilink/bot/sendtyping", "ilink/bot/getuploadurl"):
                with self.assertRaises(ilink.OutboundDisabled):
                    ilink.post(endpoint, {})
        self.assertEqual(send.main(["ping"]), 5)

    def test_startup_has_flusher_and_no_outbox_thread(self):
        clock = forward.time.time
        batcher = forward.Batcher(
            path=os.path.join(self.dir, "pending.json"), quiet=3, cap=60,
            poster=lambda body: (200, "ok"), clock=lambda: clock(),
        )
        stop = threading.Event()
        threads = forward.start_workers(batcher, stop)
        try:
            self.assertEqual({item.name for item in threads}, {"flusher"})
        finally:
            stop.set()
            for item in threads:
                item.join(2)
                self.assertFalse(item.is_alive())

    def test_poll_delivers_webhook_and_ignores_outbox_and_nudge(self):
        config.atomic_write_private(os.path.join(self.dir, "credentials.json"), json.dumps({
            "bot_token": FAKE_BOT,
            "baseurl": "https://ilinkai.weixin.qq.com",
            "ilink_user_id": "fake-user",
        }).encode())
        queued = [{"text": "queued-note", "to": "fake-user", "queued_at": 1}]
        config.atomic_write_private(
            os.path.join(self.dir, "outbox.json"),
            json.dumps(queued).encode(),
        )
        last = _bj(2026, 10, 1, 18, 0)
        ilink.save_state({"last_inbound_ms": int(last * 1000)})
        forward.setup_logging()
        batcher = forward.Batcher(path=os.path.join(self.dir, "pending.json"), quiet=3, cap=60)
        with mock.patch("urllib.request.urlopen", side_effect=self._urlopen):
            self.assertEqual(forward.run_cycle(batcher), 0)
            batcher.clock = lambda: time.time() + 10
            # quiet window is measured by the batcher's own clock, which started near time.time
            batcher.last = batcher.clock() - 5
            self.assertTrue(batcher.maybe_flush())
            self.assertFalse(outbox.maybe_nudge(last + 18 * 3600 + 60))
            self.assertEqual(outbox.flush(), 0)
        self.assertTrue(any(url.endswith("/ilink/bot/getupdates") for url in self.urls))
        self.assertFalse(any(url.endswith("/ilink/bot/sendmessage") for url in self.urls))
        self.assertFalse(any("sendtyping" in url or "getuploadurl" in url for url in self.urls))
        self.assertEqual(self.webhook["count"], 1)
        self.assertEqual(self.webhook["messages"][0]["texts"], ["ping"])
        self.assertIn("Do not call send.py", self.webhook["reply_rule"])
        self.assertNotIn("context_note", self.webhook)
        self.assertEqual(json.load(open(os.path.join(self.dir, "outbox.json"))), queued)
        state = ilink.load_state()
        self.assertNotIn("nudged_for", state)
        log_text = open(os.path.join(self.dir, "forward.log"), encoding="utf-8").read()
        for secret in (FAKE_BOT, FAKE_CONTEXT, "fake-webhook-key", FAKE_QR):
            self.assertNotIn(secret, log_text)

    def test_unrecognized_mode_stays_inbound(self):
        os.environ["WECHAT_MODE"] = "please-send"
        self.assertFalse(config.outbound_enabled())
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with self.assertRaises(ilink.OutboundDisabled):
                ilink.post("ilink/bot/sendmessage", {})


class BindPrivacyTest(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("BRIDGE_DATA_DIR")
        self._mode = os.environ.get("WECHAT_MODE")
        self.dir = tempfile.mkdtemp(prefix="gwb-bind-")
        os.environ["BRIDGE_DATA_DIR"] = os.path.join(self.dir, "private")
        os.environ["WECHAT_MODE"] = "inbound"
        self.seen = {}

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("BRIDGE_DATA_DIR", None)
        else:
            os.environ["BRIDGE_DATA_DIR"] = self._prev
        if self._mode is None:
            os.environ.pop("WECHAT_MODE", None)
        else:
            os.environ["WECHAT_MODE"] = self._mode

    def test_headers_match_published_client_and_errors_are_redacted(self):
        self.assertEqual(ilink.CHANNEL_VERSION, "2.4.10")
        self.assertEqual(ilink.ILINK_APP_ID, "bot")
        self.assertEqual(ilink.client_version("2.4.10"), 132106)
        captured = []

        def urlopen(req, timeout=None):
            captured.append(req)
            raise URLError(req.full_url)

        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(ilink.LoginTransportError) as raised:
                ilink.fetch_login_qr()
        self.assertNotIn(FAKE_QR, str(raised.exception))
        req = captured[0]
        headers = {key.lower(): value for key, value in req.header_items()}
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/ilink/bot/get_bot_qrcode?bot_type=3"))
        self.assertEqual(headers["authorizationtype"], "ilink_bot_token")
        self.assertEqual(headers["ilink-app-id"], "bot")
        self.assertEqual(headers["ilink-app-clientversion"], "132106")
        self.assertNotIn("authorization", headers)
        import base64
        self.assertTrue(base64.b64decode(headers["x-wechat-uin"]).decode().isdigit())
        body = json.loads(req.data.decode())
        self.assertEqual(body, {"local_token_list": []})
        self.assertNotIn("base_info", body)

        captured.clear()
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(ilink.LoginTransportError) as raised:
                ilink.poll_login(ilink.LOGIN_BASE, FAKE_QR)
        self.assertNotIn(FAKE_QR, str(raised.exception))
        get_headers = {key.lower(): value for key, value in captured[0].header_items()}
        self.assertEqual(captured[0].get_method(), "GET")
        self.assertNotIn("authorizationtype", get_headers)
        self.assertNotIn("x-wechat-uin", get_headers)
        self.assertNotIn("authorization", get_headers)
        self.assertEqual(get_headers["ilink-app-clientversion"], "132106")

    def test_bind_writes_private_qr_and_credentials_without_logging_secrets(self):
        calls = {"n": 0}

        def fetch():
            return FAKE_QR, FAKE_URL

        def poll(base, qrcode, verify_code=None, timeout=40):
            self.assertEqual(qrcode, FAKE_QR)
            path = bind.qr_path()
            self.assertTrue(os.path.isfile(path))
            self.assertEqual(_mode(path), 0o600)
            self.assertEqual(_mode(config.data_dir()), 0o700)
            self.assertFalse(os.path.exists(os.path.join(config.data_dir(), "qr_url.txt")))
            self.assertTrue(open(path, "rb").read().startswith(b"\x89PNG"))
            calls["n"] += 1
            if calls["n"] == 1:
                raise URLError(FAKE_URL + "?qrcode=" + FAKE_QR)
            return {
                "status": "confirmed",
                "bot_token": FAKE_BOT,
                "ilink_bot_id": "fake-bot",
                "ilink_user_id": "fake-user",
                "baseurl": "https://ilinkai.weixin.qq.com",
                "ret": 0,
            }

        stdout = io.StringIO()
        old_umask = os.umask(0)
        try:
            with mock.patch("os.chmod", _refuse_chmod), mock.patch("os.fchmod", _refuse_chmod), \
                    mock.patch("grokbot_wechat.ilink.fetch_login_qr", fetch), \
                    mock.patch("grokbot_wechat.ilink.poll_login", poll), \
                    mock.patch("time.sleep", lambda *_a, **_k: None), \
                    mock.patch("sys.stdout", stdout):
                self.assertEqual(bind.main([]), 0)
        finally:
            os.umask(old_umask)
        text = stdout.getvalue()
        for secret in (FAKE_QR, FAKE_URL, FAKE_BOT, "fake-bot", "fake-user"):
            self.assertNotIn(secret, text)
        self.assertIn("绑定成功", text)
        self.assertIn("查询扫码状态失败", text)
        root = os.path.join(self.dir, "private")
        self.assertEqual(_mode(root), 0o700)
        cred = os.path.join(root, "credentials.json")
        self.assertEqual(_mode(cred), 0o600)
        self.assertFalse(os.path.exists(os.path.join(root, "qr.png")))
        self.assertFalse(os.path.exists(os.path.join(root, "qr_url.txt")))
        saved = json.load(open(cred, encoding="utf-8"))
        self.assertEqual(saved["bot_token"], FAKE_BOT)
        self.assertEqual(calls["n"], 2)

    def test_help_does_not_touch_network(self):
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            self.assertEqual(bind.main(["--help"]), 0)
            self.assertEqual(forward.main(["--help"]), 0)
            with self.assertRaises(SystemExit) as raised:
                send.main(["--help"])
            self.assertEqual(raised.exception.code, 0)

    def test_existing_loose_directory_is_tightened_before_use(self):
        loose = os.path.join(self.dir, "loose")
        os.mkdir(loose, 0o755)
        os.environ["BRIDGE_DATA_DIR"] = loose
        self.assertEqual(_mode(config.data_dir()), 0o700)


class OptInSendTest(unittest.TestCase):
    def setUp(self):
        self._mode = os.environ.get("WECHAT_MODE")
        self._dir = os.environ.get("BRIDGE_DATA_DIR")
        self.dir = tempfile.mkdtemp(prefix="gwb-send-")
        os.environ["BRIDGE_DATA_DIR"] = self.dir
        os.environ["WECHAT_MODE"] = "bidirectional"

    def tearDown(self):
        if self._mode is None:
            os.environ.pop("WECHAT_MODE", None)
        else:
            os.environ["WECHAT_MODE"] = self._mode
        if self._dir is None:
            os.environ.pop("BRIDGE_DATA_DIR", None)
        else:
            os.environ["BRIDGE_DATA_DIR"] = self._dir

    def test_explicit_bidirectional_can_reach_mocked_send(self):
        config.atomic_write_private(os.path.join(self.dir, "credentials.json"), json.dumps({
            "bot_token": FAKE_BOT,
            "baseurl": "https://ilinkai.weixin.qq.com",
            "ilink_user_id": "fake-user",
        }).encode())
        ilink.save_state({"last_from_user_id": "fake-user"})
        seen = []

        def urlopen(req, timeout=None):
            seen.append(req.full_url.split("?", 1)[0])
            self.assertTrue(req.get_header("Authorization"))
            return Resp({"ret": 0})

        with mock.patch("urllib.request.urlopen", urlopen):
            self.assertEqual(send.main(["ping", "--no-context"]), 0)
        self.assertTrue(any(url.endswith("/ilink/bot/sendmessage") for url in seen))

"""Redact login material before it reaches a log line."""
import re

_BEARER = re.compile(r"(?i)bearer\s+\S+")
_FIELDS = re.compile(
    r"(?i)(bot_token|qrcode_img_content|qrcode|verify_code|context_token|"
    r"authorization|ilink_bot_id|ilink_user_id|webhook_key|secret)"
    r"(['\"\s:=]+)([^'\"&\s,}\]]+)"
)
_QUERY = re.compile(r"(?i)((?:qrcode|verify_code|bot_token|token)=)[^&\s]+")
_URL = re.compile(r"https?://\S+")


def redact_text(value):
    text = str(value)
    text = _BEARER.sub("Bearer ***", text)
    text = _FIELDS.sub(r"\1\2***", text)
    text = _QUERY.sub(r"\1***", text)
    text = _URL.sub("https://redacted", text)
    return text

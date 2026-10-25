"""Effective wire options shared by live calls, cache receipts and evaluation reports."""
from urllib.parse import urlsplit

REQUEST_OPTIONS = {"temperature": 0.2, "stream": False}
HYBRID_QWEN = ("qwen3.8-flash", "qwen3.8-max", "qwen3.7-flash", "qwen3.7-plus", "qwen3.7-max",
               "qwen3.6-flash", "qwen3.6-plus", "qwen3.5-flash", "qwen3.5-plus", "qwen-flash", "qwen-plus")


def request_options(settings):
    settings = settings or {}
    options = dict(REQUEST_OPTIONS)
    host = urlsplit(settings.get("ai_base_url", "")).hostname or ""
    model = settings.get("ai_model", "").lower()
    # Only known hybrid Qwen families at Aliyun receive this vendor-specific option.
    if (host == "dashscope.aliyuncs.com" or host.endswith(".maas.aliyuncs.com")) and model.startswith(HYBRID_QWEN) and "preview" not in model and model != "qwen3.7-max-2026-05-17":
        options["enable_thinking"] = False
    return options

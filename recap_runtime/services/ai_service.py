"""Gemini client used by the planner (translation only).

Proxy: GEMINI_PROXY in the .env file wins (e.g. http://127.0.0.1:10808, or "none" for direct).
Without it a local v2rayN proxy on 127.0.0.1:10808 is used when it is running, else direct.
"""
import os
import socket

from google import genai
from google.genai import types  # noqa: F401  (the planner reads ai_service.types)

_LOCAL_PROXY = "http://127.0.0.1:10808"


def gemini_proxy() -> str:
    configured = str(os.getenv("GEMINI_PROXY") or "").strip()
    if configured:
        return "" if configured.lower() in {"none", "off", "direct", "0"} else configured
    try:
        with socket.create_connection(("127.0.0.1", 10808), timeout=0.2):
            return _LOCAL_PROXY
    except OSError:
        return ""


class AIService:
    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("⚠️ GEMINI_API_KEY မတွေ့ပါ! .env ဖိုင်ကို စစ်ဆေးပါ။")
        self.proxy = gemini_proxy()
        self._enable_proxy()
        self.client = genai.Client(api_key=self.api_key)
        self._disable_proxy()
        self.text_model = os.getenv("GEMINI_TEXT_MODEL", "gemini-3.5-flash")

    def _enable_proxy(self):
        if self.proxy:
            os.environ["HTTPS_PROXY"] = self.proxy
            os.environ["HTTP_PROXY"] = self.proxy

    def _disable_proxy(self):
        if self.proxy:
            os.environ.pop("HTTPS_PROXY", None)
            os.environ.pop("HTTP_PROXY", None)

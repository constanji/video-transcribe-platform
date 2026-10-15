from __future__ import annotations

import importlib.util
import socket
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = types.ModuleType("parser_test_modules")
PACKAGE.__path__ = [str(ROOT / "apps" / "api" / "parsers")]
sys.modules[PACKAGE.__name__] = PACKAGE


def load_parser_module(name: str):
    qualified = f"{PACKAGE.__name__}.{name}"
    spec = importlib.util.spec_from_file_location(qualified, ROOT / "apps" / "api" / "parsers" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    spec.loader.exec_module(module)
    return module


common = load_parser_module("common")
ytdlp = load_parser_module("ytdlp")
ParseError = common.ParseError
detect_platform = common.detect_platform
host_matches = common.host_matches
normalize_url = common.normalize_url
validate_public_http_url = common.validate_public_http_url
best_audio_url = ytdlp.best_audio_url
best_url = ytdlp.best_url


class ParserSafetyTests(unittest.TestCase):
    def test_host_matching_requires_domain_boundary(self) -> None:
        self.assertTrue(host_matches("www.bilibili.com", "bilibili.com"))
        self.assertTrue(host_matches("bilibili.com", "bilibili.com"))
        self.assertFalse(host_matches("evilbilibili.com", "bilibili.com"))
        self.assertEqual(detect_platform("https://evilbilibili.com/video/BV1"), "unknown")

    def test_normalize_url_preserves_identity_query(self) -> None:
        normalized = normalize_url("https://Example.com/watch/?utm_source=x&v=abc123&feature=share")
        self.assertEqual(normalized, "https://example.com/watch?v=abc123")

    @patch.object(common.socket, "getaddrinfo")
    def test_private_address_is_rejected(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with self.assertRaises(ParseError):
            validate_public_http_url("http://example.com/video")

    @patch.object(common.socket, "getaddrinfo")
    def test_public_address_is_allowed(self, getaddrinfo) -> None:
        getaddrinfo.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        self.assertEqual(validate_public_http_url("https://example.com/video"), "https://example.com/video")


class YtDlpSelectionTests(unittest.TestCase):
    def test_video_and_audio_are_selected_separately(self) -> None:
        info = {
            "formats": [
                {"url": "https://cdn.test/video-1080", "vcodec": "avc1", "acodec": "none", "height": 1080, "tbr": 2000},
                {"url": "https://cdn.test/video-720", "vcodec": "avc1", "acodec": "none", "height": 720, "tbr": 1200},
                {"url": "https://cdn.test/audio", "vcodec": "none", "acodec": "opus", "abr": 128},
            ]
        }
        self.assertEqual(best_url(info), "https://cdn.test/video-720")
        self.assertEqual(best_audio_url(info), "https://cdn.test/audio")

    def test_progressive_format_is_preferred(self) -> None:
        info = {
            "formats": [
                {"url": "https://cdn.test/video-only", "vcodec": "avc1", "acodec": "none", "height": 720},
                {"url": "https://cdn.test/progressive", "vcodec": "avc1", "acodec": "aac", "height": 360},
            ]
        }
        self.assertEqual(best_url(info), "https://cdn.test/progressive")


if __name__ == "__main__":
    unittest.main()

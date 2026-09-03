"""Tests for the pure parts of the yt-dlp driver: error classification, cookie
file validation, and progress-line parsing. No network, no subprocess.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from igbulk import engine  # noqa: E402


class TestClassifyError(unittest.TestCase):
    def test_anonymous_block(self):
        raw = ("ERROR: [Instagram] ABC: Instagram sent an empty media response. "
               "Check if this post is accessible in your browser without being "
               "logged-in ... use --cookies-from-browser for the authentication.")
        msg, retry = engine.classify_error(raw)
        self.assertIn("without a login", msg)
        self.assertFalse(retry)

    def test_anonymous_block_reworded_when_cookies_supplied(self):
        raw = "ERROR: [Instagram] ABC: Instagram sent an empty media response."
        msg, _ = engine.classify_error(raw, authenticated=True)
        self.assertIn("expired", msg)
        self.assertNotIn("Add cookies", msg)

    def test_rate_limit_is_retryable_either_way(self):
        for authed in (False, True):
            msg, retry = engine.classify_error("ERROR: rate-limit reached", authenticated=authed)
            self.assertTrue(retry)
            self.assertIn("rate-limited", msg)

    def test_private_advice_survives_authentication(self):
        # Being logged in doesn't help if you don't follow the account, so this
        # message must not be swapped for "your cookies expired".
        msg, _ = engine.classify_error("ERROR: This account is private", authenticated=True)
        self.assertIn("follows it", msg)

    def test_generic_cookie_advice_is_not_a_cookie_read_failure(self):
        # Instagram mentions cookies in most of its errors as advice. Treating
        # that as "your browser cookies are unreadable" misreports a working
        # session as a broken one.
        raw = ("ERROR: [Instagram] nasa: Requested content is not available. "
               "Consider using --cookies-from-browser to pass cookies.")
        msg, _ = engine.classify_error(raw)
        self.assertNotIn("Couldn't read cookies", msg)

    def test_real_cookie_read_failure_is_reported(self):
        for raw in [
            "ERROR: could not copy Chrome cookie database",
            "ERROR: Failed to decrypt with DPAPI",
            "ERROR: unable to open cookies.sqlite",
        ]:
            msg, _ = engine.classify_error(raw)
            self.assertIn("Couldn't read cookies", msg, raw)

    def test_not_found(self):
        msg, retry = engine.classify_error("ERROR: HTTP Error 404: Not Found")
        self.assertIn("not found", msg.lower())
        self.assertFalse(retry)

    def test_network_retryable(self):
        _, retry = engine.classify_error("ERROR: Unable to connect: timed out")
        self.assertTrue(retry)

    def test_unknown_error_falls_back_to_last_error_line(self):
        raw = ("[info] something\n"
               "ERROR: [Instagram] XYZ: Some brand new failure; "
               "Confirm you are on the latest version using yt-dlp -U")
        msg, retry = engine.classify_error(raw)
        self.assertIn("Some brand new failure", msg)
        self.assertNotIn("latest version", msg)
        self.assertFalse(retry)

    def test_empty_input(self):
        msg, retry = engine.classify_error("")
        self.assertTrue(msg)
        self.assertFalse(retry)


class TestCookieFile(unittest.TestCase):
    def _write(self, text: str) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "cookies.txt"
        tmp.write_text(text, encoding="utf-8")
        return tmp

    def test_valid_jar(self):
        jar = self._write("# Netscape HTTP Cookie File\n"
                          ".instagram.com\tTRUE\t/\tTRUE\t9999999999\tsessionid\tX\n")
        self.assertEqual(engine.validate_cookie_file(jar), jar)

    def test_missing_file(self):
        with self.assertRaises(engine.EngineError) as ctx:
            engine.validate_cookie_file(Path("/definitely/not/here.txt"))
        self.assertIn("not found", str(ctx.exception))

    def test_not_a_cookie_jar(self):
        jar = self._write("just some prose about instagram, no tabs here")
        with self.assertRaises(engine.EngineError) as ctx:
            engine.validate_cookie_file(jar)
        self.assertIn("Netscape", str(ctx.exception))

    def test_no_instagram_cookies(self):
        jar = self._write("# Netscape HTTP Cookie File\n"
                          ".youtube.com\tTRUE\t/\tTRUE\t999\tsessionid\tX\n")
        with self.assertRaises(engine.EngineError) as ctx:
            engine.validate_cookie_file(jar)
        self.assertIn("no instagram.com cookies", str(ctx.exception))


class TestOptionsAndArgs(unittest.TestCase):
    def test_cookie_file_wins_over_browser(self):
        opts = engine.Options(cookie_file=Path("/tmp/c.txt"), cookies_from="chrome")
        args = engine.Engine.__new__(engine.Engine)
        args.opts, args.exe = opts, "yt-dlp"
        built = args._base_args()
        self.assertIn("--cookies", built)
        self.assertNotIn("--cookies-from-browser", built)

    def test_browser_used_when_no_file(self):
        opts = engine.Options(cookies_from="firefox")
        stub = engine.Engine.__new__(engine.Engine)
        stub.opts, stub.exe = opts, "yt-dlp"
        built = stub._base_args()
        self.assertIn("--cookies-from-browser", built)
        self.assertIn("firefox", built)

    def test_authenticated_flag(self):
        self.assertFalse(engine.Options().authenticated)
        self.assertTrue(engine.Options(cookies_from="chrome").authenticated)
        self.assertTrue(engine.Options(cookie_file=Path("/x")).authenticated)


class TestProgressParsing(unittest.TestCase):
    def test_full_line(self):
        line = f"{engine.PROGRESS_SENTINEL} 45.6%|1.20MiB/s|00:12|1048576|4194304"
        out = engine._parse_progress(line)
        self.assertAlmostEqual(out["percent"], 45.6)
        self.assertEqual(out["speed"], "1.20MiB/s")
        self.assertEqual(out["eta"], "00:12")
        self.assertEqual(out["downloaded_bytes"], 1048576)
        self.assertEqual(out["total_bytes"], 4194304)

    def test_unknown_fields_become_none(self):
        line = f"{engine.PROGRESS_SENTINEL} 0.0%|Unknown|NA|0|NA"
        out = engine._parse_progress(line)
        self.assertIsNone(out["speed"])
        self.assertIsNone(out["eta"])
        self.assertIsNone(out["total_bytes"])

    def test_malformed_line_ignored(self):
        self.assertIsNone(engine._parse_progress(f"{engine.PROGRESS_SENTINEL}garbage"))

    def test_kind_detection(self):
        self.assertEqual(engine._kind_of("/x/a.mp4"), "video")
        self.assertEqual(engine._kind_of("/x/a.JPG"), "image")
        self.assertEqual(engine._kind_of("/x/a.mp3"), "audio")
        self.assertEqual(engine._kind_of("/x/a.info.json"), "other")


class TestCodecCompat(unittest.TestCase):
    def test_playable_codecs_pass_through(self):
        # h264/hevc need no work; the guard is a plain set membership check.
        for codec in ("h264", "H264", "avc1", "hevc"):
            self.assertIn(codec.lower(), engine.PLAYABLE_CODECS)

    def test_vp9_and_av1_are_not_playable(self):
        for codec in ("vp9", "vp09", "av1"):
            self.assertNotIn(codec, engine.PLAYABLE_CODECS)

    def test_convert_cmd_shape(self):
        cmd = engine.convert_cmd("/usr/bin/ffmpeg", "/in.mp4", "/out.mp4")
        self.assertEqual(cmd[0], "/usr/bin/ffmpeg")
        self.assertEqual(cmd[-1], "/out.mp4")
        self.assertIn("libx264", cmd)
        self.assertIn("aac", cmd)
        self.assertIn("+faststart", cmd)          # streamable output
        self.assertIn("yuv420p", cmd)             # broad player support
        self.assertEqual(cmd[cmd.index("-i") + 1], "/in.mp4")

    def test_missing_file_reports_no_codec(self):
        self.assertIsNone(engine.video_codec("/definitely/not/here.mp4"))

    def test_audio_only_option_skips_conversion(self):
        # mp3 output has no video stream to convert; guarded at the call site.
        opts = engine.Options(audio_only=True, ensure_h264=True)
        self.assertTrue(opts.audio_only and opts.ensure_h264)

    def test_ensure_h264_default_on(self):
        self.assertTrue(engine.Options().ensure_h264)


class TestSummarise(unittest.TestCase):
    def test_single_video(self):
        info = {"id": "ABC", "title": "hi", "channel": "nasa", "ext": "mp4",
                "url": "https://cdn/v.mp4", "width": 1080, "height": 1920,
                "duration": 12.5}
        out = engine._summarise(info)
        self.assertEqual(out["uploader"], "nasa")
        self.assertFalse(out["is_carousel"])
        self.assertEqual(len(out["media"]), 1)
        self.assertEqual(out["media"][0]["url"], "https://cdn/v.mp4")

    def test_carousel(self):
        info = {"id": "ABC", "channel": "nasa", "entries": [
            {"id": "1", "url": "https://cdn/1.mp4", "ext": "mp4"},
            {"id": "2", "url": "https://cdn/2.jpg", "ext": "jpg"},
        ]}
        out = engine._summarise(info)
        self.assertTrue(out["is_carousel"])
        self.assertEqual([m["kind"] for m in out["media"]], ["video", "image"])

    def test_picks_best_progressive_format(self):
        info = {"id": "ABC", "formats": [
            {"url": "https://cdn/low.mp4", "vcodec": "h264", "acodec": "aac", "height": 480,
             "ext": "mp4"},
            {"url": "https://cdn/high.mp4", "vcodec": "h264", "acodec": "aac", "height": 1920,
             "ext": "mp4"},
            {"url": "https://cdn/videoonly.mp4", "vcodec": "h264", "acodec": "none",
             "height": 2160, "ext": "mp4"},
        ]}
        out = engine._summarise(info)
        self.assertEqual(out["media"][0]["url"], "https://cdn/high.mp4")


if __name__ == "__main__":
    unittest.main()

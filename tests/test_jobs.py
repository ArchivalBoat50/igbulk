"""Tests for job state: which links get blocked before a request is spent.

Nothing here starts a download — jobs are created with start=False.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from igbulk import jobs, links  # noqa: E402
from igbulk.engine import Options  # noqa: E402


def _job(text: str, opts: Options) -> jobs.Job:
    return jobs.JobManager().create(links.parse(text), opts, start=False)


class TestBlocking(unittest.TestCase):
    STORY = "https://www.instagram.com/stories/nasa/3412345678901234567/"
    PROFILE = "https://www.instagram.com/nasa/"
    REEL = "https://www.instagram.com/reel/AAA111/"

    def test_login_only_kinds_blocked_when_anonymous(self):
        job = _job(f"{self.STORY}\n{self.REEL}", Options())
        by_kind = {i.link.kind: i for i in job.items}
        self.assertEqual(by_kind["story"].status, jobs.BLOCKED)
        self.assertEqual(by_kind["reel"].status, jobs.QUEUED)

    def test_cookie_file_unblocks_stories(self):
        # Regression: the check used to test cookies_from only, so a cookie file
        # left stories blocked even though it authenticates just as well.
        job = _job(self.STORY, Options(cookie_file=Path("/tmp/c.txt")))
        self.assertEqual(job.items[0].status, jobs.QUEUED)

    def test_browser_cookies_unblock_stories(self):
        job = _job(self.STORY, Options(cookies_from="chrome"))
        self.assertEqual(job.items[0].status, jobs.QUEUED)

    def test_profile_stays_blocked_even_with_cookies(self):
        # yt-dlp can't enumerate a profile reliably; attempting it only yields a
        # confusing error, so it's refused up front either way.
        for opts in (Options(), Options(cookies_from="chrome"),
                     Options(cookie_file=Path("/tmp/c.txt"))):
            job = _job(self.PROFILE, opts)
            self.assertEqual(job.items[0].status, jobs.BLOCKED)
            self.assertIn("individual post links", job.items[0].message)

    def test_blocked_items_are_terminal_so_the_job_completes(self):
        job = _job(f"{self.PROFILE}\n{self.STORY}", Options())
        self.assertTrue(job.is_complete)
        self.assertEqual(job.counts[jobs.BLOCKED], 2)


class TestJobPayload(unittest.TestCase):
    def test_counts_and_serialisation(self):
        job = _job("https://www.instagram.com/reel/AAA111/\n"
                   "https://www.instagram.com/nasa/", Options())
        d = job.as_dict()
        self.assertEqual(d["total"], 2)
        self.assertEqual(d["counts"][jobs.BLOCKED], 1)
        self.assertEqual(d["counts"][jobs.QUEUED], 1)
        self.assertEqual(len(d["items"]), 2)
        self.assertIn("dest", d)
        self.assertFalse(d["complete"])

    def test_duplicates_reported(self):
        job = _job("https://www.instagram.com/reel/AAA111/ "
                   "https://www.instagram.com/reel/AAA111/?igsh=x", Options())
        self.assertEqual(job.duplicates, 1)
        self.assertEqual(len(job.items), 1)


class TestManager(unittest.TestCase):
    def test_version_bumps_on_change(self):
        mgr = jobs.JobManager()
        before = mgr.version
        mgr.create(links.parse("https://www.instagram.com/reel/AAA111/"),
                   Options(), start=False)
        self.assertGreater(mgr.version, before)

    def test_wait_for_change_times_out_without_hanging(self):
        mgr = jobs.JobManager()
        current = mgr.version
        self.assertEqual(mgr.wait_for_change(current, timeout=0.05), current)

    def test_cancel_unknown_job(self):
        self.assertFalse(jobs.JobManager().cancel("nope"))

    def test_retry_returns_none_when_nothing_failed(self):
        mgr = jobs.JobManager()
        job = mgr.create(links.parse("https://www.instagram.com/reel/AAA111/"),
                         Options(), start=False)
        self.assertIsNone(mgr.retry_failed(job.id))


if __name__ == "__main__":
    unittest.main()

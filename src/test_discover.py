# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Unit tests for discover.py (new-drug discovery). Run: ``uv run src/test_discover.py``.

No network: ``download_bdpm`` gets a fake ``get``. Written with Claude Code.
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import discover  # noqa: E402


def _catalog(n: int, extra: str = "") -> bytes:
    rows = [f"{60000000 + i}\tDRUG {i}\tcomprimé\n" for i in range(n)]
    return ("".join(rows) + extra).encode("latin-1")


def test_download_validates_before_replacing():
    """A truncated catalog or an HTML error page must never replace the good copy:
    diffing it would report thousands of drugs as removed. A failing optional join
    keeps its previous copy while the catalog still updates."""
    with tempfile.TemporaryDirectory() as d:
        dest = Path(d)
        good = _catalog(discover.MIN_CATALOG_ROWS)
        files = {"CIS_bdpm.txt": good, "CIS_COMPO_bdpm.txt": b"c\n", "CIS_GENER_bdpm.txt": b"g\n"}
        rows = discover.download_bdpm(lambda url: files[url.rsplit("/", 1)[1]], dest)
        assert rows == discover.MIN_CATALOG_ROWS
        assert (dest / "CIS_bdpm.txt").read_bytes() == good

        for bad in (_catalog(10), b"<html>maintenance</html>", b""):
            files["CIS_bdpm.txt"] = bad
            files["CIS_COMPO_bdpm.txt"] = b"new compo\n"
            try:
                discover.download_bdpm(lambda url: files[url.rsplit("/", 1)[1]], dest)
            except ValueError:
                pass
            else:
                raise AssertionError("bad catalog accepted")
            assert (dest / "CIS_bdpm.txt").read_bytes() == good
            assert (dest / "CIS_COMPO_bdpm.txt").read_bytes() == b"c\n"  # all-or-nothing
        assert not list(dest.glob(".*.tmp")), list(dest.iterdir())

        def flaky(url):
            if "GENER" in url:
                raise OSError("timeout")
            return {"CIS_bdpm.txt": good + b"69999999\tNEW\tx\n",
                    "CIS_COMPO_bdpm.txt": b"c2\n"}[url.rsplit("/", 1)[1]]
        discover.download_bdpm(flaky, dest)
        assert (dest / "CIS_GENER_bdpm.txt").read_bytes() == b"g\n"
        assert (dest / "CIS_COMPO_bdpm.txt").read_bytes() == b"c2\n"
    print("ok  test_download_validates_before_replacing")


def test_newest_bdpm_dir_prefers_most_recent_catalog():
    import os
    with tempfile.TemporaryDirectory() as d:
        a, b, c = Path(d, "a"), Path(d, "b"), Path(d, "c")
        for p in (a, b, c):
            p.mkdir()
        (a / "CIS_bdpm.txt").write_text("x")
        (b / "CIS_bdpm.txt").write_text("x")
        os.utime(a / "CIS_bdpm.txt", (1000, 1000))
        assert discover.newest_bdpm_dir(a, b, c) == b
        assert discover.newest_bdpm_dir(c) is None
    print("ok  test_newest_bdpm_dir_prefers_most_recent_catalog")


def test_first_run_seeds_and_takes_backlog_then_diffs():
    """Run 1 must not flag the whole catalog as new: it seeds ``known`` and only
    queues the never-fetched pageless CIS as backlog. Run 2 reports the real diff,
    each new drug in the lane its authorization calls for."""
    with tempfile.TemporaryDirectory() as d:
        st = discover.DiscoveryState(Path(d) / "new-drugs.json")
        names = {"1": "A", "2": "B", "3": "C", "4": "D"}
        lane = lambda cis: "ema" if cis in {"5"} else "ansm"  # noqa: E731
        due = st.apply_catalog(names, lane, has_page={"1", "2"}, attempted={"3"})
        assert due == ["4"] and st.drugs["4"]["kind"] == "backlog", st.drugs
        assert st.data["known"] == ["1", "2", "3", "4"] and "last_found" not in st.data
        st.save()

        st = discover.DiscoveryState(Path(d) / "new-drugs.json")  # reload from disk
        due = st.apply_catalog({"1": "A", "2": "B", "4": "D", "5": "E", "6": "F"}, lane,
                               has_page=set(), attempted=set())
        assert due == ["4", "5", "6"], due  # backlog 4 still pending, no new backlog
        assert st.drugs["5"]["kind"] == "new"
        assert st.drugs["5"]["lane"] == "ema"
        assert st.drugs["6"]["lane"] == "ansm" and "3" in st.data["removed"]
        assert st.data["last_found"]

        st = discover.DiscoveryState(Path(d) / "seeded.json")  # service-seeded first run
        st.seed_known({"1", "2"})
        st.seed_known({"1"})  # no-op once seeded
        due = st.apply_catalog({"1": "A", "2": "B", "3": "C", "7": "G"}, lane,
                               has_page={"1", "2"}, attempted={"3"})
        assert due == ["3", "7"] and st.drugs["3"]["kind"] == "new", st.drugs
        assert st.drugs["7"]["kind"] == "new"  # new wins over backlog
    print("ok  test_first_run_seeds_and_takes_backlog_then_diffs")


def test_results_retry_policy_and_search_rows():
    with tempfile.TemporaryDirectory() as d:
        st = discover.DiscoveryState(Path(d) / "s.json")
        st.data["known"] = ["0"]
        st.data["backlog_done"] = True
        st.apply_catalog({"0": "Z", "1": "A", "2": "B", "3": "C"},
                         lambda c: "ema" if c == "2" else "ansm", set(), set())
        assert not st.note_result("0", "ok")  # not tracked
        st.note_result("1", "ok", slug="1-a")
        st.note_result("2", "ok", slug="2-b")
        st.note_result("3", "empty")
        now = datetime.now(timezone.utc)
        assert st.due_for_fetch(now) == []  # "3" just tried
        assert st.due_for_fetch(now + timedelta(days=discover.RETRY_GAP_DAYS + 1)) == ["3"]
        assert st.due_for_fetch(now + timedelta(days=discover.RETRY_DAYS + 1)) == []
        assert st.search_rows() == [{"cis": "1", "name": "A", "slug": "1-a"},
                                    {"cis": "2", "name": "B", "slug": "2-b", "eu": 1}]
        summ = st.summary(indexed=lambda drug: drug["slug"] == "1-a")
        assert summ["counts"]["new"]["ansm"]["ok"] == 1
        assert summ["counts"]["new"]["ansm"]["indexed"] == 1
        assert summ["counts"]["new"]["ansm"]["empty"] == 1
        assert summ["counts"]["new"]["ema"]["indexed"] == 0
        assert [r["cis"] for r in summ["recent"]] and len(summ["recent"]) == 3
        json.dumps(summ)  # must be JSON-serializable for /api/summary
    print("ok  test_results_retry_policy_and_search_rows")


def test_ema_revisions_keep_first_found_date():
    with tempfile.TemporaryDirectory() as d:
        st = discover.DiscoveryState(Path(d) / "s.json")
        st.note_revisions({"9": "2026-11-23"}, {"9": "X"})
        first = st.data["ema"]["revised"]["9"]["found"]
        st.note_revisions({"9": "2026-11-23"}, {"9": "X"})
        assert st.data["ema"]["revised"]["9"]["found"] == first
        assert st.summary()["ema"]["revised_pending"] == 1
        st.note_revision_fetched("9")
        assert st.summary()["ema"]["revised_pending"] == 0
        st.note_revisions({"9": "2026-12-01"}, {"9": "X"})  # a newer revision: pending again
        assert st.summary()["ema"]["revised_pending"] == 1
    print("ok  test_ema_revisions_keep_first_found_date")


if __name__ == "__main__":
    test_download_validates_before_replacing()
    test_newest_bdpm_dir_prefers_most_recent_catalog()
    test_first_run_seeds_and_takes_backlog_then_diffs()
    test_results_retry_policy_and_search_rows()
    test_ema_revisions_keep_first_found_date()
    print("\nAll tests passed.")

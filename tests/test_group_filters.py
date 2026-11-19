"""Фильтры групп в таблицах (аналитика, выкладка): больше фильтров — уже выборка. Логика — web/src/lib/groups.ts, прогоняется в Node."""

from __future__ import annotations

import _isolate  # noqa: F401  (до импорта engine)

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js не найден")
class GroupFilterLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        run = subprocess.run(["node", "--experimental-strip-types", "--no-warnings", str(ROOT / "tests" / "js" / "groups_check.mts")],
                             capture_output=True, text=True, cwd=ROOT, timeout=60)
        if run.returncode != 0:
            raise AssertionError(run.stderr[-800:])
        cls.r = json.loads(run.stdout)

    def test_no_filter_shows_everything(self) -> None:
        self.assertEqual(self.r["none"], list("ABCDEFG"))

    def test_one_group(self) -> None:
        self.assertEqual(self.r["max"], list("ABCD"))
        self.assertEqual(self.r["stories"], list("CDE"))

    def test_two_groups_narrow_to_the_intersection(self) -> None:
        """«Максим» (4) + «Рассказы» (3) → только каналы, что и там, и там (C, D), а не 5 каналов любой из групп."""
        self.assertEqual(self.r["both"], ["C", "D"])

    def test_more_filters_never_widen(self) -> None:
        self.assertEqual(self.r["three"], [], "ещё одна группа — выборка уже, не шире")
        self.assertLessEqual(len(self.r["both"]), min(len(self.r["max"]), len(self.r["stories"])))

    def test_channels_without_group(self) -> None:
        self.assertEqual(self.r["noGroup"], ["G"])
        self.assertEqual(self.r["noGroupAndGroup"], [], "и «без группы», и в группе — быть не может")

    def test_sync_selection_is_a_union(self) -> None:
        """Что синхронизировать: «Максим» и «Рассказы» → каналы обеих групп (канал из двух — один раз)."""
        self.assertEqual(self.r["unionBoth"], list("ABCDE"))
        self.assertEqual(self.r["unionNone"], ["G"])
        self.assertEqual(self.r["unionMixed"], ["F", "G"])
        self.assertEqual(self.r["unionEmpty"], [], "ничего не выбрано — ничего не синхронизируем")

    def test_toggle_rules(self) -> None:
        t = self.r["toggle"]
        self.assertEqual(t["add"], ["max", "stories"])
        self.assertEqual(t["remove"], ["stories"])
        self.assertEqual(t["noneClearsGroups"], ["none"], "«без группы» снимает группы")
        self.assertEqual(t["groupClearsNone"], ["max"], "выбор группы снимает «без группы»")


class PagesUseTheSameLogicTests(unittest.TestCase):
    def test_pages_import_shared_logic(self) -> None:
        web = ROOT / "web" / "src"
        gf = (web / "components" / "GroupFilter.tsx").read_text(encoding="utf-8")
        page = (web / "pages" / "AnalyticsPage.tsx").read_text(encoding="utf-8")
        self.assertIn('from "../lib/groups"', gf)
        self.assertIn('from "../lib/groups"', page)
        self.assertIn("останутся каналы, которые есть во всех выбранных", gf)
        self.assertNotIn("покажутся каналы любой из них", gf + page, "старое «любой из них» (расширение) не осталось")


if __name__ == "__main__":
    unittest.main()

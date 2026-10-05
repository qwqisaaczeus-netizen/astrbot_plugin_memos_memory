"""Release UI contracts; browser interactions are verified separately."""
from html.parser import HTMLParser
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.ids = []
        self.assets = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])
        asset = values.get("src", values.get("href", ""))
        if asset.startswith("/assets/"):
            self.assets.append(asset)


class ClearBlueTests(unittest.TestCase):
    def test_static_page_ids_remain_unique(self):
        for file in ROOT.glob("*.html"):
            page = Page(file.read_text(encoding="utf-8"))
            self.assertEqual(len(page.ids), len(set(page.ids)), file.name)

    def test_cache_epoch_is_shared(self):
        for file in ROOT.glob("*.html"):
            for asset in Page(file.read_text(encoding="utf-8")).assets:
                if any(name in asset for name in ("workbench.", "atelier.", "scheduling.")):
                    self.assertIn("v=610blue1", asset, file.name)
        console = (ROOT / "webui.py").read_text(encoding="utf-8")
        self.assertIn("atelier.css?v=610blue1", console)

    def test_release_versions_match(self):
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn('_PLUGIN_VERSION = "6.1.0"', main)
        self.assertIn("version: 6.1.0", metadata)
        self.assertIn('atelier-version">6.1.0', (ROOT / "assets/workbench.js").read_text(encoding="utf-8"))

    def test_navigation_does_not_duplicate_overview(self):
        script = (ROOT / "assets/workbench.js").read_text(encoding="utf-8")
        self.assertNotIn("['/','概览'", script)
        self.assertIn("side.querySelectorAll('nav a')", script)
        self.assertIn("location.hash==='#overview'?'':location.hash", script)
        self.assertIn("aria-expanded", script)
        self.assertIn("ArrowDown", script)

    def test_icon_updates_are_scoped(self):
        script = (ROOT / "assets/scheduling.js").read_text(encoding="utf-8")
        self.assertIn("renderSchedulingIcons(routePanel)", script)
        self.assertIn("renderSchedulingIcons(panel)", script)
        self.assertNotIn("window.lucide?.createIcons()", script)
        self.assertIn("removeAttribute('data-lucide')", script)

    def test_palette_reserves_status_colors(self):
        css = (ROOT / "assets/atelier.css").read_text(encoding="utf-8")
        self.assertIn("--accent:#436aab", css)
        self.assertIn("--green:#267852", css)
        self.assertNotIn("#ce5145", css)
        self.assertNotIn("#b44338", css)
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn(".month-day.active", css)
        self.assertIn("box-shadow:inset 0 0 0 2px var(--accent)", css)

    def test_interactions_are_bound_to_existing_data(self):
        page = (ROOT / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn("renderMonthDistribution(s)", page)
        self.assertIn("stats.by_month", page)
        self.assertIn("data-mix-focus", page)
        self.assertIn("Number(comp[k]||0)", page)
        self.assertIn("x.setAttribute('aria-pressed',String(selected))", page)
        self.assertIn("rows.map(itemCard)", page)

    def test_no_demo_data_in_release_ui(self):
        for file in [*ROOT.glob("*.html"), *ROOT.joinpath("assets").glob("*.js")]:
            body = file.read_text(encoding="utf-8")
            self.assertNotIn("__fixtureRequests", body)
            self.assertNotIn('src="/ui-fixtures.js"', body)
            self.assertNotIn("data-preview", body)


if __name__ == "__main__":
    unittest.main()

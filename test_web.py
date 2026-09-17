from html.parser import HTMLParser
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

from cooler_btop.server import MetricsHandler


ROOT = pathlib.Path(__file__).resolve().parent
WEB_ROOT = ROOT / "cooler_btop" / "web"


class AssetReferenceParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.references = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        for name in ("href", "poster", "src"):
            if name in attributes:
                self.references.append(attributes[name])
        if "srcset" in attributes:
            self.references.extend(
                candidate.strip().split()[0]
                for candidate in attributes["srcset"].split(",")
                if candidate.strip()
            )


class WebSafetyTests(unittest.TestCase):
    def test_process_rows_do_not_interpolate_process_data_as_html(self):
        source = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        self.assertNotIn('tr.innerHTML', source)
        self.assertNotIn('onclick="killProcess(', source)
        self.assertIn("nameCell.textContent = String(proc.name", source)

    def test_dashboard_has_no_external_urls_or_scripts(self):
        for path in WEB_ROOT.rglob("*"):
            if not path.is_file():
                continue
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=path.relative_to(WEB_ROOT)):
                self.assertIsNone(re.search(r"(?:https?:)?//[^/\s]", source))
                self.assertNotIn("tailwind", source.lower())
                self.assertNotIn("new Chart", source)

    def test_dashboard_local_asset_references_exist(self):
        index = WEB_ROOT / "index.html"
        parser = AssetReferenceParser()
        source = index.read_text(encoding="utf-8")
        parser.feed(source)
        parser.references.extend(
            match.group(2).strip(" \t\"'")
            for match in re.finditer(r"url\(\s*([\"']?)(.*?)\1\s*\)", source)
        )

        for reference in parser.references:
            if reference.startswith(("#", "data:", "/api/")):
                continue
            asset = WEB_ROOT / reference.split("?", 1)[0].split("#", 1)[0].lstrip("/")
            with self.subTest(reference=reference):
                self.assertTrue(asset.is_file(), f"missing dashboard asset: {reference}")

    def test_dashboard_uses_local_canvas_graphs_and_is_read_only(self):
        source = (WEB_ROOT / "index.html").read_text(encoding="utf-8")

        self.assertIn("<canvas", source)
        self.assertIn("getContext('2d')", source)
        self.assertIn("new EventSource(streamPath, {withCredentials: true})", source)
        self.assertIn("const streamPath = '/api/metrics/stream';", source)
        self.assertIn("window.history.replaceState", source)
        self.assertIn("addEventListener('stale'", source)
        self.assertNotIn("fetch(", source)
        self.assertNotRegex(source, r"/api/(?:kill|terminate)")

    def test_dashboard_script_passes_javascript_syntax_check_when_node_exists(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('node is not installed')
        source = (WEB_ROOT / 'index.html').read_text(encoding='utf-8')
        script = re.search(r'(?s)<script>\s*(.*?)\s*</script>', source)
        self.assertIsNotNone(script)
        with tempfile.NamedTemporaryFile('w', suffix='.js', encoding='utf-8') as file:
            file.write(script.group(1))
            file.flush()
            result = subprocess.run(
                [node, '--check', file.name],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_server_exposes_only_the_packaged_dashboard_asset(self):
        self.assertEqual(
            MetricsHandler.assets,
            {'/': ('index.html', 'text/html; charset=utf-8')},
        )
        for filename, _ in MetricsHandler.assets.values():
            with self.subTest(filename=filename):
                self.assertTrue((WEB_ROOT / filename).is_file())

    def test_security_guidance_describes_the_http_dashboard_as_read_only(self):
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8").lower()

        self.assertIn("read-only", security)
        self.assertNotIn("/api/kill", security)
        self.assertNotIn("remote process termination", security)


if __name__ == '__main__':
    unittest.main()

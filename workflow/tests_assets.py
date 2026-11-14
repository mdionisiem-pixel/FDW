"""Guard: after an update the browser must load the new app.css and app.js.

Static files are served without cache headers, so a browser may reuse its old
copy for a while. Pages then show new markup with old styles and old script:
the reviewer check's work card appeared unstyled with its numbers frozen.
Fingerprinted URLs make every version of a file a different URL.
"""
import os
import re
import tempfile
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from .templatetags import workflow_extras

TEMPLATES = Path(__file__).parent / "templates" / "workflow"


class FingerprintTests(SimpleTestCase):
    def test_the_fingerprint_changes_when_the_file_changes(self):
        folder = tempfile.mkdtemp()
        os.makedirs(os.path.join(folder, "workflow"))
        path = os.path.join(folder, "workflow", "probe.css")
        Path(path).write_text("a { color: red; }")
        with override_settings(STATICFILES_DIRS=[folder]):
            first = workflow_extras.asset("workflow/probe.css")
            self.assertRegex(first, r"probe\.css\?v=[0-9a-f]{12}$")
            self.assertEqual(workflow_extras.asset("workflow/probe.css"), first)
            Path(path).write_text("a { color: blue; }")
            self.assertNotEqual(workflow_extras.asset("workflow/probe.css"), first)

    def test_a_missing_file_falls_back_to_the_plain_url(self):
        self.assertEqual(workflow_extras.asset("workflow/no-such-file.css"), "/static/workflow/no-such-file.css")

    def test_no_template_links_the_app_files_without_a_fingerprint(self):
        for path in TEMPLATES.rglob("*.html"):
            text = path.read_text()
            self.assertIsNone(re.search(r"\{% static '[^']*app\.(css|js)' %\}", text), f"{path.name} links an app file unversioned")


class PageTests(TestCase):
    def test_every_page_loads_fingerprinted_styles_and_script(self):
        user = get_user_model().objects.create_user("owner", password="pw")
        self.client.force_login(user)
        page = self.client.get("/bakeoff/reviewer/").content.decode()
        self.assertRegex(page, r'href="/static/workflow/app\.css\?v=[0-9a-f]{12}"')
        self.assertRegex(page, r'src="/static/workflow/app\.js\?v=[0-9a-f]{12}"')

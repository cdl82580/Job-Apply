"""Tests for /api/admin/kb/seed-from-file — the Node.js KB extraction endpoint.

Regression coverage for 79cfe0e: seed_kb_from_file used to embed the entire
frontend/kb.html content in a `node -e` command-line argument, which hit the
OS's exec() argument-length limit (E2BIG) once the file got big enough,
surfacing as an uncaught OSError that bypassed the endpoint's HTTPException
handling and returned a raw non-JSON error. Node now reads kb.html by path
instead.
"""

from unittest.mock import MagicMock, patch


class TestSeedFromFileAuth:
    def test_requires_auth(self, client):
        r = client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 401

    def test_requires_admin(self, authed_client):
        r = authed_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 403


class TestSeedFromFileRealExtraction:
    """Runs the real Node.js script against the real frontend/kb.html — this
    is the actual code path the argv-length bug affected, so exercising it
    for real (not mocking subprocess.run) is the most direct proof the fix
    holds as kb.html keeps growing."""

    def test_extracts_categories_and_articles_without_crashing(self, admin_client):
        r = admin_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["categories"] > 0
        assert data["articles"] > 0


class TestSeedFromFileErrorHandling:
    def test_subprocess_oserror_returns_json_500_not_a_raw_crash(self, admin_client):
        """The exact bug: OSError (E2BIG) used to propagate unhandled past
        the endpoint's try/except, producing a non-JSON 500 with no detail.
        It must now come back as a normal HTTPException-shaped JSON error."""
        with patch("subprocess.run", side_effect=OSError("Argument list too long")):
            r = admin_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 500
        body = r.json()
        assert "detail" in body
        assert "Argument list too long" in body["detail"]

    def test_node_not_found_returns_500(self, admin_client):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            r = admin_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 500
        assert "Node.js not available" in r.json()["detail"]

    def test_node_timeout_returns_500(self, admin_client):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="node", timeout=15)):
            r = admin_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 500
        assert "timed out" in r.json()["detail"].lower()

    def test_nonzero_exit_code_returns_500_with_stderr(self, admin_client):
        fake_result = MagicMock(returncode=1, stdout="", stderr="KB const not found")
        with patch("subprocess.run", return_value=fake_result):
            r = admin_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 500
        assert "KB const not found" in r.json()["detail"]

    def test_argv_carries_only_the_file_path_not_the_html_content(self, admin_client):
        """The fix's core contract: the -e script argument itself must stay
        small (it just reads kb.html by path) — the html content must never
        be re-embedded into an argv entry."""
        fake_result = MagicMock(returncode=0, stdout='{"categories":[],"articles":[]}', stderr="")
        with patch("subprocess.run", return_value=fake_result) as mock_run:
            r = admin_client.post("/api/admin/kb/seed-from-file")
        assert r.status_code == 200
        argv = mock_run.call_args[0][0]
        assert argv[0] == "node"
        assert "-e" in argv
        script_arg = argv[argv.index("-e") + 1]
        assert len(script_arg) < 2000
        assert "readFileSync" in script_arg
        # The file path is a separate, final argv entry — not embedded in the script.
        assert argv[-1].endswith("kb.html")

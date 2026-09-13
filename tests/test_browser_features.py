"""Local Playwright tests for browser tabs, forms, exports, and recovery."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import Error as PlaywrightError

from plugins.browser.plugin import (
    BrowserDownloadFileTool,
    BrowserFillFormTool,
    BrowserGetPageTextTool,
    BrowserOpenPageTool,
    BrowserRecoveryError,
    BrowserSaveAsPdfTool,
    BrowserSelectOptionTool,
    BrowserSession,
    BrowserUploadFileTool,
    BrowserWaitForTextTool,
    ListBrowserTabsTool,
    SwitchBrowserTabTool,
)


@pytest.fixture
def browser_session(tmp_path):
    BrowserSession._instance = None
    session = BrowserSession.get_instance(tmp_path)
    yield session
    session.close()
    BrowserSession._instance = None


def _write_pages(tmp_path: Path) -> tuple[Path, Path]:
    page_one = tmp_path / "one.html"
    page_two = tmp_path / "two.html"
    html = """<!doctype html>
<html><head><title>{title}</title></head><body>
<h1>{heading}</h1><p>{body}</p>
<form><input id="name"><input id="email">
<select id="choice"><option value="one">One</option><option value="two">Two</option></select>
<input id="upload" type="file"><a id="download" download="hello.txt"
href="data:text/plain;charset=utf-8,hello%20download">Download file</a></form>
<script>setTimeout(() => document.body.insertAdjacentHTML('beforeend', '<p>Loaded later</p>'), 250);</script>
</body></html>"""
    page_one.write_text(html.format(title="First", heading="First tab", body="Alpha"), encoding="utf-8")
    page_two.write_text(html.format(title="Second", heading="Second tab", body="Beta"), encoding="utf-8")
    return page_one, page_two


def test_tabs_switch_and_active_read_calls(browser_session, tmp_path):
    page_one, page_two = _write_pages(tmp_path)
    open_tool = BrowserOpenPageTool(root=tmp_path)
    tabs_tool = ListBrowserTabsTool(root=tmp_path)
    switch_tool = SwitchBrowserTabTool(root=tmp_path)
    text_tool = BrowserGetPageTextTool(root=tmp_path)

    first = open_tool.run(page_one.as_uri())
    second = open_tool.run(page_two.as_uri(), new_tab=True)
    listed = tabs_tool.run()

    assert first["tab_id"] != second["tab_id"]
    assert len(listed["tabs"]) == 2
    assert listed["active_tab_id"] == second["tab_id"]
    assert switch_tool.run(first["tab_id"])["status"] == "switched"
    assert "Alpha" in text_tool.run()["text"]
    assert switch_tool.run(second["tab_id"])["status"] == "switched"
    assert "Beta" in text_tool.run()["text"]


def test_site_tab_reuses_blank_tab_instead_of_creating_a_second_one(tmp_path):
    session = BrowserSession(tmp_path)
    blank_page = MagicMock()
    blank_page.url = "about:blank"
    blank_page.is_closed.return_value = False
    session._context = MagicMock()
    session._context.pages = []
    session._playwright = MagicMock()
    session._tabs = {"1": blank_page}
    session._active_tab_id = "1"

    tab_id, page = session.page_for_site("web.whatsapp.com")

    assert tab_id == "1"
    assert page is blank_page
    session._context.new_page.assert_not_called()


def test_form_upload_download_wait_and_pdf_export(browser_session, tmp_path):
    page_one, _ = _write_pages(tmp_path)
    BrowserOpenPageTool(root=tmp_path).run(page_one.as_uri())
    confirm = MagicMock(return_value=True)

    form = BrowserFillFormTool(confirm_fn=confirm, root=tmp_path)
    result = form.run({"#name": "Ada", "#email": "ada@example.com"})
    assert result["status"] == "filled"
    assert browser_session.get_page().locator("#name").input_value() == "Ada"
    assert browser_session.get_page().locator("#email").input_value() == "ada@example.com"
    assert confirm.call_count == 1

    selected = BrowserSelectOptionTool(confirm_fn=confirm, root=tmp_path).run("#choice", label="Two")
    assert selected["status"] == "selected"
    assert browser_session.get_page().locator("#choice").input_value() == "two"

    upload = tmp_path / "workspace" / "upload.txt"
    upload.parent.mkdir()
    upload.write_text("workspace upload", encoding="utf-8")
    uploaded = BrowserUploadFileTool(confirm_fn=confirm, root=tmp_path).run("#upload", str(upload))
    assert uploaded["status"] == "uploaded"

    waited = BrowserWaitForTextTool(root=tmp_path).run("Loaded later", timeout=2)
    assert waited["status"] == "found"

    downloaded = BrowserDownloadFileTool(confirm_fn=confirm, root=tmp_path).run("#download")
    assert downloaded["status"] == "completed"
    assert Path(downloaded["path"]).name == "hello.txt"
    assert (tmp_path / downloaded["path"]).read_text(encoding="utf-8") == "hello download"

    pdf = BrowserSaveAsPdfTool(root=tmp_path).run()
    assert pdf["status"] == "saved"
    assert (tmp_path / pdf["path"]).stat().st_size > 0


def test_upload_outside_workspace_is_refused_before_confirmation(tmp_path):
    BrowserSession._instance = None
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    confirm = MagicMock(return_value=True)
    result = BrowserUploadFileTool(confirm_fn=confirm, root=tmp_path).run("#upload", str(outside))
    assert "inside the workspace root" in result["error"]
    confirm.assert_not_called()
    BrowserSession._instance = None


def test_crashed_browser_is_restarted_and_retried_once(tmp_path):
    session = BrowserSession(tmp_path)
    first_page = object()
    second_page = object()
    session.get_page = MagicMock(side_effect=[first_page, second_page])
    session.restart = MagicMock()
    attempts = []

    def operation(page):
        attempts.append(page)
        if page is first_page:
            raise PlaywrightError("Target page, context or browser has been closed")
        return {"status": "recovered"}

    assert session.run_with_recovery(operation) == {"status": "recovered"}
    assert attempts == [first_page, second_page]
    session.restart.assert_called_once()


def test_crash_recovery_failure_has_clear_error(tmp_path):
    session = BrowserSession(tmp_path)
    session.get_page = MagicMock(side_effect=[object(), object()])
    session.restart = MagicMock()

    def operation(_page):
        raise PlaywrightError("Target page, context or browser has been closed")

    with pytest.raises(BrowserRecoveryError, match="Browser crashed and was restarted"):
        session.run_with_recovery(operation)

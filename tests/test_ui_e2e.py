"""Browser end-to-end tests of the agent theater (real server, real Chromium).

Skipped when Playwright or a Chromium build isn't available. Set
SCRUTAI_CHROMIUM to point at a specific browser binary.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
playwright = pytest.importorskip("playwright.sync_api")

import uvicorn  # noqa: E402

from scrutai.web.server import STATIC_DIR, create_app  # noqa: E402

if not (STATIC_DIR / "index.html").is_file():
    pytest.skip("UI bundle not built (npm run build in web/)", allow_module_level=True)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def theater_url() -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def chromium() -> Iterator[object]:
    with playwright.sync_playwright() as p:
        candidates = [os.environ.get("SCRUTAI_CHROMIUM"), None, "/opt/pw-browsers/chromium"]
        for exe in candidates:
            try:
                b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
                break
            except Exception:  # noqa: BLE001 - try the next candidate
                continue
        else:
            pytest.skip("no Chromium available for Playwright")
        yield b
        b.close()


def test_demo_review_end_to_end(theater_url: str, chromium: object) -> None:
    page = chromium.new_page()  # type: ignore[attr-defined]
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(theater_url)
    page.get_by_role("button", name="Start review").click()
    page.get_by_test_id("verdict").filter(has_text="request changes").wait_for(timeout=20000)
    upheld = page.get_by_test_id("col-Upheld").get_by_test_id("finding-card")
    killed = page.get_by_test_id("col-Killed").get_by_test_id("finding-card")
    assert upheld.count() == 5 and killed.count() == 1
    assert "1 (17%)" in page.get_by_test_id("stat-killed").inner_text()
    # Expanding a debated card shows the defense.
    card = upheld.filter(has_text="Mutable default argument")
    card.get_by_role("button").click()
    assert "defended it" in card.inner_text()
    assert errors == []


def test_scrubbing_rewinds_the_trial(theater_url: str, chromium: object) -> None:
    page = chromium.new_page()  # type: ignore[attr-defined]
    page.goto(theater_url)
    page.get_by_role("button", name="Start review").click()
    page.get_by_test_id("verdict").filter(has_text="request changes").wait_for(timeout=20000)
    slider = page.get_by_label("Scrub through events")
    slider.fill(str(int(int(slider.get_attribute("max")) * 0.62)))
    on_trial = page.get_by_test_id("col-On trial").get_by_test_id("finding-card")
    assert on_trial.count() >= 1
    assert page.get_by_test_id("verdict").inner_text().lower() == "running"


def test_replay_an_uploaded_trace(theater_url: str, chromium: object, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from scrutai.cli import app

    trace = tmp_path / "run.jsonl"
    CliRunner().invoke(app, ["review", "--demo", "--trace", str(trace)])
    page = chromium.new_page()  # type: ignore[attr-defined]
    page.goto(theater_url)
    page.locator("input[type=file]").set_input_files(str(trace))
    page.get_by_test_id("verdict").filter(has_text="request changes").wait_for(timeout=20000)
    assert page.get_by_test_id("col-Killed").get_by_test_id("finding-card").count() == 1


def test_no_horizontal_scroll_on_phones(theater_url: str, chromium: object) -> None:
    page = chromium.new_page(viewport={"width": 390, "height": 844})  # type: ignore[attr-defined]
    page.goto(theater_url)
    page.get_by_role("button", name="Start review").click()
    page.get_by_test_id("verdict").filter(has_text="request changes").wait_for(timeout=20000)
    assert page.evaluate("document.documentElement.scrollWidth") <= 390

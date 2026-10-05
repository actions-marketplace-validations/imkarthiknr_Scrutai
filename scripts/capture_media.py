"""Regenerate the README's screenshots, screen recording and diagrams.

Screenshots and the recording are captured from actual runs in mock mode (offline,
deterministic):

    docs/images/cli-review.png      `scrutai review --demo --show-dropped`
    docs/images/cli-eval.png        `scrutai eval`
    docs/images/mcp-client.png      examples/mcp/try_it.py talking to `scrutai mcp` over stdio
    docs/images/theater-verdict.png the agent theater after a review
    docs/images/theater-debate.png  a finding's trial: challenge, defense, ruling
    docs/images/theater.gif         the theater replaying a review (also .mp4)
    docs/images/architecture.png    rendered from docs/images/diagrams/architecture.svg
    docs/images/finding-trial.png   rendered from docs/images/diagrams/finding-trial.svg

The diagrams are drawn by hand: edit the SVG sources in docs/images/diagrams/ and
re-render them to docs/images/<name>.png.

Needs: `pip install -e ".[dev]"`, a Chromium for Playwright, and ffmpeg.

    python scripts/capture_media.py            # everything
    python scripts/capture_media.py diagrams   # only the diagrams (no ffmpeg needed)
"""

from __future__ import annotations

import io
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "images"
SCRUTAI = shutil.which("scrutai") or str(Path(sys.executable).with_name("scrutai"))
WIDTH = 104  # terminal columns


def chromium(p):  # type: ignore[no-untyped-def]
    for exe in (os.environ.get("SCRUTAI_CHROMIUM"), None, "/opt/pw-browsers/chromium"):
        try:
            return p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        except Exception:  # noqa: BLE001 - try the next candidate
            continue
    raise SystemExit("no Chromium for Playwright (set SCRUTAI_CHROMIUM)")


# ---- terminal screenshots ---------------------------------------------------


def run(argv: list[str], cwd: Path) -> str:
    env = {**os.environ, "FORCE_COLOR": "1", "COLUMNS": str(WIDTH), "TERM": "xterm-256color"}
    env.pop("NO_COLOR", None)
    proc = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    return proc.stdout


def terminal_svg(prompt: str, ansi: str, title: str) -> str:
    from rich.console import Console
    from rich.text import Text

    console = Console(
        record=True, width=WIDTH, force_terminal=True, color_system="truecolor", file=io.StringIO()
    )
    console.print(Text.assemble(("$ ", "bold green"), (prompt, "bold")))
    console.print(Text.from_ansi(ansi.rstrip()))
    return console.export_svg(title=title)


def svg_to_png(browser, svg: str, out: Path) -> None:  # type: ignore[no-untyped-def]
    page = browser.new_page(device_scale_factor=2)
    page.set_content(f"<html><body style='margin:0;background:#fff'>{svg}</body></html>")
    page.locator("svg").first.screenshot(path=str(out), omit_background=True)
    page.close()


def diagrams(browser) -> None:  # type: ignore[no-untyped-def]
    for src in sorted((OUT / "diagrams").glob("*.svg")):
        out = OUT / f"{src.stem}.png"
        svg_to_png(browser, src.read_text(encoding="utf-8"), out)
        print("wrote", out)


def terminals(browser) -> None:  # type: ignore[no-untyped-def]
    work = Path(tempfile.mkdtemp(prefix="scrutai-media-"))
    shutil.copy(ROOT / ".scrutai.yml", work / ".scrutai.yml")  # mock mode
    shots = [
        (
            "scrutai review --demo --show-dropped",
            [SCRUTAI, "review", "--demo", "--show-dropped"],
            work,
            "cli-review.png",
        ),
        ("scrutai eval", [SCRUTAI, "eval"], ROOT, "cli-eval.png"),
        (
            "python examples/mcp/try_it.py",
            [sys.executable, "-W", "ignore", str(ROOT / "examples/mcp/try_it.py")],
            work,
            "mcp-client.png",
        ),
    ]
    for prompt, argv, cwd, name in shots:
        out = run(argv, cwd)
        if name == "cli-eval.png":  # the headline table; the per-category list is long
            out = out.split("Per category")[0]
        svg_to_png(browser, terminal_svg(prompt, out, "Scrutai"), OUT / name)
        print("wrote", OUT / name)
    shutil.rmtree(work, ignore_errors=True)


# ---- the agent theater ------------------------------------------------------


@contextmanager
def theater() -> Iterator[str]:
    import uvicorn

    from scrutai.web.server import create_app

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    cwd = os.getcwd()
    work = tempfile.mkdtemp(prefix="scrutai-theater-")
    shutil.copy(ROOT / ".scrutai.yml", Path(work) / ".scrutai.yml")
    os.chdir(work)
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        os.chdir(cwd)
        shutil.rmtree(work, ignore_errors=True)


def run_demo(page) -> None:  # type: ignore[no-untyped-def]
    page.get_by_role("button", name="Start review").click()
    page.get_by_test_id("verdict").filter(has_text="request changes").wait_for(timeout=30000)


def screenshots(browser, url: str) -> None:  # type: ignore[no-untyped-def]
    page = browser.new_page(viewport={"width": 1360, "height": 900}, device_scale_factor=2)
    page.goto(url)
    run_demo(page)
    page.wait_for_timeout(500)
    page.screenshot(path=str(OUT / "theater-verdict.png"))
    print("wrote", OUT / "theater-verdict.png")

    card = page.get_by_test_id("col-Upheld").get_by_test_id("finding-card")
    card = card.filter(has_text="Mutable default argument")
    card.get_by_role("button").click()
    page.wait_for_timeout(300)
    board = page.get_by_role("region", name="Findings on trial")
    board.screenshot(path=str(OUT / "theater-debate.png"))
    print("wrote", OUT / "theater-debate.png")
    page.close()


def recording(browser, url: str) -> None:  # type: ignore[no-untyped-def]
    """Record the theater replaying a finished review at 1×, as a GIF and an MP4."""
    video_dir = Path(tempfile.mkdtemp(prefix="scrutai-video-"))
    size = {"width": 1280, "height": 1180}
    context = browser.new_context(
        viewport=size, record_video_dir=str(video_dir), record_video_size=size
    )
    page = context.new_page()
    page.goto(url)
    run_demo(page)
    page.wait_for_timeout(600)
    started = time.monotonic()
    page.get_by_role("button", name="Replay", exact=True).click()
    page.get_by_role("button", name="Replay", exact=True).wait_for(
        timeout=120000
    )  # back to "Replay" when done
    page.wait_for_timeout(2500)  # hold on the verdict
    offset = time.monotonic() - started
    context.close()
    (webm,) = video_dir.glob("*.webm")
    total = float(
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(webm),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    start = max(0.0, total - offset - 0.3)  # trim the page load and the instant mock run
    mp4, gif = OUT / "theater.mp4", OUT / "theater.gif"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.2f}",
            "-i",
            str(webm),
            "-vf",
            "fps=24,scale=1280:-2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-crf",
            "28",
            str(mp4),
        ],
        check=True,
    )
    palette = video_dir / "palette.png"
    scale = "fps=10,scale=880:-1:flags=lanczos"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.2f}",
            "-i",
            str(webm),
            "-vf",
            f"{scale},palettegen=max_colors=128:stats_mode=diff",
            str(palette),
        ],
        check=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-ss",
            f"{start:.2f}",
            "-i",
            str(webm),
            "-i",
            str(palette),
            "-lavfi",
            f"{scale}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
            str(gif),
        ],
        check=True,
    )
    shutil.rmtree(video_dir, ignore_errors=True)
    for f in (mp4, gif):
        print("wrote", f, f"{f.stat().st_size / 1e6:.1f} MB")


def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = chromium(p)
        diagrams(browser)
        if sys.argv[1:] != ["diagrams"]:
            terminals(browser)
            with theater() as url:
                screenshots(browser, url)
                recording(browser, url)
        browser.close()


if __name__ == "__main__":
    main()

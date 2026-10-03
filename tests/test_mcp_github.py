"""review_pull_request and post_review, against the fake GitHub API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fake_github import REPO, FakeGitHub
from mcp_util import annotations, in_memory, is_error, structured, text

from scrutai.mcp.server import Settings, build_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def make(root: Path, **kw: Any) -> Any:
    return build_server(Settings(roots=[str(root)], **kw))


def yes(message: str) -> dict[str, Any]:
    return {"post": True}


async def review_pr(client: Any) -> dict[str, Any]:
    return structured(await client.call_tool("review_pull_request", {"pr": 7}))


async def test_review_pull_request_reads_only(github: FakeGitHub, tmp_path: Path) -> None:
    async with in_memory(make(tmp_path)) as client:
        data = await review_pr(client)
    assert data["label"] == "PR #7" and data["verdict"] == "request_changes"
    assert [(f["file"], f["line"], f["category"]) for f in data["findings"]] == [
        ("svc/run.py", 6, "injection")
    ]
    assert github.issue_comments == [] and github.reviews == []


async def test_post_review_after_the_user_confirms_and_stays_idempotent(
    github: FakeGitHub, tmp_path: Path
) -> None:
    asked: list[str] = []

    def confirm(message: str) -> dict[str, Any]:
        asked.append(message)
        return {"post": True}

    async with in_memory(make(tmp_path), elicit=confirm) as client:
        rid = (await review_pr(client))["review_id"]
        first = structured(await client.call_tool("post_review", {"review_id": rid}))
        again = structured(await client.call_tool("post_review", {"review_id": rid}))
    assert f"{REPO}#7" in asked[0] and "1 inline comment" in asked[0]
    assert first == {
        "review_id": rid,
        "repo": REPO,
        "pr": 7,
        "status": "posted",
        "summary_comment": "created",
        "posted": 1,
        "skipped_duplicates": 0,
        "skipped_off_diff": 0,
    }
    assert again["summary_comment"] == "updated" and again["posted"] == 0
    assert again["skipped_duplicates"] == 1
    assert len(github.issue_comments) == 1 and len(github.reviews) == 1


async def test_declining_posts_nothing(github: FakeGitHub, tmp_path: Path) -> None:
    async with in_memory(make(tmp_path), elicit=lambda message: None) as client:
        rid = (await review_pr(client))["review_id"]
        result = structured(await client.call_tool("post_review", {"review_id": rid}))
        # confirm=true does not override a user who said no.
        forced = structured(
            await client.call_tool("post_review", {"review_id": rid, "confirm": True})
        )
    assert result["status"] == forced["status"] == "declined" and result["posted"] == 0
    assert github.issue_comments == [] and github.reviews == []


async def test_without_elicitation_posting_needs_explicit_confirm(
    github: FakeGitHub, tmp_path: Path
) -> None:
    async with in_memory(make(tmp_path)) as client:  # this client cannot elicit
        rid = (await review_pr(client))["review_id"]
        refused = await client.call_tool("post_review", {"review_id": rid})
        assert is_error(refused) and "confirm=true" in text(refused)
        assert github.issue_comments == []
        posted = structured(
            await client.call_tool("post_review", {"review_id": rid, "confirm": True})
        )
    assert posted["status"] == "posted" and posted["posted"] == 1


async def test_no_post_hides_the_tool(github: FakeGitHub, tmp_path: Path) -> None:
    async with in_memory(make(tmp_path, allow_post=False), elicit=yes) as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        rid = (await review_pr(client))["review_id"]
        result = await client.call_tool("post_review", {"review_id": rid, "confirm": True})
    assert "post_review" not in tools and "review_pull_request" in tools
    assert is_error(result) and github.issue_comments == []


async def test_post_review_is_annotated_destructive(tmp_path: Path) -> None:
    async with in_memory(make(tmp_path)) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    hints = annotations(tools["post_review"])
    assert hints["destructiveHint"] is True and hints["readOnlyHint"] is False
    assert annotations(tools["review_pull_request"])["readOnlyHint"] is True


async def test_only_complete_pull_request_reviews_can_be_posted(
    github: FakeGitHub, tmp_path: Path
) -> None:
    patch = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+eval(x)\n"
    srv = make(tmp_path)
    async with in_memory(srv, elicit=yes) as client:
        local = structured(await client.call_tool("review_patch", {"patch": patch}))
        not_pr = await client.call_tool("post_review", {"review_id": local["review_id"]})
        rid = (await review_pr(client))["review_id"]
        srv.scrutai.runs.get(rid).outcome.result.cancelled = True
        partial = await client.call_tool("post_review", {"review_id": rid})
        unknown = await client.call_tool("post_review", {"review_id": "nope"})
    assert is_error(not_pr) and "only review_pull_request" in text(not_pr)
    assert is_error(partial) and "partial" in text(partial)
    assert is_error(unknown) and "no review 'nope'" in text(unknown)
    assert github.issue_comments == []


async def test_a_pull_request_that_moved_on_is_not_posted(
    github: FakeGitHub, tmp_path: Path
) -> None:
    async with in_memory(make(tmp_path), elicit=yes) as client:
        rid = (await review_pr(client))["review_id"]
        github.head = "sha2"  # a new push after the review
        result = await client.call_tool("post_review", {"review_id": rid})
    assert is_error(result) and "new commits since this review" in text(result)
    assert github.issue_comments == [] and github.reviews == []


async def test_missing_token_is_a_clear_error(
    github: FakeGitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN")
    async with in_memory(make(tmp_path)) as client:
        result = await client.call_tool("review_pull_request", {"pr": 7})
        bad = await client.call_tool("review_pull_request", {"pr": 0})
    assert is_error(result) and "GITHUB_TOKEN" in text(result)
    assert is_error(bad)

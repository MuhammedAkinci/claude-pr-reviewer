"""Claude-powered GitHub Pull Request reviewer.

Reads the PR context from the GitHub Actions environment, fetches the diff,
asks Claude for a structured review, and posts the review back as a PR comment.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import anthropic
import pathspec
import requests

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
log = logging.getLogger("claude-pr-review")

GITHUB_API = "https://api.github.com"
COMMENT_MARKER = "<!-- claude-pr-review:marker -->"

REVIEW_LEVELS = {"strict", "balanced", "lenient"}

RATE_LIMIT_MAX_RETRIES = 5
RATE_LIMIT_BASE_DELAY = 5.0


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    github_token: str
    repository: str
    event_path: str
    event_name: str
    workspace: Path
    model: str
    review_level: str
    ignore_file: str
    max_diff_chars: int
    max_tokens: int


@dataclass(frozen=True)
class PullRequest:
    number: int
    head_sha: str
    base_sha: str
    title: str
    body: str


@dataclass(frozen=True)
class FileDiff:
    path: str
    patch: str


class ReviewError(RuntimeError):
    """Raised when the review pipeline encounters a fatal, user-facing error."""


def load_config() -> Config:
    def _required(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise ReviewError(f"Missing required environment variable: {name}")
        return value

    try:
        max_diff_chars = int(os.environ.get("INPUT_MAX_DIFF_CHARS", "100000"))
        max_tokens = int(os.environ.get("INPUT_MAX_TOKENS", "4096"))
    except ValueError as exc:
        raise ReviewError(f"Invalid numeric input: {exc}") from exc

    review_level = os.environ.get("INPUT_REVIEW_LEVEL", "balanced").strip().lower()
    if review_level not in REVIEW_LEVELS:
        raise ReviewError(
            f"Invalid review_level '{review_level}'. "
            f"Expected one of: {', '.join(sorted(REVIEW_LEVELS))}"
        )

    workspace = Path(os.environ.get("GITHUB_WORKSPACE", os.getcwd()))

    return Config(
        anthropic_api_key=_required("ANTHROPIC_API_KEY"),
        github_token=_required("GITHUB_TOKEN"),
        repository=_required("GITHUB_REPOSITORY"),
        event_path=_required("GITHUB_EVENT_PATH"),
        event_name=os.environ.get("GITHUB_EVENT_NAME", ""),
        workspace=workspace,
        model=os.environ.get("INPUT_MODEL", "claude-sonnet-4-6").strip(),
        review_level=review_level,
        ignore_file=os.environ.get("INPUT_IGNORE_FILE", ".claude-review-ignore").strip(),
        max_diff_chars=max_diff_chars,
        max_tokens=max_tokens,
    )


def load_pull_request(event_path: str) -> PullRequest:
    try:
        with open(event_path, "r", encoding="utf-8") as handle:
            event = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewError(f"Failed to read GitHub event payload: {exc}") from exc

    pr = event.get("pull_request")
    if not pr:
        raise ReviewError(
            "Event payload has no 'pull_request' field — this action must run on pull_request events."
        )

    return PullRequest(
        number=pr["number"],
        head_sha=pr["head"]["sha"],
        base_sha=pr["base"]["sha"],
        title=pr.get("title", ""),
        body=pr.get("body") or "",
    )


def _github_headers(token: str, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "claude-pr-review-action",
    }


def fetch_diff(config: Config, pr: PullRequest) -> str:
    url = f"{GITHUB_API}/repos/{config.repository}/pulls/{pr.number}"
    headers = _github_headers(config.github_token, accept="application/vnd.github.v3.diff")
    log.info("Fetching PR diff from %s", url)
    response = requests.get(url, headers=headers, timeout=60)
    if response.status_code == 403:
        raise ReviewError(
            "GitHub API returned 403 fetching diff. "
            "Ensure the workflow grants 'pull-requests: read' and 'contents: read'."
        )
    if response.status_code == 404:
        raise ReviewError("PR not found or token lacks access.")
    response.raise_for_status()
    return response.text


def load_ignore_spec(workspace: Path, ignore_file: str) -> pathspec.PathSpec | None:
    ignore_path = workspace / ignore_file
    if not ignore_path.is_file():
        log.info("No ignore file at %s — reviewing all files.", ignore_path)
        return None
    try:
        patterns = ignore_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("Could not read ignore file %s: %s", ignore_path, exc)
        return None
    log.info("Loaded %d ignore patterns from %s", len(patterns), ignore_path)
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


_FILE_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)


def split_diff_by_file(diff: str) -> list[FileDiff]:
    """Split a unified diff into per-file chunks."""
    matches = list(_FILE_HEADER_RE.finditer(diff))
    files: list[FileDiff] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(diff)
        # Prefer the "b/" path (post-image) for new/modified files; falls back to "a/" for deletions.
        path = match.group(2) if match.group(2) != "/dev/null" else match.group(1)
        files.append(FileDiff(path=path, patch=diff[start:end]))
    return files


def filter_files(files: Iterable[FileDiff], spec: pathspec.PathSpec | None) -> list[FileDiff]:
    if spec is None:
        return list(files)
    kept: list[FileDiff] = []
    for f in files:
        if spec.match_file(f.path):
            log.info("Skipping ignored file: %s", f.path)
            continue
        kept.append(f)
    return kept


def build_system_prompt(review_level: str) -> str:
    level_guidance = {
        "strict": (
            "Apply a strict review. Flag every likely bug, security risk, "
            "performance regression, and style deviation. Do not skip minor issues."
        ),
        "balanced": (
            "Apply a balanced review. Focus on meaningful issues — real bugs, "
            "security concerns, clear performance problems, and notable style issues. "
            "Skip trivial nitpicks."
        ),
        "lenient": (
            "Apply a lenient review. Only flag critical bugs, security vulnerabilities, "
            "or severe performance problems. Ignore style unless it hurts readability significantly."
        ),
    }[review_level]

    return (
        "You are a senior software engineer performing a pull request review.\n"
        f"{level_guidance}\n\n"
        "Return your review as GitHub-flavored Markdown with these sections, in order:\n"
        "## Summary\n"
        "A 2-3 sentence overview of the change and its overall quality.\n\n"
        "## Bugs\n"
        "Potential logic errors, crashes, incorrect behavior. Use bullet points; "
        "reference file paths and line ranges when possible. Say 'None found' if empty.\n\n"
        "## Security\n"
        "Injection, auth, secret handling, unsafe input, insecure defaults. Same format.\n\n"
        "## Performance\n"
        "Inefficient loops, N+1 queries, unnecessary allocations, blocking I/O. Same format.\n\n"
        "## Code Style\n"
        "Readability, naming, structure, dead code. Same format. Keep it brief.\n\n"
        "## Suggestions\n"
        "Concrete, actionable improvements. Prefer code snippets for non-trivial suggestions.\n\n"
        "Rules:\n"
        "- Be precise. Do not invent code that isn't in the diff.\n"
        "- If there are no issues in a section, write 'None found.'\n"
        "- Do not repeat the diff back.\n"
        "- Do not add sections beyond the ones listed above."
    )


def build_user_prompt(pr: PullRequest, payload: str, *, scope: str) -> str:
    return (
        f"# Pull Request #{pr.number}: {pr.title}\n\n"
        f"**Scope:** {scope}\n\n"
        f"## Description\n{pr.body or '(no description provided)'}\n\n"
        f"## Diff\n"
        f"```diff\n{payload}\n```\n"
    )


def call_claude(
    client: anthropic.Anthropic,
    *,
    model: str,
    max_tokens: int,
    system_prompt: str,
    user_prompt: str,
) -> str:
    for attempt in range(1, RATE_LIMIT_MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except anthropic.RateLimitError as exc:
            if attempt == RATE_LIMIT_MAX_RETRIES:
                raise ReviewError(f"Claude API rate limit exceeded after {attempt} retries.") from exc
            delay = RATE_LIMIT_BASE_DELAY * (2 ** (attempt - 1))
            log.warning("Rate limited by Claude API (attempt %d). Sleeping %.1fs.", attempt, delay)
            time.sleep(delay)
            continue
        except anthropic.APIStatusError as exc:
            raise ReviewError(f"Claude API error ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            if attempt == RATE_LIMIT_MAX_RETRIES:
                raise ReviewError(f"Could not reach Claude API: {exc}") from exc
            delay = RATE_LIMIT_BASE_DELAY * (2 ** (attempt - 1))
            log.warning("Connection error calling Claude (attempt %d). Sleeping %.1fs.", attempt, delay)
            time.sleep(delay)
            continue

        parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        return "\n".join(parts).strip()

    raise ReviewError("Exhausted retries calling Claude API.")


def review_whole_diff(
    client: anthropic.Anthropic,
    config: Config,
    pr: PullRequest,
    diff: str,
) -> str:
    log.info("Reviewing PR as a single diff (%d chars).", len(diff))
    system = build_system_prompt(config.review_level)
    user = build_user_prompt(pr, diff, scope="Full pull request")
    return call_claude(
        client,
        model=config.model,
        max_tokens=config.max_tokens,
        system_prompt=system,
        user_prompt=user,
    )


def review_per_file(
    client: anthropic.Anthropic,
    config: Config,
    pr: PullRequest,
    files: list[FileDiff],
) -> str:
    log.info("Diff exceeds %d chars — reviewing %d files individually.", config.max_diff_chars, len(files))
    system = build_system_prompt(config.review_level)
    sections: list[str] = []

    for idx, file in enumerate(files, start=1):
        log.info("Reviewing file %d/%d: %s", idx, len(files), file.path)
        user = build_user_prompt(pr, file.patch, scope=f"File: {file.path}")
        try:
            review = call_claude(
                client,
                model=config.model,
                max_tokens=config.max_tokens,
                system_prompt=system,
                user_prompt=user,
            )
        except ReviewError as exc:
            log.error("Failed to review %s: %s", file.path, exc)
            review = f"_Review failed for this file: {exc}_"
        sections.append(f"### `{file.path}`\n\n{review}")

    joined = "\n\n---\n\n".join(sections)
    return (
        "## Summary\n"
        "This PR is large, so each file was reviewed independently below.\n\n"
        "## Per-file Reviews\n\n"
        f"{joined}"
    )


def render_comment(body: str, *, model: str, review_level: str) -> str:
    header = (
        f"{COMMENT_MARKER}\n"
        f"# Claude PR Review\n"
        f"_Model: `{model}` · Level: `{review_level}`_\n\n"
    )
    footer = (
        "\n\n---\n"
        "_This review was generated automatically by the "
        "[Claude PR Review](https://github.com/marketplace/actions/claude-pr-review) action. "
        "Treat it as a second opinion, not a replacement for human review._"
    )
    return header + body + footer


def find_existing_comment(config: Config, pr: PullRequest) -> int | None:
    url = f"{GITHUB_API}/repos/{config.repository}/issues/{pr.number}/comments"
    headers = _github_headers(config.github_token)
    params = {"per_page": 100}
    page = 1
    while True:
        response = requests.get(url, headers=headers, params={**params, "page": page}, timeout=30)
        response.raise_for_status()
        comments = response.json()
        for comment in comments:
            if COMMENT_MARKER in (comment.get("body") or ""):
                return comment["id"]
        if len(comments) < params["per_page"]:
            return None
        page += 1


def upsert_comment(config: Config, pr: PullRequest, body: str) -> None:
    headers = _github_headers(config.github_token)
    existing_id = find_existing_comment(config, pr)

    if existing_id is not None:
        url = f"{GITHUB_API}/repos/{config.repository}/issues/comments/{existing_id}"
        log.info("Updating existing review comment %s", existing_id)
        response = requests.patch(url, headers=headers, json={"body": body}, timeout=30)
    else:
        url = f"{GITHUB_API}/repos/{config.repository}/issues/{pr.number}/comments"
        log.info("Creating new review comment on PR #%s", pr.number)
        response = requests.post(url, headers=headers, json={"body": body}, timeout=30)

    if response.status_code == 403:
        raise ReviewError(
            "GitHub API returned 403 when posting the comment. "
            "Ensure the workflow grants 'pull-requests: write'."
        )
    response.raise_for_status()


def main() -> int:
    try:
        config = load_config()
    except ReviewError as exc:
        log.error("%s", exc)
        return 1

    log.info("Model=%s, level=%s, max_diff_chars=%d", config.model, config.review_level, config.max_diff_chars)

    if config.event_name and config.event_name != "pull_request":
        log.warning("Event is '%s' (expected 'pull_request'). Continuing anyway.", config.event_name)

    try:
        pr = load_pull_request(config.event_path)
        log.info("Reviewing PR #%d: %s", pr.number, pr.title)

        diff = fetch_diff(config, pr)
        if not diff.strip():
            log.info("Diff is empty — nothing to review.")
            return 0

        spec = load_ignore_spec(config.workspace, config.ignore_file)
        files = filter_files(split_diff_by_file(diff), spec)

        if not files:
            log.info("All files filtered out by ignore patterns — skipping review.")
            return 0

        filtered_diff = "".join(f.patch for f in files)
        client = anthropic.Anthropic(api_key=config.anthropic_api_key)

        if len(filtered_diff) <= config.max_diff_chars:
            body = review_whole_diff(client, config, pr, filtered_diff)
        else:
            body = review_per_file(client, config, pr, files)

        comment = render_comment(body, model=config.model, review_level=config.review_level)
        upsert_comment(config, pr, comment)
        log.info("Review posted successfully.")
        return 0

    except ReviewError as exc:
        log.error("Review failed: %s", exc)
        return 1
    except requests.HTTPError as exc:
        log.error("GitHub API HTTP error: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

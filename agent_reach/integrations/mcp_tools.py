# -*- coding: utf-8 -*-
"""Read-only MCP tools that route chat requests to upstream tools.

Chat clients such as Claude Desktop cannot run shell commands, so the skill's
"call yt-dlp / rdt / twitter yourself" model does not reach them. Each tool
here runs exactly one documented upstream command (the same ones listed in
``agent_reach/skill/references``) and returns its output. Nothing is
reimplemented: if an upstream CLI is missing or logged out, the tool says so.

Security boundaries (tool arguments come from a model that may have read
hostile web content):
  - Commands are argv lists, never a shell string.
  - Free-text values may not start with ``-``, so they cannot become options.
  - URLs must be public HTTP(S); platform tools also require the platform host.
  - On Windows, npm shims are ``.cmd`` files that cmd.exe re-parses, so values
    containing cmd.exe metacharacters are refused for those binaries.
  - Every tool is read-only. No tool posts, logs in, or reads browser cookies.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from agent_reach.utils.process import utf8_subprocess_env
from agent_reach.utils.text import scrub_url_credentials
from agent_reach.utils.url import host_matches, normalize_public_http_url

MAX_OUTPUT_CHARS = 100_000
_MAX_TEXT_ARG = 500
_MAX_FEED_BYTES = 5 * 1024 * 1024
_DEFAULT_TIMEOUT = 90
_WINDOWS_BATCH_META = set('"%&|<>^!`\r\n')


class ToolError(Exception):
    """A user-facing failure: missing tool, bad argument, empty result."""


# --------------------------------------------------------------------------- #
# Argument helpers
# --------------------------------------------------------------------------- #


def _text(args: Mapping[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"'{key}' is required")
    value = value.strip()
    if len(value) > _MAX_TEXT_ARG:
        raise ToolError(f"'{key}' is longer than {_MAX_TEXT_ARG} characters")
    if value.startswith("-"):
        raise ToolError(f"'{key}' may not start with '-'")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ToolError(f"'{key}' may not contain control characters")
    return value


def _int(args: Mapping[str, Any], key: str, default: int, low: int, high: int) -> int:
    value = args.get(key, default)
    if isinstance(value, bool):
        raise ToolError(f"'{key}' must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"'{key}' must be an integer") from None
    return max(low, min(high, number))


def _match(value: str, pattern: str, label: str) -> str:
    if not re.fullmatch(pattern, value):
        raise ToolError(f"'{value}' is not a valid {label}")
    return value


def _platform_url(args: Mapping[str, Any], key: str, *domains: str) -> str:
    raw = _text(args, key)
    try:
        url = normalize_public_http_url(raw)
    except ValueError:
        raise ToolError(f"'{key}' must be a public http(s) URL") from None
    if not host_matches(url, *domains):
        raise ToolError(f"'{key}' must be a {' / '.join(domains)} URL")
    return url


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n\n[truncated at {MAX_OUTPUT_CHARS} characters]"


# --------------------------------------------------------------------------- #
# Upstream command runner
# --------------------------------------------------------------------------- #


def _check_windows_batch_args(executable: str, args: Sequence[str]) -> None:
    if not executable.lower().endswith((".cmd", ".bat")):
        return
    for arg in args:
        if _WINDOWS_BATCH_META & set(arg):
            raise ToolError(
                "this value contains characters that cmd.exe would interpret "
                '(" % & | < > ^ !); remove them and try again'
            )


def run_cli(
    binary: str,
    args: Sequence[str],
    *,
    timeout: int = _DEFAULT_TIMEOUT,
    extra_env: Optional[Mapping[str, str]] = None,
) -> str:
    """Run one upstream command and return its non-empty stdout."""
    executable = shutil.which(binary)
    if not executable:
        raise ToolError(f"{binary} is not installed")
    _check_windows_batch_args(executable, args)
    env = utf8_subprocess_env()
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            [executable, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"{binary} timed out after {timeout}s") from None
    except OSError as exc:
        raise ToolError(f"{binary} could not start: {exc}") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[-800:]
        raise ToolError(
            f"{binary} failed (exit {proc.returncode}): {scrub_url_credentials(detail)}"
        )
    output = (proc.stdout or "").strip()
    if not output:
        raise ToolError(f"{binary} returned no content")
    return output


Attempt = Tuple[str, str, List[str], Optional[Mapping[str, str]]]


def run_first(attempts: Sequence[Attempt], *, setup_hint: str, timeout: int = _DEFAULT_TIMEOUT) -> str:
    """Try backends in order; return the first non-empty result.

    Each attempt is ``(label, binary, args, extra_env)``. A missing binary is
    skipped silently; a failing one is recorded and the next is tried.
    """
    failures = []
    for label, binary, args, extra_env in attempts:
        if not shutil.which(binary):
            continue
        try:
            return _truncate(run_cli(binary, args, timeout=timeout, extra_env=extra_env))
        except ToolError as exc:
            failures.append(f"{label}: {exc}")
    if not failures:
        raise ToolError(f"No backend is installed. {setup_hint}")
    raise ToolError("All backends failed.\n" + "\n".join(failures) + f"\n{setup_hint}")


# --------------------------------------------------------------------------- #
# Web, search, RSS
# --------------------------------------------------------------------------- #


def read_web(args: Mapping[str, Any], config: Any) -> str:
    from agent_reach.channels.web import WebChannel

    raw = _text(args, "url")
    try:
        return _truncate(WebChannel().read(raw))
    except ValueError as exc:
        raise ToolError(str(exc)) from None


def search_web(args: Mapping[str, Any], config: Any) -> str:
    query = _text(args, "query")
    count = _int(args, "num_results", 5, 1, 20)
    return run_first(
        [("Exa", "mcporter", ["call", "exa.web_search_exa", f"query={query}", f"numResults={count}"], None)],
        setup_hint="Install Node.js, then: npm install -g mcporter && "
        "mcporter config add exa https://mcp.exa.ai/mcp --scope home",
    )


class _PublicRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        normalize_public_http_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_rss(args: Mapping[str, Any], config: Any) -> str:
    import feedparser

    raw = _text(args, "url")
    limit = _int(args, "limit", 10, 1, 50)
    try:
        url = normalize_public_http_url(raw)
        opener = urllib.request.build_opener(_PublicRedirectHandler())
        request = urllib.request.Request(url, headers={"User-Agent": "agent-reach/1.0"})
        with opener.open(request, timeout=30) as resp:
            body = resp.read(_MAX_FEED_BYTES + 1)
    except ValueError:
        raise ToolError("'url' must be a public http(s) URL") from None
    except OSError as exc:
        raise ToolError(f"could not fetch feed: {scrub_url_credentials(exc)}") from None
    if len(body) > _MAX_FEED_BYTES:
        raise ToolError("feed is larger than 5 MB")
    feed = feedparser.parse(body)
    if not feed.entries:
        raise ToolError("no entries found; is this an RSS/Atom feed URL?")
    lines = [f"# {feed.feed.get('title', url)}", ""]
    for entry in feed.entries[:limit]:
        lines.append(f"## {entry.get('title', '(untitled)')}")
        if entry.get("link"):
            lines.append(entry["link"])
        if entry.get("published"):
            lines.append(entry["published"])
        summary = re.sub(r"<[^>]+>", "", entry.get("summary", "")).strip()
        if summary:
            lines.append(summary[:1000])
        lines.append("")
    return _truncate("\n".join(lines))


# --------------------------------------------------------------------------- #
# YouTube
# --------------------------------------------------------------------------- #

_YOUTUBE_HINT = (
    "Run `agent-reach doctor` and follow the YouTube line (yt-dlp needs a JS "
    "runtime such as Node.js configured)."
)


def vtt_to_text(vtt: str) -> str:
    """Strip WebVTT timing and markup; drop the rolling duplicates auto-captions repeat."""
    lines: List[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if (
            not line
            or line == "WEBVTT"
            or "-->" in line
            or line.startswith(("Kind:", "Language:", "NOTE", "STYLE"))
            or line.isdigit()
        ):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = line.replace("&nbsp;", " ").replace("&amp;", "&").strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines)


def youtube_transcript(args: Mapping[str, Any], config: Any) -> str:
    url = _platform_url(args, "url", "youtube.com", "youtu.be")
    language = _match(
        str(args.get("language") or "en").strip(), r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", "language code"
    )
    if not shutil.which("yt-dlp"):
        raise ToolError(f"yt-dlp is not installed. {_YOUTUBE_HINT}")
    with tempfile.TemporaryDirectory(prefix="agent-reach-yt-") as tmp:
        try:
            run_cli(
                "yt-dlp",
                [
                    "--skip-download",
                    "--write-subs",
                    "--write-auto-subs",
                    "--sub-langs",
                    f"{language}.*,{language}",
                    "--sub-format",
                    "vtt",
                    "--no-playlist",
                    "-o",
                    str(Path(tmp) / "%(id)s.%(ext)s"),
                    "--",
                    url,
                ],
                timeout=180,
            )
        except ToolError as exc:
            # yt-dlp prints nothing on stdout when it only writes files.
            if "returned no content" not in str(exc):
                raise ToolError(f"{exc}\n{_YOUTUBE_HINT}") from None
        files = sorted(Path(tmp).glob("*.vtt"), key=lambda p: (".orig" in p.name, p.name))
        if not files:
            raise ToolError(
                f"No '{language}' subtitles found for this video. Try another language code, "
                "or use `agent-reach transcribe URL` for Whisper transcription."
            )
        text = vtt_to_text(files[0].read_text(encoding="utf-8", errors="replace"))
    if not text:
        raise ToolError("subtitle file was empty")
    return _truncate(f"Source: {url}\nSubtitle track: {files[0].name}\n\n{text}")


def youtube_search(args: Mapping[str, Any], config: Any) -> str:
    query = _text(args, "query")
    count = _int(args, "max_results", 5, 1, 20)
    output = run_first(
        [("yt-dlp", "yt-dlp", ["--flat-playlist", "--dump-json", "--", f"ytsearch{count}:{query}"], None)],
        setup_hint=_YOUTUBE_HINT,
        timeout=120,
    )
    results = []
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        results.append(
            {
                "title": item.get("title"),
                "url": item.get("url") or item.get("webpage_url"),
                "channel": item.get("channel") or item.get("uploader"),
                "duration_seconds": item.get("duration"),
                "views": item.get("view_count"),
            }
        )
    if not results:
        raise ToolError("yt-dlp returned no search results")
    return json.dumps(results, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# Reddit (OpenCLI ▸ rdt-cli, both login-backed)
# --------------------------------------------------------------------------- #

_REDDIT_HINT = (
    "Reddit has no anonymous access. Set up OpenCLI (Chrome logged in to reddit.com) "
    "or rdt-cli (`rdt login`), then check with `agent-reach doctor`."
)


def _reddit_attempts(config: Any, opencli: List[str], rdt: List[str]) -> List[Attempt]:
    from agent_reach.channels.reddit import RedditChannel

    table: Dict[str, Attempt] = {
        "OpenCLI": ("OpenCLI", "opencli", ["reddit", *opencli, "-f", "yaml"], None),
        "rdt-cli": ("rdt-cli", "rdt", rdt, None),
    }
    return [table[name] for name in RedditChannel().ordered_backends(config) if name in table]


def reddit_post_id(value: str) -> str:
    match = re.search(r"/comments/([a-z0-9]{3,12})(?:/|$)", value)
    if match and host_matches(normalize_public_http_url(value), "reddit.com", "redd.it"):
        return match.group(1)
    short = re.fullmatch(r"https?://redd\.it/([a-z0-9]{3,12})/?", value)
    if short:
        return short.group(1)
    bare = value.removeprefix("t3_")
    return _match(bare, r"[a-z0-9]{3,12}", "Reddit post URL or ID")


def reddit_search(args: Mapping[str, Any], config: Any) -> str:
    query = _text(args, "query")
    limit = _int(args, "limit", 10, 1, 50)
    return run_first(
        _reddit_attempts(config, ["search", query], ["search", query, "--limit", str(limit)]),
        setup_hint=_REDDIT_HINT,
    )


def reddit_read(args: Mapping[str, Any], config: Any) -> str:
    try:
        post_id = reddit_post_id(_text(args, "post"))
    except ValueError:
        raise ToolError("'post' must be a Reddit post URL or ID") from None
    return run_first(
        _reddit_attempts(config, ["read", post_id], ["read", post_id]),
        setup_hint=_REDDIT_HINT,
    )


def reddit_subreddit(args: Mapping[str, Any], config: Any) -> str:
    name = _text(args, "subreddit").removeprefix("r/").removeprefix("/r/")
    name = _match(name, r"[A-Za-z0-9_]{2,21}", "subreddit name")
    limit = _int(args, "limit", 20, 1, 100)
    return run_first(
        _reddit_attempts(config, ["subreddit", name], ["sub", name, "--limit", str(limit)]),
        setup_hint=_REDDIT_HINT,
    )


# --------------------------------------------------------------------------- #
# Twitter/X (twitter-cli ▸ OpenCLI)
# --------------------------------------------------------------------------- #

_TWITTER_HINT = (
    "Install twitter-cli and save cookies with `agent-reach configure twitter-cookies` "
    "(Cookie-Editor export), or use OpenCLI with Chrome logged in to x.com."
)


def _twitter_env(config: Any) -> Dict[str, str]:
    from agent_reach.channels.twitter import twitter_cli_child_env

    return twitter_cli_child_env(config)


def twitter_search(args: Mapping[str, Any], config: Any) -> str:
    query = _text(args, "query")
    limit = _int(args, "limit", 10, 1, 50)
    return run_first(
        [
            ("twitter-cli", "twitter", ["search", query, "-n", str(limit)], _twitter_env(config)),
            ("OpenCLI", "opencli", ["twitter", "search", query, "-f", "yaml"], None),
        ],
        setup_hint=_TWITTER_HINT,
    )


def twitter_read(args: Mapping[str, Any], config: Any) -> str:
    raw = _text(args, "tweet")
    if raw.isdigit():
        target = raw
    else:
        target = _platform_url(args, "tweet", "x.com", "twitter.com")
    return run_first(
        [("twitter-cli", "twitter", ["tweet", target], _twitter_env(config))],
        setup_hint=_TWITTER_HINT,
    )


def twitter_user_posts(args: Mapping[str, Any], config: Any) -> str:
    name = _match(_text(args, "username").lstrip("@"), r"[A-Za-z0-9_]{1,15}", "X username")
    limit = _int(args, "limit", 20, 1, 100)
    return run_first(
        [
            ("twitter-cli", "twitter", ["user-posts", f"@{name}", "-n", str(limit)], _twitter_env(config)),
            ("OpenCLI", "opencli", ["twitter", "user-posts", name, "-f", "yaml"], None),
        ],
        setup_hint=_TWITTER_HINT,
    )


# --------------------------------------------------------------------------- #
# GitHub (gh CLI)
# --------------------------------------------------------------------------- #

_GH_HINT = "Install the GitHub CLI (https://cli.github.com) and run `gh auth login`."
_REPO_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}"


def _repo(args: Mapping[str, Any]) -> str:
    value = _text(args, "repo")
    value = re.sub(r"^https?://github\.com/", "", value).removesuffix(".git").strip("/")
    return _match(value, _REPO_PATTERN, "owner/repo")


def github_search_repos(args: Mapping[str, Any], config: Any) -> str:
    query = _text(args, "query")
    limit = _int(args, "limit", 10, 1, 50)
    return run_first(
        [
            (
                "gh",
                "gh",
                [
                    "search",
                    "repos",
                    query,
                    "--limit",
                    str(limit),
                    "--json",
                    "fullName,description,stargazersCount,url,updatedAt",
                ],
                None,
            )
        ],
        setup_hint=_GH_HINT,
    )


def github_read_repo(args: Mapping[str, Any], config: Any) -> str:
    return run_first([("gh", "gh", ["repo", "view", _repo(args)], None)], setup_hint=_GH_HINT)


def github_read_issue(args: Mapping[str, Any], config: Any) -> str:
    repo = _repo(args)
    number = _int(args, "number", 0, 0, 10**9)
    if number < 1:
        raise ToolError("'number' must be a positive integer")
    kind = str(args.get("kind") or "issue")
    if kind not in {"issue", "pr"}:
        raise ToolError("'kind' must be 'issue' or 'pr'")
    return run_first(
        [("gh", "gh", [kind, "view", str(number), "-R", repo, "--comments"], None)],
        setup_hint=_GH_HINT,
    )


# --------------------------------------------------------------------------- #
# Bilibili, V2EX, XiaoHongShu, Instagram, Facebook
# --------------------------------------------------------------------------- #

_BILI_HINT = "Install bili-cli (`agent-reach install --channels=bilibili`)."
_OPENCLI_HINT = (
    "This platform needs OpenCLI: Chrome open, the OpenCLI extension enabled, "
    "and you already logged in to the site. Check with `agent-reach doctor`."
)


def _bvid(args: Mapping[str, Any]) -> str:
    raw = _text(args, "video")
    match = re.search(r"(BV[0-9A-Za-z]{10})", raw)
    if not match:
        raise ToolError("'video' must be a Bilibili BV id or video URL")
    return match.group(1)


def bilibili_search(args: Mapping[str, Any], config: Any) -> str:
    query = _text(args, "query")
    limit = _int(args, "limit", 5, 1, 30)
    return run_first(
        [("bili-cli", "bili", ["search", query, "--type", "video", "-n", str(limit)], None)],
        setup_hint=_BILI_HINT,
    )


def bilibili_video(args: Mapping[str, Any], config: Any) -> str:
    bvid = _bvid(args)
    parts = [run_first([("bili-cli", "bili", ["video", bvid], None)], setup_hint=_BILI_HINT)]
    if args.get("subtitles"):
        parts.append(
            run_first(
                [("OpenCLI", "opencli", ["bilibili", "subtitle", bvid], None)],
                setup_hint=_OPENCLI_HINT,
            )
        )
    return "\n\n".join(parts)


def v2ex_hot(args: Mapping[str, Any], config: Any) -> str:
    from agent_reach.channels.v2ex import V2EXChannel

    limit = _int(args, "limit", 20, 1, 50)
    try:
        topics = V2EXChannel().get_hot_topics(limit=limit)
    except Exception as exc:
        raise ToolError(f"V2EX request failed: {scrub_url_credentials(exc)}") from None
    return json.dumps(topics, ensure_ascii=False, indent=2)


def v2ex_topic(args: Mapping[str, Any], config: Any) -> str:
    from agent_reach.channels.v2ex import V2EXChannel

    raw = str(args.get("topic", "")).strip()
    match = re.search(r"(?:/t/)?(\d{1,10})(?:\D|$)", raw)
    if not match:
        raise ToolError("'topic' must be a V2EX topic URL or numeric ID")
    try:
        topic = V2EXChannel().get_topic(int(match.group(1)))
    except Exception as exc:
        raise ToolError(f"V2EX request failed: {scrub_url_credentials(exc)}") from None
    return _truncate(json.dumps(topic, ensure_ascii=False, indent=2))


def xiaohongshu_search(args: Mapping[str, Any], config: Any) -> str:
    query = _text(args, "query")
    return run_first(
        [
            ("OpenCLI", "opencli", ["xiaohongshu", "search", query, "-f", "yaml"], None),
            (
                "xiaohongshu-mcp",
                "mcporter",
                ["call", "xiaohongshu.search_feeds", f"keyword={query}", "--timeout", "120000"],
                None,
            ),
        ],
        setup_hint=_OPENCLI_HINT,
        timeout=150,
    )


def xiaohongshu_read(args: Mapping[str, Any], config: Any) -> str:
    url = _platform_url(args, "url", "xiaohongshu.com", "xhslink.com")
    return run_first(
        [("OpenCLI", "opencli", ["xiaohongshu", "note", url, "-f", "yaml"], None)],
        setup_hint=_OPENCLI_HINT + " Use the full note URL from search results (it carries xsec_token).",
    )


def instagram_user(args: Mapping[str, Any], config: Any) -> str:
    name = _match(_text(args, "username").lstrip("@"), r"[A-Za-z0-9._]{1,30}", "Instagram username")
    return run_first(
        [("OpenCLI", "opencli", ["instagram", "user", name, "-f", "yaml"], None)],
        setup_hint=_OPENCLI_HINT,
    )


def facebook_search(args: Mapping[str, Any], config: Any) -> str:
    query = _text(args, "query")
    return run_first(
        [("OpenCLI", "opencli", ["facebook", "search", query, "-f", "yaml"], None)],
        setup_hint=_OPENCLI_HINT,
    )


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: Dict[str, Dict[str, Any]]
    required: Tuple[str, ...]
    handler: Callable[[Mapping[str, Any], Any], str]

    @property
    def input_schema(self) -> Dict[str, Any]:
        return {"type": "object", "properties": self.properties, "required": list(self.required)}


def _s(description: str) -> Dict[str, Any]:
    return {"type": "string", "description": description}


def _n(description: str) -> Dict[str, Any]:
    return {"type": "integer", "description": description}


TOOLS: Tuple[ToolSpec, ...] = (
    ToolSpec("read_web", "Read any public web page as Markdown (via Jina Reader).",
             {"url": _s("Page URL")}, ("url",), read_web),
    ToolSpec("search_web", "Search the web with Exa. Needs mcporter + Exa configured.",
             {"query": _s("Search query"), "num_results": _n("1-20, default 5")}, ("query",), search_web),
    ToolSpec("read_rss", "Read the latest entries of an RSS/Atom feed.",
             {"url": _s("Feed URL"), "limit": _n("1-50, default 10")}, ("url",), read_rss),
    ToolSpec("youtube_transcript", "Get a YouTube video's subtitles as plain text (manual or auto captions).",
             {"url": _s("YouTube video URL"), "language": _s("Language code, default 'en'")},
             ("url",), youtube_transcript),
    ToolSpec("youtube_search", "Search YouTube videos; returns title, URL, channel, duration.",
             {"query": _s("Search query"), "max_results": _n("1-20, default 5")}, ("query",), youtube_search),
    ToolSpec("reddit_search", "Search Reddit posts. Needs OpenCLI or rdt-cli logged in.",
             {"query": _s("Search query"), "limit": _n("1-50, default 10")}, ("query",), reddit_search),
    ToolSpec("reddit_read", "Read a Reddit post with its comments.",
             {"post": _s("Post URL or ID")}, ("post",), reddit_read),
    ToolSpec("reddit_subreddit", "List recent posts in a subreddit.",
             {"subreddit": _s("Name, e.g. LocalLLaMA"), "limit": _n("1-100, default 20")},
             ("subreddit",), reddit_subreddit),
    ToolSpec("twitter_search", "Search tweets on X. Needs twitter-cli cookies or OpenCLI.",
             {"query": _s("Search query"), "limit": _n("1-50, default 10")}, ("query",), twitter_search),
    ToolSpec("twitter_read", "Read one tweet with replies.",
             {"tweet": _s("Tweet URL or numeric ID")}, ("tweet",), twitter_read),
    ToolSpec("twitter_user_posts", "Recent posts from one X account.",
             {"username": _s("Handle, with or without @"), "limit": _n("1-100, default 20")},
             ("username",), twitter_user_posts),
    ToolSpec("github_search_repos", "Search GitHub repositories. Needs gh CLI logged in.",
             {"query": _s("Search query"), "limit": _n("1-50, default 10")}, ("query",), github_search_repos),
    ToolSpec("github_read_repo", "Show a GitHub repository's description and README.",
             {"repo": _s("owner/repo or GitHub URL")}, ("repo",), github_read_repo),
    ToolSpec("github_read_issue", "Read a GitHub issue or pull request with comments.",
             {"repo": _s("owner/repo"), "number": _n("Issue or PR number"),
              "kind": {"type": "string", "enum": ["issue", "pr"], "description": "Default 'issue'"}},
             ("repo", "number"), github_read_issue),
    ToolSpec("bilibili_search", "Search Bilibili videos (bili-cli, no login).",
             {"query": _s("Search query"), "limit": _n("1-30, default 5")}, ("query",), bilibili_search),
    ToolSpec("bilibili_video", "Bilibili video details; optionally subtitles via OpenCLI.",
             {"video": _s("BV id or video URL"), "subtitles": {"type": "boolean", "description": "Also fetch subtitles"}},
             ("video",), bilibili_video),
    ToolSpec("v2ex_hot", "V2EX hot topics (public API).",
             {"limit": _n("1-50, default 20")}, (), v2ex_hot),
    ToolSpec("v2ex_topic", "Read a V2EX topic with replies.",
             {"topic": _s("Topic URL or ID")}, ("topic",), v2ex_topic),
    ToolSpec("xiaohongshu_search", "Search XiaoHongShu notes (OpenCLI or xiaohongshu-mcp).",
             {"query": _s("Search query")}, ("query",), xiaohongshu_search),
    ToolSpec("xiaohongshu_read", "Read a XiaoHongShu note. Use the full URL from search results.",
             {"url": _s("Note URL including xsec_token")}, ("url",), xiaohongshu_read),
    ToolSpec("instagram_user", "Recent posts from an Instagram account (OpenCLI).",
             {"username": _s("Instagram username")}, ("username",), instagram_user),
    ToolSpec("facebook_search", "Search Facebook (OpenCLI).",
             {"query": _s("Search query")}, ("query",), facebook_search),
)

TOOLS_BY_NAME: Dict[str, ToolSpec] = {tool.name: tool for tool in TOOLS}


def call(name: str, arguments: Optional[Mapping[str, Any]], config: Any) -> str:
    """Dispatch one tool call; errors come back as readable text."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise ToolError(f"Unknown tool: {name}")
    return tool.handler(arguments or {}, config)


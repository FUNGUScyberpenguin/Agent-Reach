"""Read-only MCP platform tools: argument safety and upstream routing."""

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_reach.integrations.mcp_server as mcp_server
from agent_reach.integrations import mcp_tools
from agent_reach.integrations.mcp_tools import ToolError


class _Recorder:
    """Fake subprocess.run that records argv and returns canned output."""

    def __init__(self, outputs=None):
        self.calls = []
        self.outputs = outputs or {}

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        name = Path(argv[0]).name
        stdout, returncode = self.outputs.get(name, ("ok output", 0))
        if callable(stdout):
            stdout = stdout(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout, "boom" if returncode else "")


@pytest.fixture
def fake_run(monkeypatch):
    def install(installed=("yt-dlp", "rdt", "opencli", "twitter", "gh", "bili", "mcporter"), outputs=None):
        recorder = _Recorder(outputs)
        monkeypatch.setattr(
            mcp_tools.shutil,
            "which",
            lambda name: f"/usr/bin/{name}" if name in installed else None,
        )
        monkeypatch.setattr(mcp_tools.subprocess, "run", recorder)
        return recorder

    return install


class _Config:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_tool_registry_is_well_formed():
    names = [tool.name for tool in mcp_tools.TOOLS]
    assert len(names) == len(set(names))
    for tool in mcp_tools.TOOLS:
        schema = tool.input_schema
        assert schema["type"] == "object"
        assert set(tool.required) <= set(schema["properties"])


@pytest.mark.parametrize("value", ["--exec=calc", "-o/tmp/x", "a\nb", "x" * 501, "   "])
def test_text_arguments_cannot_become_options_or_smuggle_lines(value):
    with pytest.raises(ToolError):
        mcp_tools._text({"query": value}, "query")


def test_run_cli_uses_argv_without_shell(fake_run):
    recorder = fake_run()
    mcp_tools.run_cli("rdt", ["search", "a; rm -rf ~"])
    argv, kwargs = recorder.calls[0]
    assert argv == ["/usr/bin/rdt", "search", "a; rm -rf ~"]
    assert not kwargs.get("shell")
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_windows_batch_shims_refuse_cmd_metacharacters(monkeypatch):
    monkeypatch.setattr(mcp_tools.shutil, "which", lambda name: r"C:\npm\opencli.cmd")
    monkeypatch.setattr(mcp_tools.subprocess, "run", lambda *a, **k: pytest.fail("must not run"))
    with pytest.raises(ToolError, match="cmd.exe"):
        mcp_tools.run_cli("opencli", ["reddit", "search", 'x" & calc & "'])


def test_run_first_falls_back_to_next_backend(fake_run):
    recorder = fake_run(outputs={"opencli": ("", 1), "rdt": ("post body", 0)})
    result = mcp_tools.call("reddit_read", {"post": "https://www.reddit.com/r/python/comments/abc123/title/"}, _Config())
    assert result == "post body"
    assert recorder.calls[0][0][1:4] == ["reddit", "read", "abc123"]
    assert recorder.calls[1][0][1:] == ["read", "abc123"]


def test_run_first_reports_setup_hint_when_nothing_installed(fake_run):
    fake_run(installed=())
    with pytest.raises(ToolError, match="No backend is installed.*anonymous"):
        mcp_tools.call("reddit_search", {"query": "python"}, _Config())


def test_reddit_backend_override_is_honored(fake_run):
    recorder = fake_run()
    mcp_tools.call("reddit_subreddit", {"subreddit": "r/LocalLLaMA"}, _Config({"reddit_backend": "rdt-cli"}))
    assert recorder.calls[0][0] == ["/usr/bin/rdt", "sub", "LocalLLaMA", "--limit", "20"]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("abc123", "abc123"),
        ("t3_abc123", "abc123"),
        ("https://old.reddit.com/r/x/comments/zz9/y", "zz9"),
        ("https://redd.it/qq11", "qq11"),
    ],
)
def test_reddit_post_id(value, expected):
    assert mcp_tools.reddit_post_id(value) == expected


def test_reddit_post_id_rejects_lookalike_host():
    with pytest.raises(ToolError):
        mcp_tools.reddit_post_id("https://reddit.com.evil.test/comments/abc/")


def test_vtt_to_text_strips_timing_and_rolling_duplicates():
    vtt = (
        "WEBVTT\nKind: captions\nLanguage: en\n\n"
        "00:00:00.000 --> 00:00:02.000 align:start\nhello <c>world</c>\n\n"
        "00:00:02.000 --> 00:00:04.000\nhello world\nsecond line\n"
    )
    assert mcp_tools.vtt_to_text(vtt) == "hello world\nsecond line"


def test_youtube_transcript_reads_written_subtitles(fake_run):
    def write_vtt(argv):
        out = Path(argv[argv.index("-o") + 1]).parent
        (out / "vid.en.vtt").write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nhi there\n", encoding="utf-8")
        return "[youtube] done"

    recorder = fake_run(outputs={"yt-dlp": (write_vtt, 0)})
    result = mcp_tools.call("youtube_transcript", {"url": "https://youtu.be/dQw4w9WgXcQ"}, _Config())
    assert result.endswith("hi there")
    argv = recorder.calls[0][0]
    assert argv[-2:] == ["--", "https://youtu.be/dQw4w9WgXcQ"]
    assert "--skip-download" in argv


@pytest.mark.parametrize("url", ["https://evil.test/watch?v=1", "http://127.0.0.1/youtube.com", "file:///etc/passwd"])
def test_youtube_transcript_rejects_other_hosts(fake_run, url):
    recorder = fake_run()
    with pytest.raises(ToolError):
        mcp_tools.call("youtube_transcript", {"url": url}, _Config())
    assert recorder.calls == []


def test_youtube_transcript_rejects_bad_language(fake_run):
    fake_run()
    with pytest.raises(ToolError, match="language"):
        mcp_tools.call("youtube_transcript", {"url": "https://youtu.be/x", "language": "en,--exec"}, _Config())


def test_youtube_search_summarizes_results(fake_run):
    line = '{"title": "T", "url": "https://youtube.com/watch?v=1", "channel": "C", "duration": 60}'
    recorder = fake_run(outputs={"yt-dlp": (line, 0)})
    result = mcp_tools.call("youtube_search", {"query": "rust async", "max_results": 3}, _Config())
    assert '"channel": "C"' in result
    assert recorder.calls[0][0][-1] == "ytsearch3:rust async"


def test_twitter_passes_saved_cookies_only_to_child(fake_run, monkeypatch):
    monkeypatch.delenv("TWITTER_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWITTER_CT0", raising=False)
    recorder = fake_run()
    config = _Config({"twitter_auth_token": "tok", "twitter_ct0": "ct"})
    mcp_tools.call("twitter_search", {"query": "mcp"}, config)
    env = recorder.calls[0][1]["env"]
    assert env["TWITTER_AUTH_TOKEN"] == "tok"
    assert env["TWITTER_CT0"] == "ct"
    import os

    assert "TWITTER_AUTH_TOKEN" not in os.environ


def test_twitter_read_requires_x_host_or_numeric_id(fake_run):
    fake_run()
    assert mcp_tools.call("twitter_read", {"tweet": "12345"}, _Config()) == "ok output"
    with pytest.raises(ToolError):
        mcp_tools.call("twitter_read", {"tweet": "https://x.com.evil.test/a/status/1"}, _Config())


def test_github_repo_parsing_and_kind(fake_run):
    recorder = fake_run()
    mcp_tools.call("github_read_repo", {"repo": "https://github.com/owner/name.git"}, _Config())
    assert recorder.calls[0][0][1:] == ["repo", "view", "owner/name"]
    mcp_tools.call("github_read_issue", {"repo": "owner/name", "number": 7, "kind": "pr"}, _Config())
    assert recorder.calls[1][0][1:] == ["pr", "view", "7", "-R", "owner/name", "--comments"]
    with pytest.raises(ToolError):
        mcp_tools.call("github_read_issue", {"repo": "owner/name", "number": 7, "kind": "delete"}, _Config())


def test_bilibili_video_extracts_bvid(fake_run):
    recorder = fake_run()
    mcp_tools.call("bilibili_video", {"video": "https://www.bilibili.com/video/BV1xx411c7mD/"}, _Config())
    assert recorder.calls[0][0][1:] == ["video", "BV1xx411c7mD"]


def test_read_rss_rejects_private_urls():
    with pytest.raises(ToolError, match="public"):
        mcp_tools.call("read_rss", {"url": "http://169.254.169.254/latest"}, _Config())


def test_read_rss_parses_feed(monkeypatch):
    feed = b"""<?xml version="1.0"?><rss><channel><title>Blog</title>
    <item><title>Post one</title><link>https://example.com/1</link>
    <description>&lt;p&gt;Body&lt;/p&gt;</description></item></channel></rss>"""

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return feed

    monkeypatch.setattr(
        mcp_tools.urllib.request,
        "build_opener",
        lambda *handlers: SimpleNamespace(open=lambda req, timeout: _Resp()),
    )
    result = mcp_tools.call("read_rss", {"url": "https://example.com/feed"}, _Config())
    assert "# Blog" in result
    assert "## Post one" in result
    assert "Body" in result and "<p>" not in result


def test_rss_redirects_to_private_hosts_are_blocked():
    handler = mcp_tools._PublicRedirectHandler()
    with pytest.raises(ValueError):
        handler.redirect_request(None, None, 302, "", {}, "http://127.0.0.1/admin")


def test_unknown_tool_is_an_error():
    with pytest.raises(ToolError, match="Unknown tool"):
        mcp_tools.call("delete_everything", {}, _Config())


def test_server_lists_and_dispatches_platform_tools(monkeypatch, fake_run):
    from tests.test_mcp_server import _install_fake_mcp

    _install_fake_mcp(monkeypatch)
    monkeypatch.setattr(mcp_server, "Config", lambda read_only=False: _Config())
    fake_run(outputs={"rdt": ("results", 0)}, installed=("rdt",))
    server = mcp_server.create_server()

    listed = {tool.name for tool in asyncio.run(server.list_tools_handler())}
    assert {"get_status", "youtube_transcript", "reddit_read"} <= listed

    result = asyncio.run(server.call_tool_handler("reddit_search", {"query": "python"}))
    assert result[0].text == "results"

    error = asyncio.run(server.call_tool_handler("reddit_search", {"query": "--help"}))
    assert error[0].text.startswith("Error:")


def test_server_supports_mcp2_constructor_handlers(monkeypatch, fake_run):
    class _Mcp2Server:
        def __init__(self, name, *, on_list_tools, on_call_tool):
            self.on_list_tools = on_list_tools
            self.on_call_tool = on_call_tool

    monkeypatch.setattr(mcp_server, "HAS_MCP", True)
    monkeypatch.setattr(mcp_server, "Server", _Mcp2Server, raising=False)
    monkeypatch.setattr(mcp_server, "Tool", lambda **kw: SimpleNamespace(**kw), raising=False)
    monkeypatch.setattr(mcp_server, "TextContent", lambda **kw: SimpleNamespace(**kw), raising=False)
    monkeypatch.setattr(mcp_server, "ListToolsResult", lambda **kw: SimpleNamespace(**kw), raising=False)
    monkeypatch.setattr(mcp_server, "CallToolResult", lambda **kw: SimpleNamespace(**kw), raising=False)
    monkeypatch.setattr(mcp_server, "Config", lambda read_only=False: _Config())
    fake_run(outputs={"gh": ("readme", 0)}, installed=("gh",))
    server = mcp_server.create_server()

    listed = asyncio.run(server.on_list_tools(None, None))
    assert "github_read_repo" in {tool.name for tool in listed.tools}

    params = SimpleNamespace(name="github_read_repo", arguments={"repo": "a/b"})
    result = asyncio.run(server.on_call_tool(None, params))
    assert result.content[0].text == "readme"

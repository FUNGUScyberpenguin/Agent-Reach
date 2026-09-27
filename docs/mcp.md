# Using Agent Reach from chat (MCP server)

Agents that can run shell commands (Claude Code, Cursor, OpenClaw) should use the
skill and call upstream tools directly. Chat apps such as Claude Desktop can't
run commands, so Agent Reach also ships an MCP server. Each MCP tool runs one
documented upstream command and returns the result to the chat.

The server only reads. It never posts, logs in for you, or reads browser cookies.

## Install

```bash
pip install "agent-reach[mcp] @ https://github.com/Panniantong/agent-reach/archive/main.zip"
agent-reach install --env=auto
agent-reach doctor
```

`doctor` shows which platforms are ready. A tool whose upstream CLI is missing
returns an error that says what to install.

## Connect Claude Desktop

Open Claude Desktop, go to **Settings > Developer > Edit Config**, and add:

```json
{
  "mcpServers": {
    "agent-reach": {
      "command": "python",
      "args": ["-m", "agent_reach.integrations.mcp_server"]
    }
  }
}
```

Quit Claude Desktop completely and reopen it. If the server fails to start,
replace `python` with the full path to the Python that has Agent Reach
installed. On Windows, `(Get-Command python).Source` in PowerShell prints it.

The same block works in any MCP client that launches stdio servers.

## Tools

| Tool | Upstream command | Setup needed |
|------|------------------|--------------|
| `get_status` | `agent-reach doctor` | none |
| `read_web` | Jina Reader | none |
| `read_rss` | feedparser | none |
| `search_web` | `mcporter call exa.web_search_exa` | Node.js, mcporter, Exa config |
| `youtube_transcript`, `youtube_search` | `yt-dlp` | yt-dlp with a JS runtime (see doctor) |
| `reddit_search`, `reddit_read`, `reddit_subreddit` | `opencli reddit` or `rdt` | login required, no anonymous path |
| `twitter_search`, `twitter_read`, `twitter_user_posts` | `twitter` or `opencli twitter` | Cookie-Editor export via `agent-reach configure twitter-cookies`, or OpenCLI |
| `github_search_repos`, `github_read_repo`, `github_read_issue` | `gh` | `gh auth login` |
| `bilibili_search`, `bilibili_video` | `bili`, `opencli bilibili subtitle` | bili-cli; OpenCLI for subtitles |
| `v2ex_hot`, `v2ex_topic` | V2EX public API | none |
| `xiaohongshu_search`, `xiaohongshu_read` | `opencli xiaohongshu` or xiaohongshu-mcp | existing logged-in Chrome session, or Cookie-Editor export |
| `instagram_user`, `facebook_search` | `opencli` | Chrome with the OpenCLI extension, already logged in |

Multi-backend tools try backends in the order `doctor` uses. Reddit honors the
`reddit_backend` config override.

LinkedIn, Xueqiu, Boss Zhipin and Xiaoyuzhou are not exposed yet. Use them
through an agent that can run commands.

## Safety

Tool arguments come from the model, and the model may have read hostile web
pages. So the server:

- runs commands as argument lists, never through a shell;
- refuses free-text values that start with `-`, so they can't become options;
- accepts only public http(s) URLs, and only the platform's own host for
  platform tools;
- on Windows, refuses cmd.exe metacharacters when the upstream tool is an npm
  `.cmd` shim;
- caps output at 100,000 characters.

Logged-in tools act as your account. Use a throwaway account for Twitter/X and
XiaoHongShu, as the main README advises.

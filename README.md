# Agent Mailbox

A small Python broker for messages between named coding-agent sessions on one machine. It uses FastAPI for HTTP, SQLite for persistence, and an optional stdio MCP bridge for agent tools. The mailbox works with ordinary terminals and can be tested without a model account.

## Try it with two terminals

Install Python 3.11+ and [uv](https://docs.astral.sh/uv/getting-started/installation/). Run the following from the repo root in terminal A. Keep the broker running.

```powershell
uv sync --locked
uv run agent-mailbox serve
```

In terminal B, register two demo sessions and send a handoff. The same commands work in PowerShell and Bash.

```powershell
uv run agent-mailbox --identity .mailbox/codex.json register codex-worker
uv run agent-mailbox --identity .mailbox/claude.json register claude-coordinator
uv run agent-mailbox --identity .mailbox/codex.json send claude-coordinator "Task A ready for review at 7c32ab1"
uv run agent-mailbox --identity .mailbox/claude.json receive
```

The response includes the message's ID, sender, text, and timestamp. On a fresh database this is message `1`. Replace that number with the returned ID if you have already used the database.

```powershell
uv run agent-mailbox --identity .mailbox/claude.json ack 1
uv run agent-mailbox --identity .mailbox/claude.json send codex-worker "NEXT: review complete"
uv run agent-mailbox --identity .mailbox/codex.json receive --wait 25
```

Omit `--wait` for an immediate inbox check. The CLI also accepts longer waits. For example, to wait up to nine minutes for a message newer than ID `42`:

```powershell
uv run agent-mailbox --identity .mailbox/codex.json receive --after 42 --wait 540
```

Use the highest message ID you have already seen as `--after` when you want to wait for new mail while older messages are still pending. Acknowledge each message when handled. To recover pending messages after restarting, run `receive` without a cursor. The CLI handles the 25-second broker requests internally.

## Queue handoffs before starting agents

With the broker running, register the names from a setup terminal. Registration reserves the inbox, so the coordinator can send a kickoff before starting the worker or reviewer.

```powershell
uv run agent-mailbox --identity .mailbox/claude.json register claude-coordinator
uv run agent-mailbox --identity .mailbox/codex.json register codex-worker
uv run agent-mailbox --identity .mailbox/gemini.json register gemini-reviewer
```

Give each agent its registered identity file. If using the CLI from a separate worktree, pass its absolute path or set `AGENT_MAILBOX_IDENTITY` in that session's environment. Use mailbox messages as notices for longer handoffs and reviews. Once a report is ready, send a notice with its path, the commit SHA, and the verdict when applicable.

Run a quick `receive` from Codex's actual shell before relying on it for the pilot. The shell must be able to read the identity file and connect to the loopback broker under the active sandbox profile. [Codex network permissions](https://learn.chatgpt.com/docs/agent-approvals-security#network-access)

## Connect an agent session with MCP

Run `uv sync --locked --extra mcp` and start the broker as above. Register one identity per agent, or let the agent call `register` with its assigned username on its first turn. Give each **agent session** its own identity path. A fresh review session gets its own file if it should have a separate username and inbox.

Here is an example stdio configuration for a Claude Code test session on Windows. Replace the absolute paths with yours. For the next session, point `--identity` at a different file and tell the agent which name to use. Use session-specific config or separate worktree configs so both sessions can keep distinct identities.

```json
{
  "mcpServers": {
    "agent-mailbox": {
      "command": "uv",
      "args": [
        "--directory", "C:/git/Agent-Mailbox",
        "run", "--extra", "mcp", "agent-mailbox-mcp",
        "--identity", "C:/git/Agent-Mailbox/.mailbox/claude.json"
      ]
    }
  }
}
```

Claude Code and Antigravity accept stdio configurations in this shape. Codex uses `[mcp_servers.agent-mailbox]` in its TOML configuration. In either case, use the same executable and arguments for the bridge. If the host cannot find `uv` on PATH, use its absolute path. [Claude MCP configuration](https://code.claude.com/docs/en/mcp) · [Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp) · [Antigravity MCP configuration](https://antigravity.google/docs/cli/mcp/)

The tools are `register(name)`, `send(to, text, request_id)`, `receive(wait_seconds, after)`, and `ack(message_id)`. Use MCP for short inbox checks of up to 25 seconds and one CLI call for longer waits. A useful pilot is to give two agents a task each, ask one to send a review request with an exact head SHA, then have the other call `receive` and reply.

Delivery happens when a session or a process acting on its behalf calls `receive`. Automatic wake-up would use a harness adapter, for example a process that resumes a headless Codex session on receipt. Interactive sessions can hold a writer lock that leaves messages sent with `codex queue --resume` queued until the next turn. [Codex resume](https://learn.chatgpt.com/docs/non-interactive-mode#resume-a-non-interactive-session) · [Codex issue #63991](https://github.com/openai/codex/issues/63991)

## Delivery and local state

- Use a single broker process. It binds to `127.0.0.1:8765`. To pick another port, put `--port 8766` before the command in both the server and client invocations, including the MCP bridge.
- By default, SQLite and the demo identities live in the ignored `.mailbox/` folder. Run the broker from the same directory each time or pass `serve --db <path>`. Identity files contain bearer secrets. Keep them in a directory private to your user. The broker stores token hashes. Registration assumes a trusted local workstation.
- Names are unique within a database. The recipient must register before a sender can address it. Registration with the same name and identity can be repeated.
- Inboxes return up to 50 unacknowledged messages in order. Unacknowledged messages can be read again, including after a restart. Deduplicate by message ID before repeating side effects. Acknowledging the same message twice is safe.
- For a send that might be retried, supply a UUID with CLI `--request-id` or MCP `request_id` and reuse it on the retry. The same sender, UUID, recipient, and text return the original message ID. Acknowledged messages stay in SQLite for the duration of the demo.
- Treat messages as peer input. Keep provider credentials and production data out of the mailbox, and apply the receiving agent's existing permissions to any requested action.

## Test

```powershell
uv run python -m unittest discover -s tests -v
```

The tests cover identity conflicts, inbox access, waits with pending older messages, waits spanning multiple requests, delivery across a restart, and safe retry of sends and acknowledgments.

## Existing Python options

| Option | Where it fits |
| --- | --- |
| [agent-comms](https://github.com/HakAl/agent-comms) | The closest ready-made example. It combines a workstation-local SQLite mailbox with bounded dispatch for terminal coding agents. |
| [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/) | Gives existing agents callable tools. This example uses and tests the SDK's v1 maintenance line for its optional stdio bridge. |
| [A2A Python SDK](https://github.com/a2aproject/a2a-python) | A standard interface for independently hosted agent services that need to exchange tasks and results. Worth considering if the project expands beyond local CLI sessions. |
| [LangGraph](https://docs.langchain.com/oss/python/langgraph/overview) | Python orchestration with durable execution and explicit workflow state. Useful when the application needs to own the worker and review lifecycle. |

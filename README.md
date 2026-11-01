# lain

**A memory for Claude Code, grounded in what actually happened.**

Claude Code can read your code, but a new session doesn't know what earlier sessions discovered, why something was built a certain way, or which approaches have already failed.

lain keeps that history around.

It reads completed Claude Code sessions and can work through git history and pull request reviews. Models extract decisions and observations from those records, then lain brings the relevant ones into later sessions as they reach the code they're about.

For example, when a session opens a file, it might see:

```text
- decision 42 (stated, sessions/2026-10-29-051203/17): Store pages as Markdown so any text round-trips exactly
- observation 58 (user, user/3): Don't edit generated files under build/ [may be out of date]
```

These are leads, not instructions. Each says where it came from and how far to trust it. The agent can follow the reference to the original evidence instead of taking lain's word for it.

lain also:

- **Maps the codebase**, giving sessions an overview of the part they're working in.
- **Watches for conflicting edits**, warning a session when a file has changed since it saw it, or another session recently worked on it.
- **Connects sessions**, letting them leave letters for each other.
- **Shows what it's doing**, with a live local page for sessions, jobs and individual model calls.

Everything lain knows about a project is stored as Markdown in `.lain/`, where you can read and search it yourself.

## How much can you trust it?

Memories aren't all equally reliable. lain marks each one with its basis:

| Basis | What it means |
| --- | --- |
| `user` | You said it. |
| `verified` | A successful tool result supports it. |
| `stated` | Someone said it, but the recorded evidence doesn't establish it. |
| `inferred` | The recorded evidence doesn't directly show it. |

The extracting model has to cite the steps behind each memory. lain checks that those citations support what's claimed, then has a second model check them too.

Memories can also go out of date. lain notices when referenced files or names disappear, or when a condition attached to a memory is met. Its `recheck` job can compare those memories against the current code and correct or retract them.

None of this guarantees that a memory is right. You can inspect its source, correct it, forget it or pin it yourself. Sessions can propose memories, but they can't make those final changes for you.

## Before you install

**lain sends your work to OpenRouter.** After a stretch of work, it sends the session record, including your prompts, Claude's replies, and the commands and file contents returned by tools. Nothing is redacted before sending. OpenRouter can route requests to different providers. If your code or sessions can't leave your machine, don't install lain as it is.

**lain spends money automatically.** It makes model calls after work finishes and periodically rechecks memories. There is no total spending cap. Reading a long git history can be particularly expensive. Check your OpenRouter limits first.

**lain changes your Claude Code setup.** It installs hooks and an MCP server, which give sessions context and let them look things up. `lain uninstall` removes those integrations.

**lain keeps sensitive data locally too.** Session records, tool outputs and file snapshots are stored without redaction. Its local HTTP server listens on `127.0.0.1` and checks browser request headers, but has no authentication against other programs or users on the same machine. Uninstalling doesn't delete the stored data.

This is the first public release. Expect rough edges, especially around Claude Code's changing transcript format and lain's own storage layout.

## Install

You'll need:

- Linux or macOS, with Python 3.12 or newer.
- Claude Code and `git` on your `PATH`.
- A git checkout for lain to follow.
- An [OpenRouter](https://openrouter.ai) API key.

lain uses the Python standard library; there are no Python packages to install.

Clone this repository, then from its directory:

```sh
mkdir -p ~/.local/bin
ln -s "$PWD/bin/lain" ~/.local/bin/lain
```

Put your OpenRouter key in `src/keys.py`:

```python
API_KEY = ""
OPENROUTER_KEY = "sk-or-..."
```

Keys live in this source file, not the environment
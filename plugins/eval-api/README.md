# Moveworks Eval API plugin

Evaluate one of your Moveworks agents with the
[Moveworks Eval API](https://docs.moveworks.com/api-reference/alpha-eval-api/overview), guided step by step
by your coding agent in Claude Code or Codex.

Ask "run a Moveworks eval" and the agent walks you through six steps:

1. **Prerequisites:** confirm your access token works and the Eval API is on for your org.
2. **Choose a path:** run ready-made starter test cases, or write test cases from your own questions.
3. **Eval config:** which agent to test, who it runs for, and which systems are simulated or real.
4. **Test cases:** the starter examples, or your questions turned into task files.
5. **Validate and run:** a dry run (seconds, creates nothing), then the real run (usually 10 to 40 minutes).
6. **Report:** scores, failed checks, tool calls, and the full conversations.

Nothing runs against your org without your explicit yes.

## Before you start

- Your org is enabled for the Eval API and the Files API.
- You have an access token from `POST /oauth/v1/token`, saved in a file only you can read
  (for example `~/.moveworks/eval_api/token`, with `chmod 600`). See the prerequisites in the
  [Eval API overview](https://docs.moveworks.com/api-reference/alpha-eval-api/overview). Tokens come from the
  same endpoint as the [Conversations API](https://docs.moveworks.com/api-reference/conversations-api/overview).
- `bash`, `curl` and `jq` are installed.

Never paste your token into the chat. The plugin reads it only from the file, and never prints it.

## Install

In Claude Code:

```
/plugin install eval-api --marketplace moveworks/skills
```

For Codex and older Claude Code versions, see the [repository README](../../README.md#install).

## What's inside

| Path | What it is |
|---|---|
| `skills/moveworks-eval-api/SKILL.md` | the guided walkthrough the agent follows |
| `skills/moveworks-eval-api/eval.sh` | the API helper (`bash eval.sh --help`); reads the token from a file and hands it to curl on stdin |
| `skills/moveworks-eval-api/templates/` | the eval config and an annotated test case to copy |
| `skills/moveworks-eval-api/examples/starter-tasks.yaml` | four test cases that work against almost any agent |

The Eval API is in alpha (`v1alpha1`) and can change. Check the
[official documentation](https://docs.moveworks.com/api-reference/alpha-eval-api/overview) if something behaves
differently than described here.

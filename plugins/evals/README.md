# Moveworks Evals plugin

Everything for evaluating Moveworks agents, in one plugin. It includes two skills: the Eval API walkthrough
and the improvement loop, both below.

## Eval API

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

## Improvement loop

Improve one assistant from its own traffic, every day. Ask "run an improvement loop on my assistant" and the
agent walks you through eight steps:

1. **Set up:** which assistant, how many days of traffic, how big a sample.
2. **Pull traffic:** the assistant's conversations from the
   [Data API](https://docs.moveworks.com/api-reference/data-api), onto your machine only.
3. **Sample:** a stratified sample across capabilities (tool family, utterance type), good and poor
   experiences, and who asked. You see the table before anything is built.
4. **Sensitivity gate:** every sampled conversation is rewritten as an anonymized task or dropped: personal
   data, customer-confidential detail, private calendar or HR content, credentials and anything in a
   `<sensitive>` tag never reach the suite. Mechanical leak checks back up the model, and any error drops
   the conversation.
5. **Task suite:** Eval API tasks with tagged judges (`Correctness`, `Grounding`, `Tool Choice`,
   `Formatting`, `Refusal Appropriateness`, plus code checks for `Completion` and `Safety`).
6. **Run:** a dry run, then the eval run after your yes, against simulated systems only.
7. **Analyze:** failed checks and traces become failure modes, each with one proposed change to
   instructions, tools or documentation.
8. **Daily report:** the negative experiences seen in traffic, the pass rate per tag over time, and what to
   merge. One self-contained HTML file.

The loop proposes changes; it never applies them. Sampled traffic, the suite and the reports stay in a work
folder on your machine that ignores itself in git.

It needs, on top of the Eval API access below: a Data API token saved in a file only you can read (for
example `~/.moveworks/data_api/token`), `python3`, and the `claude` CLI for the labelling and gate model
calls (or your own command, see `skills/rsi-loop/lib/llm.py`).

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
/plugin install evals --marketplace moveworks/skills
```

For Codex and older Claude Code versions, see the [repository README](../../README.md#install).

## What's inside

| Path | What it is |
|---|---|
| `skills/moveworks-eval-api/SKILL.md` | the guided walkthrough the agent follows |
| `skills/moveworks-eval-api/eval.sh` | the API helper (`bash eval.sh --help`); reads the token from a file and hands it to curl on stdin |
| `skills/moveworks-eval-api/templates/` | the eval config and an annotated test case to copy |
| `skills/moveworks-eval-api/examples/starter-tasks.yaml` | four test cases that work against almost any agent |
| `skills/rsi-loop/SKILL.md` | the improvement loop the agent follows |
| `skills/rsi-loop/rsi.sh` | one command per step (`bash rsi.sh --help`); calls the Eval API helper above |
| `skills/rsi-loop/lib/` | pull, sample, sensitivity gate, suite build, summaries, analysis check and report (Python, standard library only) |
| `skills/rsi-loop/templates/` | loop settings, eval configs, the invented seed records, the gate's invented examples, and the analysis shape |

The Eval API is in alpha (`v1alpha1`) and can change. Check the
[official documentation](https://docs.moveworks.com/api-reference/alpha-eval-api/overview) if something behaves
differently than described here.

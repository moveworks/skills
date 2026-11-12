---
name: moveworks-eval-api
description: Run evaluations of one of your Moveworks agents with the Moveworks Eval API (v1alpha1), starting from an access token you already have. Walks through the eval config (which agent, who it runs for, simulated or real systems), the test case files (either ready-made starter examples, or your own questions and expected answers turned into task files), validating with a dry run, starting and following the run, and reading the report. Use when someone asks to "run a Moveworks eval", "evaluate my Moveworks agent", "set up an eval config", "write eval test cases", "validate my task files", or "read my eval report".
---

# Run Moveworks Eval API evaluations

This skill takes someone from **"I have an access token"** to **"here is how my agent did"**. It does not
create or explain how to get a token: that is covered by Moveworks' published documentation (see
Prerequisites).

Files next to this one (`<KIT>` below means this file's directory):

| File | Use |
|---|---|
| `eval.sh` | every API call: `check`, `catalog`, `dry-run`, `create`, `status`, `report`, `transcript`, `cancel`, `list` (`bash <KIT>/eval.sh --help`) |
| `templates/config.yaml` | the eval config to fill in |
| `templates/task.yaml` | one annotated test case to copy |
| `examples/starter-tasks.yaml` | four ready-made test cases that work against almost any agent |

Reference: [Eval API overview](https://docs.moveworks.com/api-reference/alpha-eval-api/overview) (flow,
limits, every endpoint) and the [Files API upload](https://docs.moveworks.com/api-reference/alpha-files-api/files/upload-file).

## Rules

- **Never touch the token.** Do not open, print, copy, or summarize the token file, and never put a token on
  a command line, in a file you write, or in a message. Only `eval.sh` reads it, and it hands it to curl on
  stdin. If the user pastes a token into the chat, stop and tell them it is now in this conversation's
  transcript and they should treat it as exposed (follow their organization's process for a new one).
- **Never start a run without an explicit yes** to the summary in step 5.
- **One question per message.** Give each question a one-line reason. Use multiple-choice questions when
  there is a fixed set of options.
- **Keep the user oriented.** Start each step with `**Step N of 6: <name>.**` and one sentence on what it
  gives them; end it with `Done: <what works now>. Next: <step>.`
- **Work in a folder of the user's choosing** (default `./moveworks-eval/`). Copy templates there; never
  edit the files in `<KIT>`.
- When the user asks what a field means, answer from the Field reference at the end of this file.

## Start: show the journey

Send this on its own, then wait for the user:

> **Here's what we'll do: 6 steps**
> 1. **Prerequisites:** confirm your token works and the Eval API is on for your org.
> 2. **Choose a path:** run ready-made starter examples, or write test cases from your own questions.
> 3. **Eval config:** which agent to test, who it runs for, and which systems are simulated or real.
> 4. **Test cases:** the starter examples, or your questions turned into task files.
> 5. **Validate and run:** a dry run (seconds, creates nothing), then the real run (usually 10–40 minutes).
> 6. **Report:** scores, failed checks, tool calls, and the full conversations.
>
> **Have ready:** your access token saved in a file, and the address of the agent you want to test.
> Ready?

## 1. Prerequisites

**Step 1 of 6: Prerequisites.** Confirms your access before anything else.

Tell the user what the Eval API needs (from the published [Eval API overview](https://docs.moveworks.com/api-reference/alpha-eval-api/overview)):

- Their org is **enabled for the Eval API and the Files API** by Moveworks.
- They hold an **access token** from `POST /oauth/v1/token`, the same token type the Conversations API uses,
  bound to a user who is **provisioned for the Conversations API**.
- That user has **Creator Studio view access**, and **Moveworks Setup edit access** where the org enforces it.

If they don't have a token yet, point them to Moveworks' published guides and stop there; do not walk them
through it:
[Conversations API overview](https://docs.moveworks.com/api-reference/conversations-api/overview) ·
[Create the Conversations API chatbot](https://docs.moveworks.com/api-reference/conversations-api/create-the-chatbot) ·
[Set up service-to-service authentication](https://docs.moveworks.com/api-reference/conversations-api/set-up-service-to-service-authentication)
(Okta and Entra ID variants are linked from the overview). Access tokens are short-lived; they will need to
fetch a fresh one when it expires.

Then ask, one at a time:
1. **Where is the token saved?** A file path only (default `~/.moveworks/eval_api/token`). They should
   create the file themselves, readable only by them (`chmod 600`). Do not ask for the token itself.
2. **Which data center?** US production is `https://api.moveworks.ai` (the default). If they use another
   region, ask for the host their Moveworks team gave them.

Run the check (set the two values at the top of every command; shell variables don't persist):

```bash
EVAL_TOKEN_FILE=<path> EVAL_HOST=<host> bash <KIT>/eval.sh check
```

| Result | Meaning |
|---|---|
| `OK (200)` | ready |
| `401` | the token is invalid or expired: get a fresh one and retry |
| `403` | the user lacks a permission or Conversations API provisioning: the message names it; their admin or Moveworks team fixes it |
| `404` | the Eval API isn't enabled for the org, or the host is the wrong region |

Done: your token works. Next: choose a path.

## 2. Choose a path

**Step 2 of 6: Choose a path.** Decides whether we start from examples or from your questions.

Ask (multiple choice):
- **Starter examples (quickest):** four ready-made test cases that check general behavior (answers, admits
  when nothing matches, follows up, doesn't invent). Good for a first end-to-end run.
- **My own test cases:** turn their questions and expected answers into task files.

They can do the starter run first and their own afterwards.

Done: path chosen. Next: the eval config.

## 3. Eval config

**Step 3 of 6: Eval config.** One file per run: which agent, who it runs for, and where its tool calls go.

Copy `templates/config.yaml` into the work folder, then fill it in with the user, one value at a time:

1. **`title`**: a name for the run.
2. **`scoped_agent_id`**: the agent under test. Tell them how to find it: open the agent in their Moveworks
   assistant in the browser; the id is the UUID in the address after `/assistant/agents/` (in the
   `mw_route` parameter, URL-encoded as `%2Fassistant%2Fagents%2F<id>`). It must belong to the org their
   token is for. Moveworks tests a temporary copy; the live agent is never changed.
3. **`requestor`**: `name` and `email` of the **token's user**. Explain: there are two "users" and both are
   fixed for the whole run. The conversations always run as the token's user (their permissions, their
   identity in real systems); `requestor` is the persona the simulated systems treat as "me". Keep them the
   same person. To test different users, do one run per user, each with that user's token.
4. **`platform`** (optional): leave out for the default (`hera`); `minerva` only if their assistant runs on
   it. Agents with MCP connectors need `hera`.
5. **`connector_map`**: for each connector the agent calls, one of three choices. Explain the trade-off:
   - **mocked (simulated):** the connector points at a Moveworks simulator that starts empty for every test
     case and is loaded with that test case's `import_data`. Answers are known in advance and repeatable,
     and nothing real changes. This tests the agent's behavior, not their content.
   - **as_is (real):** the real connector with real credentials and live data. This tests the whole system,
     including their real content; expected answers can change as the data changes, and writes are real.
   - **left out:** no row; the connector is cut off for the run.
   - Built-in actions (ticket updates, password resets, handoffs) bypass `connector_map` and always act for
     real in their org, so test cases should not ask for them unless that is intended.
   - **If every connector should be left out** (e.g. the starter path), keep the template's placeholder row:
     the API needs at least one row, and that row names no real connector.
   - For a **mocked** row, find the simulator with `bash <KIT>/eval.sh catalog <word>` (e.g. `hr`, `kb`,
     `pages`). Its `eval_URL` is `https://marketplace.moveworks.com/api/purple-suite/<app>` plus the path that
     matches the connector's action. Each simulator publishes its endpoints and record formats at
     `https://marketplace.moveworks.com/api/purple-suite/<app>/openapi.json`.
   - The dry run can't see the agent's connectors. A connector left out by mistake shows up only after a
     run, as a `WARNING_CODE_UNMAPPED_CONNECTOR_SKIPPED` warning naming it.

Show the finished config back as a short list.

Done: the config is ready. Next: test cases.

## 4. Test cases

**Step 4 of 6: Test cases.** Each test case is one simulated conversation, with its own data and checks.

Explain the shape of a run once: **one user, one agent copy and one connector setup** for the whole run;
**up to 500 test cases**, each starting in **its own fresh simulated environment** with its own data.

### 4A. Starter examples

Copy `examples/starter-tasks.yaml` into the work folder and replace `<YOUR NAME>` / `<YOUR EMAIL>` with the
token's user. Tell them what the four check: describes what it can do; says "not found" for a policy that
doesn't exist instead of inventing one; keeps context on a follow-up; handles an unscripted user. With the
placeholder connector row, no connector is called.

### 4B. Their own test cases

Ask for their questions and the answer each should get (one at a time, or a list they paste). For each
question, work through these and write one test case from `templates/task.yaml`:

| Ask | Goes into |
|---|---|
| What does the user ask? (exact words, or leave unscripted) | `user_agent.user_turns` (optional) |
| Who is the user and when do they stop? How should they answer follow-up questions? | `user_agent.user_agent_prompt` (required) |
| What data should the agent find when it looks things up? | `import_data` (required) |
| What does a correct answer say, and what must it not do? | `graders` → `llm_as_judge` (required) |
| Must it use a particular tool, or leave data unchanged? | `graders` → `tool_sequence` / `deterministic` (optional) |
| A short id | `goal_id` (required, unique) |

Explain these points as they come up:

- **`user_agent_prompt` vs `user_turns`:** an AI plays the employee. `user_turns` are scripted opening
  lines sent in order; the prompt covers everything else (follow-up questions the agent asks, when to end).
  The prompt is required even when every line is scripted.
- **How `import_data` works:** the agent never reads it directly. When it calls a **mocked** tool, the
  simulator answers from these records, exactly as a real API would. Each collection name (e.g.
  `employees`, `kb_articles`, `pages`, `courses`) is a table that one simulator reads; the name must match
  the catalog exactly (`eval.sh catalog`). Several collections for several simulators can sit side by side.
  **as_is** connectors never see this data. At least one record is required even if every connector is
  as_is: keep one placeholder employee (the token's user).
- **Field names** follow the simulator's format (e.g. `firstName`, not `first_name`). Neither collection nor
  field names are checked by the dry run: a typo just means the agent finds nothing.
- **Each test case starts empty:** seed everything its question depends on, plus anything those records
  refer to (e.g. an employee's department). To test tricky cases, add look-alikes or leave records out on
  purpose.
- **Writing judge checks:** one line per check in `high_level_behavior`. Write what a correct answer says
  and what it must not do (invent facts, cite the wrong source, ignore a newer document). A `Category:`
  prefix (e.g. `Correctness:`) groups scores in the report; if any bullet in any file uses one, every bullet
  in every file must.
- **With real (as_is) content,** you can't fix the exact answer in advance; check behavior instead
  ("cites an article", "says it couldn't find one", "does not invent a number").
- **Pack test cases into one file**, separated by `---`. Every upload counts against the org's rate limit.

Done: test cases written. Next: validate and run.

## 5. Validate and run

**Step 5 of 6: Validate and run.** A dry run first (creates nothing), then the real run.

```bash
EVAL_TOKEN_FILE=<path> EVAL_HOST=<host> bash <KIT>/eval.sh dry-run <config.yaml> <task file(s)>
```

A `200` means the token, permissions, config, connector mapping and every task's format check out. On a
`400`, explain each issue line in plain words (each names `file#task CODE at path`) and fix the files:

| Code | Meaning |
|---|---|
| `ISSUE_CODE_MISSING_REQUIRED_FIELD` | a required key is missing or empty (e.g. `import_data.collections`) |
| `ISSUE_CODE_UNKNOWN_FIELD` | a misspelled or unsupported key |
| `ISSUE_CODE_MALFORMED_YAML` | the file doesn't parse |
| `ISSUE_CODE_DUPLICATE_GOAL_ID` | two test cases share a `goal_id` |
| `ISSUE_CODE_UNPREFIXED_JUDGE_ASSERTION` | some judge bullets have a `Category:` prefix and some don't |
| `ISSUE_CODE_UNMAPPED_CONNECTOR` | `connector_map` rows conflict for one connector |
| "at least one llm_as_judge" | a test case has only tool or data checks |

Say what the dry run cannot check: collection and field names in `import_data`, and connectors the config
leaves out. Warnings are not errors: `AS_IS_CONNECTOR_WRITES` means real connectors may write;
`OTHER` about native plugins means built-in actions act for real.

Then show this summary and ask for an explicit yes:
- agent (`scoped_agent_id`), platform, number of test cases, the user it runs as
- connectors: simulated / real / left out
- time: usually 10–40 minutes for a handful of test cases, mostly setup and teardown; runs stop after 6 hours
- effects: a temporary copy of the agent and per-test-case simulators are created and deleted; real (as_is)
  connectors and built-in actions act for real; one active run per user, extra runs queue

On yes:

```bash
EVAL_TOKEN_FILE=<path> EVAL_HOST=<host> bash <KIT>/eval.sh create <config.yaml> <task file(s)>
EVAL_TOKEN_FILE=<path> EVAL_HOST=<host> bash <KIT>/eval.sh status <run id> --wait
```

Run `status --wait` in the background and report progress as it changes: `QUEUED` → `RUNNING`
(`PREPARING`, then `RUNNING_TASKS`) → `COMPLETED`, then cleanup. **If it stops with a 401**, the token
expired: the run keeps going on the Moveworks side. Ask the user to refresh the token in the same file
their usual way, then run `status <run id> --wait` again. To stop a run: `eval.sh cancel <run id>`.
To find a run id again: `eval.sh list`.

Done: the run finished. Next: the report.

## 6. Report

**Step 6 of 6: Report.** What the agent did, and how it was graded.

`status --wait` prints the summary when the run completes; `eval.sh report <run id>` prints it again and
saves `report.json`. Give the user a short table: each test case, pass or fail, its failed checks, and the
scores per category. Explain:

- **"Finished" is not "passed".** A conversation can finish and still fail its checks.
- **Failed checks** quote the exact judge line that failed, the missing tool call, or the data check's
  actual vs expected value.
- **Task errors** (e.g. a turn timeout) mean the conversation itself failed; the report still covers every
  other test case.

Offer the conversations: `eval.sh transcript <run id> [<test case id>]` shows each turn, every tool call
with the connector mode it went through (mocked or as_is), its arguments and what came back. Start with
failed test cases. This is how they tell why a test case passed or failed.

To run more: edit the test cases and go back to step 5. Every run produces new results; the agent's wording
can differ from run to run, and real content can change.

## Field reference

**Eval config** (one per run)

| Field | Required | Notes |
|---|---|---|
| `title` | yes | label in reports |
| `requestor.name`, `requestor.email` | yes | the persona; use the token's user |
| `scoped_agent_id` | yes | from the agent's address; same org as the token |
| `connector_map[]` | yes (≥ 1 row) | each row: `name` (required), `mode` (`mocked` default, or `as_is`), `eval_URL` (required for mocked), `original_URL` (optional, to disambiguate) |
| `platform` | no | `hera` (default) or `minerva` |
| `source_org`, `target_org` | no | default to the token's org; any other value is rejected |

**Test case** (many per file, separated by `---`)

| Field | Required | Notes |
|---|---|---|
| `goal_id` | yes | unique across the whole run |
| `frozen_time` | no | pins "now" for date logic |
| `import_data.collections` | yes (≥ 1 record) | collection names from `eval.sh catalog`; records in the simulator's format |
| `user_agent.user_agent_prompt` | yes | the simulated user's instructions |
| `user_agent.user_turns[].utterance` | no | scripted opening lines |
| `graders[]` | yes (≥ 1 `llm_as_judge`) | each needs `name` (unique in the file) and `type`; `desc` optional |
| `llm_as_judge` → `high_level_behavior` | yes | one `- ` line per check; optional `Category:` prefixes, all or nothing |
| `tool_sequence` → `expected_sequence` | yes | tool names, any order |
| `deterministic` → `collections_to_fetch`, `query`, `expected_value` | yes | read-only SQL on the simulator afterwards; `comparison_type` optional (default `equals`) |

**Limits:** up to 500 test cases per run; each ≤ 2 MiB; all seed data in a run ≤ 64 MiB; each file
≤ 100 MiB; one active run per user; runs stop after 6 hours; about 120 API requests per minute per org.

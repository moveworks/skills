---
name: rsi-loop
description: Run a daily improvement loop on one Moveworks assistant. Pulls the assistant's real traffic with the Data API, draws a stratified sample across capabilities (tool family and utterance type), experiences (good and poor, from an LLM experience judge) and audience, passes every sampled conversation through a sensitivity gate that rewrites it as an invented task with the same intent or drops it, builds an Eval API task suite with tagged judges, runs it, reads the traces of failing and poor tasks to find the turn and step where each went wrong, turns that into proposed changes (instructions, MCP tools, documentation, plugins), and writes a daily report of the negative experiences seen and what to approve or skip. Use when someone asks to "run an RSI loop on my assistant", "set up a self-improvement loop", "build an eval suite from real traffic", "sample my assistant's traffic for evals", "what should I fix in my assistant", or "give me the daily improvement report".
---

# Run an improvement loop on a Moveworks assistant

This skill runs a recursive self-improvement (RSI) loop on **one assistant in one org**: look at what real
users asked, measure how the assistant does on a clean copy of those requests, work out why it fails, and
hand a person a short list of changes to merge. Then do it again tomorrow and watch the numbers move.

Use it when someone wants to improve an assistant from its own traffic. To run a one-off eval from questions
the user already has, use the `moveworks-eval-api` skill next to this one instead.

The loop proposes changes. It never applies them: do not edit the assistant's instructions, tools or
documentation as part of this skill.

Files next to this one (`<KIT>` below means this file's directory; `<WORK>` is the user's work folder):

| File | Use |
|---|---|
| `rsi.sh` | every step: `init`, `pull`, `sample`, `gate-test`, `gate`, `build`, `check`, `dry-run`, `run`, `summarize`, `traces`, `packets`, `analysis-check`, `report`, `decide`, `daily`, `leak-scan`, `status` (`bash <KIT>/rsi.sh --help`) |
| `lib/` | the Python behind each step (standard library only); each file's header documents its inputs and outputs |
| `tests/` | unit tests for the gate's wording check, the experience judge and the trace step (`python3 -m unittest discover -s <KIT>/tests`) |
| `templates/loop.json` | the loop's settings: org, assistant, window, sample size, simulators |
| `templates/config.mocked.yaml`, `templates/config.talk-only.yaml` | eval configs to fill in |
| `templates/world.json` | the invented records seeded into every eval task |
| `templates/fewshot_gate.json` | the gate's explicit rules and invented examples of what to keep, rewrite and drop |
| `templates/analysis.json` | the shape of the failure analysis you write in step 7 |
| `../moveworks-eval-api/` | the Eval API helper (`eval.sh`) and its walkthrough; this skill calls it for every eval step |

Reference: [Data API](https://docs.moveworks.com/api-reference/data-api) and
[Eval API overview](https://docs.moveworks.com/api-reference/alpha-eval-api/overview).

## The vocabulary

Use these words, in this order, whenever you describe results:

- **Task suite:** the set of tasks for this assistant. Each task is one conversation to simulate. The suite
  is append-only: a task keeps its wording from the day it was added, so results compare over time.
- **Judges:** every task carries judges. An AI judge checks the conversation line by line; code checks
  (did it reply at all, did a read-only request leave the data alone) are judges too.
- **Tags:** every judge check carries one tag from a small fixed vocabulary: `Correctness`, `Grounding`,
  `Tool Choice`, `Formatting`, `Refusal Appropriateness`, `Completion`, `Safety`. Checks that overlap share a tag.
- **Per-tag pass rate over time:** the metric. Passed checks over checks with that tag, per run, shown as a
  trend. One run is a baseline, and say so.

Never report a bare count ("85 cases", "12 failing"). Give a rate and the number of checks or conversations
behind it. Strata (who asked, which tool family, which utterance type, good or poor experience) are how you
slice a rate, not tags.

## Rules

- **The sensitivity gate is not optional.** Nothing reaches the suite, the analysis or the report except
  through step 4 and the leak checks in steps 5, 7 and 8. The suite stores invented tasks with the same
  intent, never the user's wording. Never write a task by hand from a real conversation, never copy text
  from `<WORK>/raw/` anywhere, and never skip or weaken a check that fails: fix the content and run it again.
- **Real traffic stays in `<WORK>/raw/`.** Do not open, print, quote or summarize the files in it, and that
  includes the trace playbacks in `<WORK>/raw/traces/`. The
  scripts read them; you read the outputs (`strata.md`, `population.json`, `gate_test.md`, summaries), which
  hold labels and rates only. If diagnosing a failure truly needs a production conversation, ask the user
  first, read only that conversation, and write down only a paraphrase.
- **Never touch a token.** Do not open, print or copy the Data API or Eval API token files, and never put a
  token on a command line or in a file. Only `lib/pull.py` and `eval.sh` read them. If the user pastes a
  token into the chat, tell them it is now in this conversation's transcript and should be treated as exposed.
- **Never start an eval run without an explicit yes.** A daily run needs a standing yes, recorded once in
  `loop.json` (step 8).
- **Simulated systems only.** Never map a connector `as_is` for this loop. It would act for real and bring
  real records back in the report.
- **Nothing from `<WORK>` goes into a repository, a ticket or a chat message** unless `rsi.sh leak-scan`
  says it is clean and the user asked for it. The work folder ignores itself in git.
- **One question per message**, each with a one-line reason. Start each step with `**Step N of 8: <name>.**`
  and end it with `Done: <what exists now>. Next: <step>.`
- Never edit the files in `<KIT>`. To change the seeded records or the gate's examples, copy the template
  into `<WORK>` (`world.json`, `fewshot_gate.json`) and edit the copy.

## Design notes

How the loop answers the questions an eval team asks first:

- **Where did a conversation go wrong, not just pass or fail?** Step 7 reads the agent trace of every failing
  and poor task: the eval run's own trajectory, and a playback of the production conversation behind it
  (`mw chat-eval playback`, conversation playback in Cockpit). Each failure mode names the turn and the step (the tool
  call) where it went wrong. Playback needs a signed-in Cockpit session; without one, `rsi.sh traces` records
  `needs_cockpit_session` and the report says so at the top of What to merge.
- **How does it tell good from poor without a judge?** It has one. An explicit LLM experience judge, with a
  small rubric and invented few-shots, labels every conversation good or poor from the request, the final reply
  and the user's next message. Thumbs down, no reply, retry, connect prompts, hand-offs and slow replies stay
  as its features, and its agreement with them is recorded in `strata.md`, `population.json` and the report.
- **Utterances carry names, ids and personal data.** Every sampled conversation passes the sensitivity gate
  (step 4) before anything is stored: it is dropped, or rewritten as an invented task with the same intent. A
  wording check refuses any task that shares 8 words in a row with its conversation or 10 with any pulled
  message, and a second model that sees only the rewrite audits it adversarially. The report counts every step.
- **What is the output?** Proposals. Each one is a before and after diff against the assistant's instructions,
  an MCP tool's description or schema, documentation, or a plugin, with Approve / Skip in the daily report.
  The loop applies nothing.

## Start: show the journey

Send this on its own, then wait for the user:

> **Here's the loop: 8 steps**
> 1. **Set up:** which assistant, how many days of traffic, how big a sample, and a check that your access works.
> 2. **Pull traffic:** the assistant's conversations from the Data API, onto this machine only.
> 3. **Sample:** a stratified sample across capabilities, good and poor experiences, and who asked. You see the table before anything is built.
> 4. **Sensitivity gate:** every sampled conversation is rewritten as an anonymized task or dropped. Tested on 10 first.
> 5. **Task suite:** the rewritten tasks become Eval API tasks with tagged judges.
> 6. **Run:** a dry run, then the eval run after your yes.
> 7. **Analyze:** failed checks and traces become failure modes, each with a proposed change.
> 8. **Daily report:** the negative experiences seen, the pass rate per tag over time, and what to merge.
>
> **Have ready:** a Data API token and an Eval API token, each saved in its own file, and the assistant's id.
> Ready?

Set the work folder on every command (shell variables do not persist): `RSI_WORK=<WORK> bash <KIT>/rsi.sh <command>`.

## 1. Set up

**Step 1 of 8: Set up.** Decides what the loop improves and confirms access.

Ask, one at a time:

1. **Which assistant?** Its id (the UUID in the assistant's address in the browser) or its exact name, plus a
   short display name for reports.
2. **Which org?** A label for reports. Each token is bound to one org; both tokens must be for this one.
3. **How many days of traffic?** Default 14.
4. **How many conversations to sample?** Default 100. This is the most the suite grows by per sample.
5. **Where should the work folder go?** Default `./rsi-loop/`. It will hold real conversations, so it must be
   on this machine, not in a synced or shared folder.
6. **Where are the tokens saved?** File paths only. Data API default `~/.moveworks/data_api/token`; Eval API
   default `~/.moveworks/eval_api/token`. Each file readable only by the user (`chmod 600`).

The Data API token needs read access to conversations, interactions, plugin calls and plugin resources. The
loop never requests the users table. For the Eval API prerequisites, follow step 1 of the `moveworks-eval-api`
skill; do not walk the user through creating a token.

```bash
RSI_WORK=<WORK> bash <KIT>/rsi.sh init --org <org> --assistant <id or name> --name "<display name>" --days 14 --n 100
RSI_WORK=<WORK> EVAL_TOKEN_FILE=<path> bash <KIT>/rsi.sh check
```

`init` writes `<WORK>/loop.json` (see the Field reference) and copies the two eval config templates into
`<WORK>/suite/`. If machines also call the assistant (scheduled runs, integrations that open every message
with the same line), ask for those opening lines and add them to `automation_prefixes` in `loop.json`, so
the sample can tell a person from a machine caller.

Done: the work folder exists and access is confirmed. Next: pull traffic.

## 2. Pull traffic

**Step 2 of 8: Pull traffic.** Copies the assistant's conversations for the window onto this machine.

```bash
RSI_WORK=<WORK> DATA_API_TOKEN_FILE=<path> bash <KIT>/rsi.sh pull
```

It scans the window hour by hour, keeps the turns this assistant handled, then pulls their tool calls and
reactions. It paces itself and backs off on rate limits; finished days are cached, so tomorrow's pull only
fetches the new hours. Run it in the background for a busy org.

Report from the printed manifest, as rates: share of turns with a thumbs down, with no reply, retried, and
the reply-time percentiles. Do not open the pulled files.

Done: `<WORK>/raw/<window>/` holds the traffic. Next: sample.

## 3. Sample

**Step 3 of 8: Sample.** Chooses which conversations become tasks, and makes the choice visible.

```bash
RSI_WORK=<WORK> bash <KIT>/rsi.sh sample
```

Three pivots:

| Pivot | Strata | How it is assigned |
|---|---|---|
| **Capability** | tool family (the system the turn called most) and utterance type (what the user wanted done) | tool family from the tool calls; utterance types are induced once from the traffic by a model, then frozen in `taxonomy.json` so they stay comparable |
| **Experience** | good or poor, with the reason | an explicit LLM judge (rubric and invented few-shots in `lib/sample.py`) reads the request, the final reply and the user's next message; the hard signals (thumbs down, no reply, the user asked again, an ask to connect a system, a hand-off, a reply that says it could not do it or is the model's thinking notes, a slow reply) are its features. If a judge call fails, that conversation uses the hard-signal rule |
| **Audience** | a person or a machine caller, and the channel | from `automation_prefixes` and the platform |

Cells are audience x tool family x experience. Every non-empty cell gets at least two picks; poor cells then
get 55% of the rest, because the loop is looking for what to fix. Each pick carries a weight (cell
population over cell sample) so pass rates can be weighted back to real traffic. One conversation per
distinct prompt, and a cap per user.

Show the user `<WORK>/sample/strata.md`: population against sample for every cell, then each margin
(utterance type, channel, poor reason, tool action, depth), and the judge's agreement with the hard signals.
A big disagreement in one signal (say, most "retried" conversations judged good) usually means that signal is
mismeasured; say so. Ask whether the split looks right before going
on; `--poor-share`, `--people-share` and `--n` change it. Nothing in this folder holds utterance text.

Done: `sample.jsonl` lists the picks and ranked reserves; `population.json` holds the traffic rates the
report uses. Next: the sensitivity gate.

## 4. Sensitivity gate

**Step 4 of 8: Sensitivity gate.** Turns each sampled conversation into an anonymized task, or drops it.
This step is required every time, and it runs before anything is written to the suite.

The suite stores invented tasks with the same intent, never the user's wording. What the gate does to each
conversation:

1. **Pre-check:** a `<sensitive>` tag anywhere in the conversation drops it unread.
2. **Gate (model, few-shot):** drops requests that are about, or cannot be stated without, personal data,
   customer-confidential detail, private calendar or HR content, credentials, internal material or unreleased
   roadmap content; also drops non-tasks. Otherwise it rewrites the request in new words with generic people,
   companies, ids and dates, and notes what private data the task would read if it ran for a real user. It
   follows the explicit `rules` in the few-shot file: internal initiative, project and roadmap content is
   rewritten into invented content, not one-word swaps, and unreleased roadmap content is dropped.
3. **Mechanical leak check:** the rewrite may not contain any term the gate removed, an email, a link, an id
   or a long number.
4. **Wording check (hard):** the task and its success criteria may not share 8 or more words in a row with
   the source conversation (user or assistant turns), 10 or more with any message in the pull, or the whole
   text of a short message. A task that does is reworded by a model that sees only the rewritten task, up to
   three times, and dropped as `source_wording` if it still matches. Before anything is written, every kept
   task is checked once more, and a match is dropped, never emitted.
5. **Adversarial audit (second model call that sees only the final rewrite):** it tries to recover anything
   real from it; a flag drops the conversation.
6. **Duplicates** of a kept task are dropped. **Any error drops the conversation.** Dropped picks are
   replaced from the same cell's reserves.

Always test on 10 first and read the result with the user:

```bash
RSI_WORK=<WORK> bash <KIT>/rsi.sh gate-test 10
```

It prints rewritten text only. Check with the user that nothing in the kept rows looks like a real name,
customer, project or id, that no row reads like a copy of a real message with a name swapped, and that the
drops make sense. If the gate keeps something it should drop or the reverse, copy
`templates/fewshot_gate.json` to `<WORK>/fewshot_gate.json`, add an **invented** example of the right
behavior (and, for a rewrite that was too close, a `not_this` with the wrong version), and test again. Then
the full run:

```bash
RSI_WORK=<WORK> bash <KIT>/rsi.sh gate
```

Report the share kept, the share dropped per reason and per step, the share the wording check reworded (it
is all appended to `strata.md`), and which cells are still short after backfill.

Done: `tasks_candidates.jsonl` holds rewritten tasks only: no original text and no source ids. Next: the suite.

## 5. Task suite

**Step 5 of 8: Task suite.** Turns the rewritten tasks into Eval API tasks with tagged judges.

First the eval config, once. Read the tool families in `strata.md`, then find which have a simulator:
`bash <KIT>/../moveworks-eval-api/eval.sh catalog <word>`. Copy `<WORK>/suite/config.mocked.yaml` to
`<WORK>/suite/config.yaml` and fill it in with the user (the `moveworks-eval-api` skill, step 3, explains
every field). Then list the simulated tool families in `loop.json` under `simulators`, for example
`{"<tool family>": "<simulator app>"}`. If nothing can be simulated yet, use `config.talk-only.yaml` and
build with `--no-deterministic`.

```bash
RSI_WORK=<WORK> bash <KIT>/rsi.sh build
```

Every task gets:

- the rewritten request as the scripted opening line, and a simulated user who answers follow-ups only from
  that request;
- the same invented records (`world.json`), so generic names like DEMO-101 resolve to something;
- one AI judge with five tagged lines: `Correctness` (the task's own success criteria) and four fixed lines
  for `Grounding`, `Tool Choice`, `Formatting` and `Refusal Appropriateness`;
- code checks tagged `Completion` (it replied) and `Safety` (a read-only request changed nothing, and no
  real connector was called).

Each task also gets a run tier and an environment fit, recorded in `suite/manifest.jsonl`:

| Value | Meaning |
|---|---|
| `ready` | safe to run |
| `held:private_runtime_data` | would read calendar, mail, HR or customer data as the token's user; never uploaded |
| `held:real_side_effect` | asks for a change in a system that is not simulated; never uploaded |
| `simulated` / `no_system_needed` | the task can be judged fairly |
| `cut_off` | its system has no simulator, so an honest "I cannot reach it" is the best possible reply; its rates are reported apart |

A system with no built-in simulator stays `cut_off` until you stand up a simulated app for it and add it to
`config.yaml` and `simulators`. Tell the user which tool families are in that state and what share of
traffic they are: that is the gap in what the suite can measure.

`build` ends with a mechanical leak check over every task file and removes the files if it fails. The suite
is append-only (up to `max_suite_tasks`); `--replace` starts it over and breaks the trend.

Done: `<WORK>/suite/tasks/ready-*.yaml` are ready to upload; `held.yaml` is not. Next: run.

## 6. Run

**Step 6 of 8: Run.** A dry run, then the eval run after a yes.

```bash
RSI_WORK=<WORK> EVAL_TOKEN_FILE=<path> bash <KIT>/rsi.sh dry-run
```

Explain any issue lines with the table in the `moveworks-eval-api` skill, step 5. Then show this summary
and ask for an explicit yes: the assistant, the number of tasks, which systems are simulated and which are
left out, that a temporary copy of the assistant is tested and the live one is not changed, and that a run
usually takes 10 to 40 minutes or more.

On yes, run the pilot file first (one task per environment fit and experience), read its result, then the rest:

```bash
RSI_WORK=<WORK> EVAL_TOKEN_FILE=<path> bash <KIT>/rsi.sh run pilot
RSI_WORK=<WORK> EVAL_TOKEN_FILE=<path> bash <KIT>/rsi.sh run all
```

Run these in the background. Files run one at a time, a finished file is skipped on a re-run, and an
interrupted one resumes from its saved run id. If polling stops with a 401, the token expired: the run
continues on the Moveworks side; ask the user to refresh the token file and run the same command again.

`run` ends by writing `<WORK>/runs/<date>/summary.json`. Report the pass rate per tag with the checks
behind each, then the same leaving out `cut_off` tasks, then the weakest strata. "Finished" is not "passed".

Done: today's pass rates exist. Next: analyze.

## 7. Analyze

**Step 7 of 8: Analyze.** Finds where each failing or poor task went wrong, then turns that into failure
modes, each with one proposed change.

First the traces. Pass or fail says that a task failed, not where:

```bash
RSI_WORK=<WORK> bash <KIT>/rsi.sh traces
RSI_WORK=<WORK> bash <KIT>/rsi.sh packets
```

`traces` plays back the production conversation behind every task with a failed check and every
poor-experience task (`mw chat-eval playback`; set `trace_org` in `loop.json` if the org's name there is a
label, or `RSI_TRACE_CMD` for another tool). The playbacks are real traffic and stay in `<WORK>/raw/traces/`;
`analysis/<date>.traces.json` keeps only where each one went wrong: the turn, the step (the tool call, in
order), the tool and an outcome class. **It needs a signed-in Cockpit session.** If it prints
`needs_cockpit_session`, ask the user to open the playback page in Cockpit in Chrome and send the cookies with the
Moveworks Tools extension, then run it again. Do not try to sign in for them. Without traces, carry on: the
report says the traces are missing and the causes rest on reply text and tool names.

`<WORK>/analysis/<date>.packets.json` holds one packet per task with a failed check: its strata, the judge
line or data check that failed, the trace from the run (each tool call with its status, connector mode
and error, and the final reply), and where it went wrong: `went_wrong` from the run, `went_wrong_in_traffic`
from the production trace. These are the suite's rewritten tasks against simulated systems, so you
may read them. With no eval run yet, the packets are the suite's poor-experience tasks and the signals the
sampler saw, and the analysis is marked `traffic_sample`.

Read the packets and write `<WORK>/analysis/<date>.json` in the shape of `templates/analysis.json`:

- **Failure modes.** Group tasks that fail for the same reason. For each: a plain title, its tasks, two or
  three evidence lines, the likely cause, and your confidence. Evidence describes what the trace shows
  ("two searches and no call to the tool for that system"); it never quotes a conversation. For every task
  whose packet names where it went wrong, add a `went_wrong` line: `{"task", "turn", "step", "what"}`, with
  `what` in your own words ("the tracker read came back not connected, and the reply asked the user to
  authorise").
- **Not failures.** A task whose failed check is the environment's or the sampler's doing goes here with the
  reason, not into a mode.
- **Proposals.** One per failure mode. `kind` is what changes: `prompt` (the assistant's instructions),
  `tool` (an MCP tool's description, schema or behavior), `documentation` (knowledge the assistant reads),
  `plugin` (a plugin's logic or configuration), `skill`, `access` (a connection or permission, no merge
  needed), `platform` (report it to Moveworks) or `loop` (the sample or suite is measuring it wrong). Give where it merges, the exact before and after
  text, the tags it should move, what you expect, and your confidence. Describe locations in words: no
  links, ids or emails.

Prefer the smallest change that explains the most failed checks. If you cannot tell the cause from the
trace, say so and propose the diagnostic instead of a guess.

```bash
RSI_WORK=<WORK> bash <KIT>/rsi.sh analysis-check
```

It fails if a failed task is in no mode or in two, a mode has no proposal, a proposal is incomplete, a tag
is not in the vocabulary, a traced task has no `went_wrong` line, or any text has an id, link or email in it
or repeats wording from the pulled traffic. Fix what it names and run it again.

Done: the analysis passes its check. Next: the report.

## 8. Daily report

**Step 8 of 8: Daily report.** One page: what went wrong for users, and what to merge.

```bash
RSI_WORK=<WORK> bash <KIT>/rsi.sh report
```

It writes `<WORK>/reports/<date>.html` (self-contained) and `.md`, in this order:

1. **Negative experiences in traffic:** the poor rate for the window, what went wrong as shares, where it
   happens (audience x tool family, utterance type), and the poor rate by day.
2. **What to merge:** each proposal with its kind, where it merges, the before and after text, Approve / Skip,
   the failure mode it fixes with its evidence and the turn and step where it went wrong, how much traffic its
   tasks stand for, the tags it should move and where those tags are today. A "Needs Cockpit session" note
   heads this section when traces are missing.
3. **Eval suite, pass rate per tag:** the latest run, the change since the previous run, and the trend over
   the last ten runs, plus pass rates per stratum.
4. **Sensitivity gate:** the share kept, dropped per step (gate, leak check, wording check, adversarial audit,
   duplicates) and per reason, and the share the wording check reworded.
5. **Design notes:** the four answers above.

The report is scanned before it is written, and is not written if the scan finds an id, link, email or
wording from real traffic. Give the user the path to the HTML file and a three-line summary: the poor rate,
the tag that moved most, and the first change to merge. The user decides: Approve / Skip in the HTML copies
`rsi.sh decide <date> <id> approve|skip`, which records it on the proposal (you can run it when they tell you
in chat). An approved change is still theirs to merge; when they say it was merged, set its `status` to
`merged` in the analysis file so tomorrow's report shows it.

### Running it every day

One command does the mechanical part of the day:

```bash
RSI_WORK=<WORK> DATA_API_TOKEN_FILE=<path> EVAL_TOKEN_FILE=<path> bash <KIT>/rsi.sh daily
```

It pulls the new hours, samples (with the experience judge), gates, adds any new tasks to the suite,
validates with a dry run, and then starts the eval run **only if** `loop.json` has
`"run_approval": "standing"`. It ends with `traces`, `packets` and a report. Ask the user once whether
they want that standing yes; set it only on their explicit answer, and tell them how to turn it off (set it
back to `"ask"`). Without it, `daily` stops before the run and prints the command.

After `daily`: do step 7 for the new date, then run `report` again. A merged proposal is tested by the next
run: the tags it named should move, and the report shows whether they did.

Schedule `daily` with whatever the user already uses (cron, launchd, a scheduled agent session). The
analysis step needs an agent session, so the day's report is complete once that session has run.

When the user is done with a window, tell them they can delete `<WORK>/raw/`; nothing after step 4 needs it
except the quote checks in steps 7 and 8.

## Field reference

**`loop.json`**

| Field | Default | Notes |
|---|---|---|
| `org` | required | label for reports; both tokens must belong to it |
| `assistant` | required | the assistant's id or exact name, matched against each turn |
| `assistant_name` | empty | display name for reports; an id is never shown |
| `window_days` | 14 | days of traffic per pull |
| `sample_size` | 100 | conversations per sample |
| `id_prefix` | `rsi` | prefix of every task's `goal_id` |
| `persona` | an employee using a work assistant | who the simulated user is |
| `automation_prefixes` | `{}` | `{"<opening line>": "<label>"}` for machine callers |
| `human_sources` | `["interactive"]` | sources that count as a person asking |
| `simulators` | `{}` | `{"<tool family>": "<simulator app>"}` for systems the eval config simulates |
| `no_system_families` | `["no tool"]` | tool families that need no connector |
| `max_suite_tasks` | 300 | the suite stops growing here |
| `run_approval` | `ask` | `standing` lets `daily` start the eval run |
| `trace_org` | `org` | the org name the trace playback tool expects, when `org` is only a label |

**What each step writes** (all under `<WORK>`, none of it for sharing unless `leak-scan` passes)

| Path | Holds | Real text? |
|---|---|---|
| `raw/<window>/`, `raw/traces/` | pulled conversations, derived turns, trace playbacks | **yes: never leaves this machine** |
| `sample/strata.md`, `population.json`, `sample.jsonl`, `labels.jsonl`, `taxonomy.json` | strata, rates, picks, judge verdicts and agreement | no (ids only) |
| `sample/tasks_candidates.jsonl`, `audit.jsonl`, `gate_test.md` | rewritten tasks, gate decisions | no |
| `suite/tasks.jsonl`, `tasks/*.yaml`, `held.yaml`, `manifest.jsonl`, `tags.json` | the task suite | no |
| `runs/<date>/` | eval reports, `summary.json`, `traces.jsonl` | simulated conversations only |
| `analysis/<date>.traces.json`, `<date>.packets.json`, `<date>.json` | where each task went wrong (turn, step, tool, outcome); failed checks with traces; failure modes and proposals | no |
| `reports/<date>.html`, `.md` | the daily report | no |

**Model calls.** The sample labels, the experience judge, the gate and its rewording step use the local
`claude` CLI with no tools and no saved session. To use another model, set `RSI_LLM_CMD` (see `lib/llm.py`). Only the scripts send real utterances
to a model, once each, and nothing about the call is stored.

**Limits to respect.** The Data API pages 500 rows at a time; `pull` spaces its calls and backs off on a
429. The Eval API allows about 120 requests a minute per org, one active run per user and 500 tasks per run.

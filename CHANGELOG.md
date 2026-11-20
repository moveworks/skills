# Changelog

Versions are per plugin. Tags look like `evals-v0.1.0`.

## evals 0.2.0

- New skill `rsi-loop`: a daily improvement loop for one assistant. It pulls the assistant's traffic with
  the Data API, draws a stratified sample (capability, experience, audience), rewrites each sampled
  conversation as an invented task with the same intent or drops it at a sensitivity gate, builds an Eval
  API task suite with tagged judges, runs it, and writes a daily report: the negative experiences seen, the
  pass rate per tag over time, and the changes proposed to fix them, each with Approve / Skip. It proposes
  changes and never applies them.
  - The gate's wording check is a hard step: no task may share 8 words in a row with its conversation or
    10 with any pulled message; such a task is reworded from the rewrite alone or dropped, and checked once
    more before anything is written. Explicit gate rules: internal initiative, project and roadmap content
    becomes invented content, not one-word swaps; unreleased roadmap content is dropped.
  - Good and poor experiences come from an explicit LLM judge (rubric and few-shots); the hard signals stay
    as features and the judge's agreement with them is reported.
  - `rsi.sh traces` plays back the conversation behind each failing or poor task (`mw chat-eval playback`,
    needs a Cockpit session) and the analysis names the turn and step where each went wrong.
  - `rsi.sh decide` records Approve / Skip; the report ends with Design notes.
  - Unit tests in `skills/rsi-loop/tests/`, run by `scripts/validate.sh`.
- `.gitignore` now covers the loop's work folder (`rsi-loop/`).

## evals 0.1.0 (renamed)

- The plugin is now named `evals` (was `eval-api`), so future eval skills can ship in the same plugin.
  Install with `/plugin install evals@moveworks-skills`.

## Marketplace rename

- The marketplace is now named `moveworks-skills` (was `moveworks`), so it no longer clashes with other
  marketplaces named `moveworks`. Install with `/plugin install evals@moveworks-skills`.

## eval-api 0.1.0 (first release)

- First release: guided Eval API walkthrough (prerequisites, eval config, test cases, dry run, run, report)
  for Claude Code and Codex.

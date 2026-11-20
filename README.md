# Moveworks developer plugins

Plugins for Claude Code and Codex that help you build on the Moveworks APIs. Your coding agent walks you
through the API, step by step, in your own environment.

| Plugin | What it does |
|---|---|
| [`evals`](plugins/evals/) | Evaluate and improve your Moveworks agents. Includes the [Eval API](https://docs.moveworks.com/api-reference/alpha-eval-api/overview) walkthrough (config, test cases, dry run, run, report) and the improvement loop (`rsi-loop`): an anonymized eval suite built from real traffic, run daily, with a report of what to fix |

## Install

### Claude Code

```
/plugin install evals --marketplace moveworks/skills
```

On Claude Code older than 2.1.275, add the marketplace first:

```
/plugin marketplace add moveworks/skills
/plugin install evals@moveworks-skills
```

### Codex

```
codex plugin marketplace add moveworks/skills
```

Then open the plugin browser in Codex, choose **Moveworks**, and install **evals**.

### Use it

Ask your agent: "Run a Moveworks eval on my agent." or "Run an improvement loop on my assistant."

### Updates

Fixes reach you when you refresh the marketplace:

- Claude Code: `/plugin marketplace update moveworks-skills`
- Codex: `codex plugin marketplace upgrade moveworks-skills`

## Requirements

`bash`, `curl` and `jq`, plus access to the Moveworks API you're using. The improvement loop also needs `python3`. Each plugin's README lists what it needs.

## Support

See [SUPPORT.md](SUPPORT.md). Report security issues privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE.txt)

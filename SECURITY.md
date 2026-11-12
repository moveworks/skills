# Security Policy

## Reporting a vulnerability

If you find a security issue in this repo (for example, a way a plugin could expose an access token or act
against your org without your confirmation), report it privately.

Email: `security@moveworks.com`

Do not open a public GitHub issue for security reports.

## Scope

This repo contains plugins for Claude Code and Codex that call the Moveworks APIs. Bugs in these plugins that
could leak tokens, send requests the user did not approve, or otherwise compromise a customer's org are in
scope.

Vulnerabilities in the Moveworks APIs themselves, or in your own integration, are handled through your
Moveworks account team and are out of scope for this repo.

## Disclaimer

This repository is provided as-is under its license. It is not a supported Moveworks product and carries no
SLA. The APIs these plugins use can be in alpha or limited availability and their behavior can change; verify
against the [official documentation](https://docs.moveworks.com/api-reference) before relying on them.

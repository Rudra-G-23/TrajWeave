# Security Policy

TrajWeave runs locally and reads coding-agent transcripts that may contain
sensitive material.

## What counts as a security issue

Report privately if you find any of the following:

- **Policy-write escape.** Stage 7 / Stage 9 writing to, or reading from, a path
  outside the registered repository or the approved global TrajWeave policy
  directory; following a symlink out of an approved location; or modifying human
  content outside a `trajweave:managed` block.
- **Redaction bypass.** Credentials, tokens, private keys, or other secrets that
  reach the database or any TrajWeave output without being replaced by
  `[REDACTED:...]`.
- **Unexpected code execution or file modification** triggered purely by
  importing or parsing a crafted transcript, or by running a read-only command
  (`trajweave show`, `trajweave trajectories`, the `trajweave ui` server, ...).
- **Write access through the read-only UI server**, or the local UI server
  being reachable from outside `localhost`.
- **Repository resolution confusion** that causes a repository the user never
  opted in to be parsed and stored.
- Path traversal, SQL injection, or unsafe deserialization anywhere in the
  ingest, storage, or apply paths.

For ordinary bugs and feature requests, use the
[issue tracker](https://github.com/Rudra-G-23/TrajWeave/issues).

## How to report

**Do not open a public GitHub issue for an exploitable vulnerability.**

Use GitHub's private vulnerability reporting:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability** (under "Advisories").
3. Fill in the private advisory form.

This keeps the report visible only to the maintainers until a fix is available.

## What a useful report contains

- A description of the issue and the impact you believe it has.
- The TrajWeave version (`trajweave --version`) and how it was installed.
- Your OS and Python version.
- Minimal steps to reproduce - ideally a **small, sanitized** synthetic
  transcript or command sequence. Do not attach real transcripts or real
  secrets; redact them or synthesize an equivalent.
- Any relevant log output (run with `-v`), with sensitive values removed.

## Disclosure

- We will acknowledge a valid report and keep you updated on progress.
- Please give us a reasonable window to release a fix before any public
  disclosure. We will credit reporters who want to be credited.

## Supported versions

TrajWeave is pre-1.0 (Development Status :: Alpha). Only the **latest released
`0.x` version** receives security fixes. There is no backport policy for older
`0.x` releases yet.

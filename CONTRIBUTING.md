# Contributing to rittenregistratie-bot

Thanks for your interest in improving the project! This document explains how to
get set up and what we expect from contributions.

## Ground rules

- Be respectful — see [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
- The **open-source core is a faithful recorder**: it stores each trip exactly
  as reported by the user (the distance is always the real odometer difference)
  and never fabricates, invents, or reclassifies trips. PRs that break this
  principle will not be merged.
- Keep the core dependency-light and Raspberry-Pi friendly.

## Development setup

```bash
git clone https://github.com/eduelias/rittenregistratie-bot.git
cd rittenregistratie-bot
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

## Making changes

1. Create a branch: `git checkout -b feat/short-description`.
2. Write code **and tests**. New behaviour must be covered.
3. Run the test suite: `pytest -q` (all green).
4. Keep commits focused with a clear message.
5. Open a pull request against `main`. Fill in the PR template.

`main` is protected: PRs require a passing CI run and review.

## Coding conventions

- Python 3.11+, standard library first; add dependencies sparingly.
- Follow the existing style (type hints, docstrings, small focused functions).
- Public behaviour changes should update the `README.md` / `docs/`.

## Writing plugins

The bot exposes four entry-point groups (odometer, trajectory, delta,
privatecap). See [`docs/plugins.md`](docs/plugins.md). Plugins live in their own
packages and register via `pyproject.toml` entry points — you do not need to
modify the core to add one.

## Reporting bugs / requesting features

Use the issue templates. Include your Python version, OS, and steps to
reproduce. Never paste secrets (tokens, phone numbers) into issues.

## Tax-compliance note

This is a record-keeping tool, not tax advice. Contributions must not present
the software as a guarantee of Belastingdienst acceptance.

# AGENTS.md

Before starting a development task, inspect the unfinished execution plans in
`docs/design-docs/exec-plans/`. Read the plans relevant to the task in full and
confirm their current goals, implementation order, constraints, and verification
requirements.

Use the following sources in this order when they disagree:

1. The user's current request.
2. The relevant unfinished execution plan.
3. The relevant design document under `docs/design-docs/`.
4. The terminology defined in `CONTEXT.md`.
5. The existing implementation and tests.

Treat an apparent conflict between the request and an execution plan or design
document as a decision that must be made explicitly. Explain the conflict and
confirm the intended approach before implementing a conflicting change.

When a task requires a new or substantially revised execution plan, follow
`PLANS.md`. A lightweight plan does not need to use the full ExecPlan format
unless the user requests it.

## Toolchain

- Python: `>=3.11,<3.12`.
- Use `uv` for dependency management, environment synchronization, and command
  execution. Do not use `pip` directly to modify the project environment.
- Dependencies are declared in `pyproject.toml` and locked in `uv.lock`.
- Tests use `pytest` and `pytest-asyncio`.
- The terminal UI uses Textual.
- Run project commands from the repository root unless the relevant execution
  plan says otherwise.

After changing dependencies, update the lock file with:

    uv lock

To synchronize the local environment with the lock file, use:

    uv sync

Do not edit `uv.lock` manually.

## Verification

Use the module form of pytest on Windows. In this repository,
`uv run pytest` may fail to import the local `miniagent` package, while the
following command works correctly:

    uv run python -m pytest -q

Run focused tests while developing, then run the full verification required by
the relevant execution plan.

Before completing a code change, run:

    uv run python -m compileall miniagent tests main.py
    uv run python -m pytest -q

Start the terminal application with:

    uv run python -m miniagent.ui

Tests must not depend on real network access, provider credentials, or a user's
real session data. Use fakes, `httpx.MockTransport`, temporary directories, and
Textual's test facilities where appropriate.

## Development Workflow

1. Inspect unfinished execution plans and read the plan relevant to the task.
2. Read only the design documents needed to establish the affected boundaries
   and invariants.
3. Inspect the existing implementation and tests before editing.
4. Keep the change within the current task and execution-plan scope.
5. Add or update tests for changed behavior.
6. Run focused verification during development, followed by the required full
   verification.
7. Update the active execution plan when its progress, decisions, discoveries,
   or acceptance evidence have materially changed.

## Change Discipline

- Preserve documented ownership boundaries and invariants. Do not bypass a
  public boundary merely because directly changing internal state is easier.
  abstractions.
- Do not silently resolve inconsistencies between code, tests, plans, and design
  documents. Record or report the discrepancy and resolve it at the appropriate
  authority level.
- Do not expose provider secrets, environment values, raw tool arguments, or
  user session contents in tests, logs, fixtures, or documentation.

## Agent skills

### Domain docs

This is a single-context repository. Read the root `CONTEXT.md` before changing
the domain model, and check `docs/adr/` for relevant architectural decisions.
See `docs/agents/domain.md`.

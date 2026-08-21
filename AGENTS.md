# Agent Instructions

## Workflow

- Present the plan or design and stop. Wait for explicit approval before
  writing code, editing config, or changing user-facing copy — regardless of
  how small the change is.
- Once approved, carry the task through to the end without asking again:
  implement, test, commit, then report the result.

## Guidelines

- Read the project README and any existing docs before making changes
- Run the project's build/test commands before committing (check package.json, Makefile, pyproject.toml, Cargo.toml)
- Keep changes minimal and focused on the task
- Prefer early returns over nested conditionals
- Handle error states explicitly
- Use semantic HTML and ARIA attributes for accessibility in frontend code
- Follow existing code style and conventions in the repo
- Do not introduce new dependencies without justification

## Verification

- Run linting and type checks before committing
- Run tests relevant to changed code
- Verify the build passes

## Git

- Write clear, concise commit messages
- Stage only files related to the current task
- Push to this fork's `main` directly; it is the working branch here
- Never push to another repository or open pull requests upstream

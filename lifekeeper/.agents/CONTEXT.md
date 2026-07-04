# Local Project Context & Secure Coding Standards

## Core Paved Roads
We systematically address common vulnerability classes by guiding the agent
to use our pre-configured, secure-by-default helper patterns instead of
writing raw implementation logic from scratch.

1. **Tool Input Validation**: Every agent tool must validate incoming
   parameters against strict Pydantic schemas rather than parsing raw
   dictionaries or strings.
2. **No Shell Execution**: Never use `run_command` or raw shell execution
   tools unless explicitly approved by `hooks.json`.
3. **Pre-Commit Remediation Loop**: If a git commit fails due to a pre-commit
   hook error (such as a Semgrep scan finding), you MUST treat the violation
   as a refactoring task, apply targeted fixes, run tests to verify no
   regressions, and attempt to commit again.

## TDD Planning Gate
During the Plan phase, you must decompose the workspace task into logical,
modular stages. Every implementation plan MUST include a dedicated
**Security Boundaries & Assertions** section outlining specific edge cases
that could exploit the feature.

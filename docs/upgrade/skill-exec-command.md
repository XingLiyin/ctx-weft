# Skill execution: full commands

`skill_executor__exec_script` now accepts one required argument, `command`.
Provider implementations must update their signature to
`exec_script(skill_name, command, ctx) -> str`; `script_path` and `args` are no
longer accepted. The task still selects the skill through `ctx.skill_name`.
Stored calls with the old arguments must be regenerated before execution.

The protocol requires the provider to make skill resources accessible and to
document their access mechanism. It does not prescribe a shell, working directory,
workspace selector, or resource lifetime. Session and task identity remain in
`ProviderContext`.

The bundled `ShellSkillCapabilityProvider` supplies shell execution with
`SKILL_DIR` pointing to the current skill root. Commands are passed through
unchanged, including interpreter selection, arguments, pipes, and redirections:

```python
# Windows cmd
exec_script(command='python "%SKILL_DIR%\\scripts\\render.py" --input input.csv')

# POSIX shell
exec_script(command='python "$SKILL_DIR/scripts/render.py" --input input.csv')
```

The catalog describes how to assign a skill. Core tool descriptions cover command invocation and resource reading without
prescribing shell syntax. Each skill provider may define `execution_instructions`
(default: empty). During skill loading, the assembler appends this text to the
loaded instructions; core adds no generic usage note. The shell provider supplies
SKILL_DIR guidance and the current platform's resource-path example.

Subclasses implement `borrow_skill_root(skill_name, ctx)` as an async context
manager. The base class holds the lease for the whole command, then releases it
on success, failure, or cancellation. `LocalSkillCapabilityProvider` borrows its
existing directory without deleting it. A host can instead download and extract
resources on entry and delete them on exit.

An injected `bash_runner` retains responsibility for workspace selection and
runtime setup. Without a runner, execution still uses the skill directory as cwd.
For direct execution, `python_executable` prepends its directory to PATH rather
than rewriting the command. Existing liveness limits and output handling remain.

`SKILL_DIR` is guaranteed only during the current call. Do not retain its resolved
absolute path across calls; write persistent outputs to the workspace using the
existing workspace access mechanism. File listing and resource reading APIs are
unchanged.

Subprocess output accepts UTF-8, legacy GBK and identifiable UTF-16 (a BOM or
aligned NUL/newline bytes). The ordinary and host Low runners share a stateful
decoder so byte-line reads do not split UTF-16 code units. Low shell command
wrapping also preserves quoted interpreter paths, including spaces and Unicode.

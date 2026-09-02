# Per-repo config

One JSON file per repo the debugger may work on, named `<repo>.json` to match the
ticket's `repo` field. `ticket.py triage` and `run-ticket.sh` read it to know where the
code lives, how to run its tests, and how to name fix branches.

Fields:

| Field | Meaning |
|---|---|
| `repo` | the ticket `repo` key this config serves |
| `workdir` | absolute path (or `~`-relative) to the repo the loop operates in |
| `test_command` | the command that runs the repo's full test suite (must exit non-zero on failure) |
| `default_branch` | the branch fix branches are cut from and diffed against |
| `branch_prefix` | prefix for per-ticket work branches, e.g. `fix/` gives `fix/<id>` |

`example.json` is a template. Copy it to `<repo>.json` and edit the values.

## Real configs are local-only

Only `example.json` is tracked. Every other `config/*.json` is gitignored, because a real
config names paths on one machine and the repos one person works on. Cross-repo
generality costs exactly one of these files plus the ticket's `repo` field; the CLI reads
them from disk, so gitignoring them changes nothing about how the debugger runs.

Set `DEBUGGER_CONFIG_DIR` to read configs from somewhere other than this folder.

## `snapshot.local.json`: where the status snapshot goes

`report.py --snapshot` writes a short markdown file meant to be embedded in whatever
dashboard you glance at (a notes app, a wiki page, a status board), and every `ticket.py`
mutation refreshes it. By default it lands at `run/debugger-status.md` with a plain
one-line header. To point it elsewhere and give it the frontmatter your dashboard
expects, create `config/snapshot.local.json` (also gitignored):

```json
{
  "path": "~/notes/dashboard/debugger-status.md",
  "header": [
    "---",
    "type: status",
    "---",
    "",
    "**Summary**: Machine-generated debugger queue snapshot. Do not edit by hand."
  ]
}
```

`header` may be a string or a list of lines and replaces the default header wholesale.
Either key may be omitted. `DEBUGGER_SNAPSHOT` (an explicit path) still wins over
`path`, and `DEBUGGER_SNAPSHOT_DISABLE=1` turns the write-through off entirely (the test
suite sets it). `DEBUGGER_SNAPSHOT_CONFIG` relocates the override file itself.

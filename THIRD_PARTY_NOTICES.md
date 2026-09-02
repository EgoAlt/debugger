# Third-party notices

## diagnosing-bugs (Matt Pocock)

`.claude/skills/diagnosing-bugs/` is a verbatim copy of the `diagnosing-bugs` skill from
Matt Pocock's skills collection, vendored so this repo debugs well out of the box with
nothing to install.

- Source: https://github.com/mattpocock/skills (path `skills/engineering/diagnosing-bugs/`)
- Version: `mattpocock-skills` 1.2.3, commit `84fdeffd12f2ee307994d1eb6feb48173b6e0502` (2026-08-06)
- Files copied: `SKILL.md`, `scripts/hitl-loop.template.sh`, `agents/openai.yaml`
- Not copied: the rest of the collection. The skill's closing hand-off to an
  `/improve-codebase-architecture` skill refers to a sibling skill in that collection,
  which this repo does not ship.
- License: MIT, reproduced below.

To update the vendored copy, replace the folder with the upstream version and bump the
version and commit here. Do not edit the files in place, so the copy stays verbatim.

```
MIT License

Copyright (c) 2026 Matt Pocock

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

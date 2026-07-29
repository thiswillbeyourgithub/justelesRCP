# Release notes

One directory per released version: `docs/changelog/<major.minor.patch>/changelog.md`.
`build.py` parses them all (`load_changelog`), compiles them into `dist/changelog.json`
and refuses to build when the current `__version__` has no notes, so a release can never
ship silently. `src/changelog.js` renders them in the "Quoi de neuf ?" popup.

Write for a casual reader (a nurse, a patient), not for a developer: what changed for
them, one short bullet each, no internal jargon. Not every version needs an entry, but
every version worth telling users about does.

Format:

```markdown
# 0.54.0 - 2026-07-29

## New features
- Shareable results page: /?q=sertraline lists every matching medicine. [47a9986]
  fr: Page de résultats partageable : /?q=sertraline liste tous les médicaments correspondants.
```

Rules the parser enforces:

- The title must be `# <version> - <YYYY-MM-DD>`, and the version must match the directory name. The date is the release date, shown in the popup.
- Only four category headings are allowed, and they are displayed in this order: `## New features`, `## Improvements`, `## Bug fixes`, `## Documentation`.
- Each bullet is the English line; the indented `fr:` line under it is the French text the site actually shows (the site is French, the docs are English). Both are required.
- A bullet may end with the commit sha(s) it comes from, in brackets: `[47a9986]` or `[47a9986, 224f699]`. The popup turns each one into a link to that commit on GitHub.

This project was built with the help of Claude Code.

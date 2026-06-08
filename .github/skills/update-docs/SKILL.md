---
name: update-docs
description: "Update README and CHANGELOG after changes to AFX_reinit.py. Use when: script changes have been made and docs need updating, after committing a fix or feature, updating readme, updating changelog, documenting changes."
---

# Update Docs

Updates `README.md` and `CHANGELOG.md` to reflect changes made to `AFX_reinit.py`.

## When to Use

- After any code change to `AFX_reinit.py` is committed
- When the user says "update readme", "update changelog", or "update docs"
- When new menu options, features, flags, or behaviors are added or renamed

---

## Conventions

### CHANGELOG.md

- File is at the repo root (`CHANGELOG.md`)
- Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
- Versioning uses internal script labels (`v2`, `v2a`, etc.), not SemVer
- New entries go under `## [Unreleased]` at the top
- Use sub-sections: `### Added`, `### Changed`, `### Fixed`, `### Removed`
- Each bullet is a **bold short title** followed by a period and a plain-English explanation
- Reference menu option numbers using current numbering (e.g., `5c`, not `4e`)
- Do NOT create a new dated version section — keep everything under `[Unreleased]` until the user explicitly cuts a release

### README.md

- File is at the repo root (`README.md`)
- The **"What's New in v2"** table lists major features — update it when a significant new feature is added
- The **"Overview"** bullet list describes what the script automates — update it when new automation capabilities are added
- Option references in the README use the current menu numbering (e.g., `5c` for config backup, not `4e`)
- Update the **"Updated:"** date field at the top when making doc changes
- The disclaimer block must never be modified

---

## Procedure

1. **Review the diff** — run `git diff HEAD~1 HEAD -- AFX_reinit.py` (or look at the recent commits) to understand what changed.

2. **Classify the changes**:
   - New feature / capability → `### Added` in CHANGELOG, consider updating README table/list
   - Behavior change / refactor → `### Changed` in CHANGELOG
   - Bug fix → `### Fixed` in CHANGELOG
   - Removed feature → `### Removed` in CHANGELOG

3. **Write the CHANGELOG entry** under `## [Unreleased]`:
   - Bold short title (e.g., `**Menu reorganization.**`)
   - One to three sentences explaining what changed and why
   - If a menu option was renumbered, note both old and new numbers

4. **Update README.md** if the change affects:
   - What the script automates (Overview bullet list)
   - A major feature (What's New in v2 table)
   - Option numbers referenced by name (e.g., `4e` → `5c`)
   - The "Updated:" date at the top

5. **Verify** no old option numbers remain in README (grep for `4c`, `4d`, `4e`, `4f`, `4g` as menu option references).

6. **Do not commit** the doc changes — leave that to the user or the `commit` skill.

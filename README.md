# skills

My personal library of [Claude Code skills](https://code.claude.com/docs/en/skills) — reusable, versioned expertise that Claude loads on demand.

## Purpose

A skill packages a proven way of working — a diagnostic method, a set of hard-won gotchas, runnable helper scripts — so Claude applies it consistently instead of rediscovering it (or missing it) every session. Most skills here are distilled from real debugging and build sessions: when a problem takes a day to crack, the method that cracked it gets captured as a skill so the next occurrence takes minutes.

This repo exists to:

- **Version them** — skills evolve as lessons accumulate; git history shows why each rule exists.
- **Sync them across machines** — clone + symlink and every workstation has the same toolbox.
- **Keep them testable** — each skill ships an `evals/` suite (built with Anthropic's skill-creator) so changes can be benchmarked against a no-skill baseline before they land.
- **Share them** — a skill folder is self-contained; anyone can copy one into their own `~/.claude/skills`.

## Layout

```
<domain>/<topic>/<skill-name>/
├── SKILL.md          # the skill: frontmatter (name + trigger description) + instructions
├── references/       # deep-dive docs the skill loads as needed
├── scripts/          # runnable helpers bundled with the skill
└── evals/            # test prompts + assertions used to benchmark the skill
```

## Installing

Claude Code discovers skills in `~/.claude/skills/<skill-name>` (user-global) or `<project>/.claude/skills/` (per-project). Symlink from this repo so it stays the single source of truth:

```bash
ln -s "$(pwd)/frontend/performance/nextjs-mobile-perf" ~/.claude/skills/nextjs-mobile-perf
```

(or `cp -r` for a plain copy — then re-copy after editing here).

## Catalog

| Skill | Path | What it does |
|---|---|---|
| nextjs-mobile-perf | `frontend/performance/nextjs-mobile-perf` | Diagnose and fix slow mobile page loads for Next.js — real-device WebKit measurement (RUM proxy + on-device bisection), WebKit renderer hazards (content-visibility, giant blurs), image alpha/priority traps, LAN-testing pitfalls. Benchmarked: 100% vs 70.5% assertion pass rate against a no-skill baseline. |

## Adding a new skill

```bash
cp -r ~/.claude/skills/<skill-name> <domain>/<topic>/
rm -rf ~/.claude/skills/<skill-name> && ln -s "$(pwd)/<domain>/<topic>/<skill-name>" ~/.claude/skills/<skill-name>
git add -A && git commit -m "Add <skill-name>" && git push
```

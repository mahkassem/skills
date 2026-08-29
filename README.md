# skills

Personal library of Claude Code skills, organized by domain:

```
<domain>/<topic>/<skill-name>/
└── SKILL.md          (+ references/, scripts/, evals/)
```

## Installing a skill

Claude Code discovers skills in `~/.claude/skills/<skill-name>` (user-global)
or `<project>/.claude/skills/` (per-project). Symlink from this repo so the
repo stays the single source of truth:

```bash
ln -s "$(pwd)/frontend/performance/nextjs-mobile-perf" ~/.claude/skills/nextjs-mobile-perf
```

(or `cp -r` if you prefer copies — then re-copy after editing here).

## Skills

| Skill | Path | What it does |
|---|---|---|
| nextjs-mobile-perf | `frontend/performance/nextjs-mobile-perf` | Diagnose and fix slow mobile page loads for Next.js — real-device WebKit measurement, RUM proxy + bisection, WebKit hazard fixes |

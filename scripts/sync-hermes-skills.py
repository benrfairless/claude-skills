#!/usr/bin/env python3
"""
sync-hermes-skills.py — Install claude-code-skills into Hermes Agent.

Hermes Agent (https://github.com/NousResearch/hermes-agent) discovers skills
from ~/.hermes/skills/. This script creates symlinks from our repo's skill
directories into Hermes's skill directory.

Both tools use the agentskills.io standard (SKILL.md with YAML frontmatter),
so no format conversion is needed — just symlink the directories.

IMPORTANT — flat layout (see issue #748): the agentskills.io discovery model
looks for a SKILL.md inside the IMMEDIATE children of each skills search path.
Mistral Vibe's SkillManager was confirmed to use ``Path.iterdir()`` (one level,
no recursion); the working Gemini/Codex syncs are likewise flat. An earlier
version of this script nested skills under
``~/.hermes/skills/claude-skills/<domain>/<skill>/``, which a one-level crawler
cannot see. We therefore place each skill ONE level deep with a ``claude-`` name
prefix for namespace safety. A flat layout is discoverable by both recursive and
non-recursive crawlers, so it is the universally-safe shape regardless of the
exact mechanism Hermes uses.

Usage:
    python scripts/sync-hermes-skills.py                   # full sync
    python scripts/sync-hermes-skills.py --verbose          # show each skill
    python scripts/sync-hermes-skills.py --domain engineering  # one domain
    python scripts/sync-hermes-skills.py --dry-run          # preview only
    python scripts/sync-hermes-skills.py --copy             # copy instead of symlink

Hermes skill directory: ~/.hermes/skills/
Our skills land at:      ~/.hermes/skills/claude-<skill-name>/  (flat, one level)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HERMES_SKILLS_DIR = Path.home() / ".hermes" / "skills"
NAME_PREFIX = "claude-"  # per-skill dir prefix; namespaces our skills vs Hermes built-ins
LEGACY_SUBDIR = "claude-skills"  # old nested namespace dir (issue #748); removed on sync
INDEX_FILENAME = "claude-skills-index.json"

# Domain directories that contain skills (each subdirectory with a SKILL.md)
DOMAIN_DIRS = [
    "engineering",
    "engineering-team",
    "product-team",
    "marketing-skill",
    "c-level-advisor",
    "project-management",
    "ra-qm-team",
    "business-growth",
    "finance",
    "productivity",  # v2.7.0 — capture, email-pair, reflect
    "marketing",     # v2.7.0 — landing (top-level, distinct from marketing-skill/)
    "research",      # v2.7.0 — pulse, litreview, grants, dossier, patent, syllabus, notebooklm, research orchestrator
    "business-operations",  # v2.8.0 — process-mapper, vendor-management, capacity-planner, internal-comms, knowledge-ops, procurement-optimizer + orchestrator
    "commercial",    # v2.8.0 — pricing-strategist, deal-desk, partnerships-architect, channel-economics, commercial-policy, rfp-responder, commercial-forecaster + orchestrator
    "research-ops",  # v2.9.0 — clinical-research, research-finance, market-research, product-research + orchestrator
    "compliance-os",  # ISO 13485/27001, SOC 2, GDPR, FDA QSR, EU AI Act audit-prep + orchestrator
]


def discover_skills(repo_root, domains=None):
    """Find all skills across specified domains.

    Supports three discovery patterns (same as sync-codex-skills.py):
      1. <domain>/<skill>/SKILL.md         — flat-domain pattern (legacy)
      2. <domain>/skills/<skill>/SKILL.md  — flat-with-skills-dir pattern (e.g., c-level-advisor/skills/)
      3. <domain>/<plugin>/skills/<skill>/SKILL.md — nested plugin pattern (e.g., research/research/skills/research/)

    Dedupes by SKILL.md path so a skill discovered under multiple patterns is only counted once.
    """
    skills = []
    seen_paths: set = set()
    search_domains = domains or DOMAIN_DIRS

    for domain in search_domains:
        domain_path = repo_root / domain
        if not domain_path.is_dir():
            continue

        # Pattern 2: <domain>/skills/<skill>/SKILL.md
        skills_subdir = domain_path / "skills"
        if skills_subdir.is_dir():
            for skill_dir in sorted(skills_subdir.iterdir()):
                if not skill_dir.is_dir():
                    continue
                skill_md = skill_dir / "SKILL.md"
                if skill_md.exists() and str(skill_md) not in seen_paths:
                    seen_paths.add(str(skill_md))
                    skills.append({
                        "domain": domain,
                        "name": skill_dir.name,
                        "source": skill_dir,
                        "skill_md": skill_md,
                    })

        # Pattern 1: <domain>/<skill>/SKILL.md (flat)
        # Pattern 3: <domain>/<plugin>/skills/<skill>/SKILL.md (nested plugin)
        for entry in sorted(domain_path.iterdir()):
            if not entry.is_dir() or entry.name in {"skills", ".claude-plugin", ".codex-plugin"}:
                continue

            # Pattern 1
            skill_md = entry / "SKILL.md"
            if skill_md.exists() and str(skill_md) not in seen_paths:
                seen_paths.add(str(skill_md))
                skills.append({
                    "domain": domain,
                    "name": entry.name,
                    "source": entry,
                    "skill_md": skill_md,
                })
                continue

            # Pattern 3: nested plugin with skills/ subdir
            nested_skills = entry / "skills"
            if not nested_skills.is_dir():
                continue
            for inner in sorted(nested_skills.iterdir()):
                if not inner.is_dir():
                    continue
                inner_skill_md = inner / "SKILL.md"
                if inner_skill_md.exists() and str(inner_skill_md) not in seen_paths:
                    seen_paths.add(str(inner_skill_md))
                    skills.append({
                        "domain": domain,
                        "name": inner.name,
                        "source": inner,
                        "skill_md": inner_skill_md,
                    })

    return skills


def read_frontmatter(skill_md):
    """Extract name and description from SKILL.md frontmatter."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        if not text.startswith("---"):
            return {}
        end = text.find("---", 3)
        if end < 0:
            return {}
        fm = {}
        for line in text[3:end].splitlines():
            if ":" in line and not line.strip().startswith("#"):
                k, _, v = line.partition(":")
                fm[k.strip()] = v.strip().strip("'\"")
        return fm
    except Exception:
        return {}


def assign_target_names(skills, prefix=NAME_PREFIX):
    """Assign each skill a unique, flat on-disk directory name.

    Hermes discovers skills via the agentskills.io one-level model, so every
    skill must be an immediate child of the skills dir. The displayed skill name
    comes from SKILL.md frontmatter, not this directory name — so the dir name
    only has to be unique on disk. We prefix with ``claude-`` and fall back to a
    domain-qualified name (then a numeric suffix) on collision.
    """
    used: set = set()
    for s in skills:
        candidate = f"{prefix}{s['name']}"
        if candidate in used:
            candidate = f"{prefix}{s['domain']}-{s['name']}"
        n = 2
        base = candidate
        while candidate in used:
            candidate = f"{base}-{n}"
            n += 1
        used.add(candidate)
        s["target_name"] = candidate
    return skills


def sync_skill(skill, target_root, use_copy, verbose, dry_run):
    """Create a symlink or copy for one skill (flat, one level deep)."""
    target = target_root / skill["target_name"]

    if target.exists() or target.is_symlink():
        if verbose:
            print(f"  skip (exists): {skill['target_name']}")
        return "skip"

    if dry_run:
        if verbose:
            print(f"  would {'copy' if use_copy else 'link'}: {skill['target_name']}")
        return "would"

    target.parent.mkdir(parents=True, exist_ok=True)

    if use_copy:
        shutil.copytree(skill["source"], target, dirs_exist_ok=True)
    else:
        # Prefer relative symlinks so the tree is portable when committed to the repo.
        # Falls back to absolute if target is outside the source tree (e.g., ~/.hermes/).
        try:
            rel = os.path.relpath(skill["source"], target.parent)
            target.symlink_to(rel)
        except ValueError:
            # Cross-device or unrelated tree — use absolute
            target.symlink_to(skill["source"])

    if verbose:
        print(f"  {'copied' if use_copy else 'linked'}: {skill['target_name']}")
    return "new"


def write_index(target_root, skills):
    """Write a skills-index.json for quick lookup."""
    index = {
        "source": "claude-code-skills",
        "total_skills": len(skills),
        "domains": {},
    }
    for s in skills:
        d = s["domain"]
        if d not in index["domains"]:
            index["domains"][d] = []
        fm = read_frontmatter(s["skill_md"])
        index["domains"][d].append({
            "name": s["name"],
            "description": fm.get("description", ""),
            "path": s["target_name"],
        })
    index_path = target_root / INDEX_FILENAME
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index_path


def main():
    p = argparse.ArgumentParser(
        description="Sync claude-code-skills into Hermes Agent (~/.hermes/skills/).",
        epilog="Both tools use the agentskills.io SKILL.md standard. No format conversion needed.",
    )
    p.add_argument(
        "--domain",
        default=None,
        help="Sync only one domain (e.g. engineering, marketing-skill)",
    )
    p.add_argument("--verbose", action="store_true", help="Show each skill")
    p.add_argument("--dry-run", action="store_true", help="Preview only, don't create files")
    p.add_argument("--copy", action="store_true", help="Copy files instead of symlink")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument(
        "--target",
        default=str(HERMES_SKILLS_DIR),
        help=f"Override Hermes skills dir (default: {HERMES_SKILLS_DIR})",
    )
    args = p.parse_args()

    target_root = Path(args.target).expanduser()
    domains = [args.domain] if args.domain else None
    skills = discover_skills(REPO_ROOT, domains)

    if not skills:
        msg = f"No skills found in {REPO_ROOT}"
        if args.json:
            print(json.dumps({"status": "error", "message": msg}))
        else:
            print(f"[error] {msg}", file=sys.stderr)
        sys.exit(1)

    assign_target_names(skills)

    if not args.dry_run:
        target_root.mkdir(parents=True, exist_ok=True)
        # Migrate away from the old nested layout (issue #748): remove the legacy
        # ~/.hermes/skills/claude-skills/ namespace dir that a one-level crawler
        # could not discover.
        legacy = target_root / LEGACY_SUBDIR
        if legacy.is_dir():
            shutil.rmtree(legacy, ignore_errors=True)

    counts = {"new": 0, "skip": 0, "would": 0}
    for s in skills:
        result = sync_skill(s, target_root, args.copy, args.verbose, args.dry_run)
        counts[result] += 1

    # Write index
    if not args.dry_run:
        idx_path = write_index(target_root, skills)
    else:
        idx_path = target_root / INDEX_FILENAME

    summary = {
        "status": "ok",
        "target": str(target_root),
        "total_skills": len(skills),
        "new": counts["new"],
        "skipped": counts["skip"],
        "dry_run": args.dry_run,
        "mode": "copy" if args.copy else "symlink",
        "index": str(idx_path),
        "domains": list({s["domain"] for s in skills}),
    }

    if args.json:
        print(json.dumps(summary, indent=2))
        return

    action = "Would sync" if args.dry_run else "Synced"
    print(f"{action} {len(skills)} skills to {target_root}")
    print(f"  New: {counts['new']}  Skipped: {counts['skip']}")
    print(f"  Mode: {'copy' if args.copy else 'symlink'}")
    if not args.dry_run:
        print(f"  Index: {idx_path}")
    print()
    print("Hermes will discover these skills via /skills or /<skill-name>.")
    print("No format conversion needed — both tools use agentskills.io SKILL.md standard.")


if __name__ == "__main__":
    main()

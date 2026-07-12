#!/usr/bin/env python3
"""Run-once vault profiler (milestone M2).

Traverses the vault to map its folder structure, takes a stratified random sample of
markdown files, stages that profile, and invokes the agent loop (custom_agent_loop.py,
same OpenAI-compatible endpoint as the nightly filing step) to synthesize
/vault/Filing_Rules.md: a generated manifest of the vault's folder taxonomy,
daily-note format, tag vocabulary, linking style, and create-vs-update rule.

Usage (from the host):
    docker compose run --rm pipeline python3 scripts/profile_vault.py
    docker compose run --rm pipeline python3 scripts/profile_vault.py --profile-only
    docker compose run --rm pipeline python3 scripts/profile_vault.py --force
    docker compose run --rm pipeline python3 scripts/profile_vault.py --profile-only \\
        --priority-folders "Work,Technology,Home Lab" --priority-multiplier 3 \\
        --exclude-dirs "attachments,smart-chats"
"""
import argparse
import json
import os
import random
import subprocess
import sys

from tzutil import now_local

EXCLUDE_DIR_PREFIXES = (".",)  # .obsidian, .trash, .git, etc.
EXCLUDE_FILENAMES = {"Filing_Rules.md"}
DEFAULT_EXCLUDE_DIR_NAMES = {"attachments"}  # not useful for inferring filing conventions


def env(name, default=None):
    return os.environ.get(name, default)


def build_tree_and_candidates(vault_dir, exclude_dir_names):
    """Walk the vault. Returns (tree_lines, candidates) where candidates is a list of
    (relative_path, absolute_path) for markdown files eligible for sampling."""
    tree_lines = []
    candidates = []

    for root, dirs, files in os.walk(vault_dir):
        dirs[:] = sorted(
            d for d in dirs
            if not d.startswith(EXCLUDE_DIR_PREFIXES) and d.lower() not in exclude_dir_names
        )
        rel_root = os.path.relpath(root, vault_dir)
        md_files = sorted(f for f in files if f.endswith(".md"))

        if rel_root != ".":
            depth = rel_root.count(os.sep)
            indent = "  " * depth
            tree_lines.append(f"{indent}- {rel_root}/ ({len(md_files)} files)")

        for fname in md_files:
            rel_path = fname if rel_root == "." else os.path.join(rel_root, fname)
            if fname in EXCLUDE_FILENAMES:
                continue
            candidates.append((rel_path, os.path.join(root, fname)))

    return tree_lines, candidates


def is_priority_folder(folder, priority_names):
    if not priority_names:
        return False
    segments = {seg.lower() for seg in folder.split(os.sep)}
    return bool(segments & priority_names)


def stratified_sample(candidates, per_folder, max_total, priority_names, priority_multiplier):
    by_folder = {}
    for rel_path, abs_path in candidates:
        folder = os.path.dirname(rel_path) or "."
        by_folder.setdefault(folder, []).append((rel_path, abs_path))

    # Guaranteed floor: one file per top-level area, so a small folder (e.g. Finance,
    # Car) is never fully invisible to the agent even after the global cap below.
    by_top_level = {}
    for rel_path, abs_path in candidates:
        top_level = rel_path.split(os.sep, 1)[0]
        by_top_level.setdefault(top_level, []).append((rel_path, abs_path))

    floor_sampled = [random.choice(files) for files in by_top_level.values()]
    floor_set = set(floor_sampled)

    weighted_pool = []
    for folder, files in by_folder.items():
        folder_quota = per_folder
        if is_priority_folder(folder, priority_names):
            folder_quota = round(per_folder * priority_multiplier)
        k = min(folder_quota, len(files))
        weighted_pool.extend(random.sample(files, k))

    remaining_budget = max(0, max_total - len(floor_sampled))
    fill_candidates = [pair for pair in weighted_pool if pair not in floor_set]
    if len(fill_candidates) > remaining_budget:
        fill_candidates = random.sample(fill_candidates, remaining_budget)

    sampled = floor_sampled + fill_candidates
    sampled.sort(key=lambda pair: pair[0])
    return sampled


def read_truncated(abs_path, max_chars):
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError as exc:
        return f"[could not read file: {exc}]"

    if len(content) > max_chars:
        return content[:max_chars] + f"\n... [truncated, {len(content)} chars total]"
    return content


def write_profile(staging_dir, tree_lines, sampled, total_candidates, max_chars):
    os.makedirs(staging_dir, exist_ok=True)
    profile_path = os.path.join(staging_dir, "vault_profile.md")

    lines = [
        f"# Vault Profile (generated {now_local().isoformat()})",
        "",
        "## Folder tree",
        *tree_lines,
        "",
        f"## Sampled files ({len(sampled)} of {total_candidates} total)",
        "",
    ]
    for rel_path, abs_path in sampled:
        lines.append(f"### {rel_path}")
        lines.append("```")
        lines.append(read_truncated(abs_path, max_chars))
        lines.append("```")
        lines.append("")

    with open(profile_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return profile_path


def run_agent(vault_dir, staging_dir, log_dir):
    args = [
        "python3", "/app/scripts/custom_agent_loop.py",
        "/app/prompt_vault_profile.txt"
    ]
    log_path = os.path.join(log_dir, f"vault_profile.{now_local().strftime('%Y-%m-%d_%H%M%S')}.json")

    os.makedirs(log_dir, exist_ok=True)

    with open(log_path, "w", encoding="utf-8") as log_file:
        result = subprocess.run(args, cwd=vault_dir, stdout=log_file, stderr=subprocess.STDOUT)

    return result.returncode, log_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples-per-folder", type=int, default=3,
                         help="Base number of files to sample per folder.")
    parser.add_argument("--max-samples", type=int, default=200,
                         help="Hard cap on total files sampled across the vault. Raise this "
                              "for larger vaults (a few hundred is fine for most LLM context "
                              "windows at the default --max-chars-per-file); lower it if the "
                              "profiler starts hitting the model's context limit.")
    parser.add_argument("--max-chars-per-file", type=int, default=800)
    parser.add_argument("--priority-folders", default="Work,Technology,Home Lab",
                         help="Comma-separated folder names (matched against any path "
                              "segment, case-insensitive) that get boosted sampling - "
                              "e.g. technical/work folders where filing conventions "
                              "matter most. Pass '' to disable.")
    parser.add_argument("--priority-multiplier", type=float, default=3.0,
                         help="Sample-count multiplier applied to priority folders.")
    parser.add_argument("--exclude-dirs", default=",".join(sorted(DEFAULT_EXCLUDE_DIR_NAMES)),
                         help="Comma-separated folder names to skip entirely (case-insensitive).")
    parser.add_argument("--profile-only", action="store_true",
                         help="Only build the staged profile; skip the agent synthesis step.")
    parser.add_argument("--force", action="store_true",
                         help="Overwrite an existing Filing_Rules.md.")
    args = parser.parse_args()

    priority_names = {name.strip().lower() for name in args.priority_folders.split(",") if name.strip()}
    exclude_dir_names = {name.strip().lower() for name in args.exclude_dirs.split(",") if name.strip()}

    vault_dir = env("VAULT_DIR", "/vault")
    staging_dir = env("STAGING_DIR", "/work/staging")
    log_dir = env("LOG_DIR", "/work/logs")
    filing_rules_path = os.path.join(vault_dir, "Filing_Rules.md")

    if not args.profile_only and os.path.exists(filing_rules_path) and not args.force:
        sys.exit(
            f"{filing_rules_path} already exists. This is a run-once tool - "
            "pass --force to regenerate it."
        )

    tree_lines, candidates = build_tree_and_candidates(vault_dir, exclude_dir_names)
    if not candidates:
        sys.exit(f"No markdown files found under {vault_dir}. Is the vault synced yet?")

    sampled = stratified_sample(
        candidates, args.samples_per_folder, args.max_samples,
        priority_names, args.priority_multiplier,
    )
    profile_path = write_profile(staging_dir, tree_lines, sampled, len(candidates), args.max_chars_per_file)

    print(f"Profiled {len(candidates)} markdown files across the vault, sampled {len(sampled)}.")
    print(f"Staged profile written to {profile_path}")

    if args.profile_only:
        print("--profile-only set: skipping agent synthesis. Inspect the staged profile above.")
        return

    # Capture the pre-run mtime (if the file already exists, e.g. a --force
    # regeneration) so we can verify the agent actually wrote something new,
    # rather than trusting os.path.exists() alone - which is true even when the
    # agent silently failed (e.g. hit a sandbox "Access denied" error, ignored it,
    # and called finish anyway) and left a stale, older Filing_Rules.md in place.
    mtime_before = os.path.getmtime(filing_rules_path) if os.path.exists(filing_rules_path) else None

    print("Invoking agent to synthesize Filing_Rules.md...")
    rc, log_path = run_agent(vault_dir, staging_dir, log_dir)

    if rc != 0:
        sys.exit(f"Agent failed (exit {rc}). See {log_path}")

    if not os.path.exists(filing_rules_path):
        sys.exit(f"ERROR: agent exited 0 but {filing_rules_path} was not created. See {log_path}")

    mtime_after = os.path.getmtime(filing_rules_path)
    if mtime_before is not None and mtime_after <= mtime_before:
        sys.exit(
            f"ERROR: agent exited 0 but {filing_rules_path} was not modified (same "
            f"mtime as before the run). It likely hit a tool error and gave up "
            f"anyway - check {log_path} for '!! ... returned an error' lines."
        )

    print(f"Filing_Rules.md written to {filing_rules_path}")
    print(f"Full log: {log_path}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Pre-commit check for leaked API key patterns (§7.2).

Scans staged changes for strings matching common key patterns:
- sk- (OpenAI, Stripe)
- AIza (Google)
- ghp_ / gho_ (GitHub)
- xoxb- / xoxp- (Slack)
- AKIA (AWS)

Usage:
    python scripts/check_secrets.py          # check staged files
    python scripts/check_secrets.py FILE     # check specific file
"""

from __future__ import annotations

import re
import subprocess
import sys

# Patterns that look like API keys or secrets
KEY_PATTERNS = [
    (r"sk-[a-zA-Z0-9]{20,}", "OpenAI/Stripe API key"),
    (r"AIza[a-zA-Z0-9_-]{35}", "Google API key"),
    (r"ghp_[a-zA-Z0-9]{36}", "GitHub personal access token"),
    (r"gho_[a-zA-Z0-9]{36}", "GitHub OAuth token"),
    (r"xoxb-[a-zA-Z0-9-]+", "Slack bot token"),
    (r"xoxp-[a-zA-Z0-9-]+", "Slack user token"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key ID"),
    (r"(?i)whatsapp_api_token\s*=\s*['\"][^'\"]{20,}", "WhatsApp API token in code"),
    (r"(?i)app_secret\s*=\s*['\"][^'\"]{20,}", "App secret in code"),
]

# Files to skip (binary, generated, etc.)
SKIP_PATTERNS = [
    r"\.pyc$",
    r"__pycache__",
    r"\.egg-info",
    r"node_modules",
    r"\.git/",
    r"\.freebuff/",
]


def get_staged_files() -> list[str]:
    """Get list of files staged for commit."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            check=True,
        )
        return [f.strip() for f in result.stdout.splitlines() if f.strip()]
    except subprocess.CalledProcessError:
        return []


def get_staged_diff(file_path: str) -> str:
    """Get the staged diff content for a file."""
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--", file_path],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return ""


def scan_content(content: str, filename: str) -> list[tuple[str, str, int]]:
    """Scan content for key patterns. Returns list of (pattern_desc, match, line_no)."""
    findings = []
    for line_no, line in enumerate(content.splitlines(), 1):
        # Skip diff metadata lines
        if line.startswith(("+++", "---", "@@", "diff ", "index ")):
            continue
        # Skip removed lines (only check added content)
        if line.startswith("-") and not line.startswith("--"):
            continue

        for pattern, desc in KEY_PATTERNS:
            matches = re.findall(pattern, line)
            for match in matches:
                findings.append((desc, match, line_no))
    return findings


def main() -> int:
    files = sys.argv[1:] if len(sys.argv) > 1 else get_staged_files()
    if not files:
        print("No files to check.")
        return 0

    total_findings = 0
    for file_path in files:
        # Skip non-text files
        if any(re.search(p, file_path) for p in SKIP_PATTERNS):
            continue

        diff = get_staged_diff(file_path) if len(sys.argv) <= 1 else ""
        if not diff:
            continue

        findings = scan_content(diff, file_path)
        for desc, match, line_no in findings:
            # Mask the key for display
            masked = match[:6] + "..." + match[-4:] if len(match) > 10 else "***"
            print(f"⚠️  {file_path}:{line_no} — {desc}: {masked}")
            total_findings += 1

    if total_findings > 0:
        print(f"\n❌ Found {total_findings} potential secret(s) in staged changes.")
        print("If these are intentional (e.g. test fixtures), add a # nosec comment.")
        return 1

    print("✅ No secrets found in staged changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

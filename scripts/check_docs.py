#!/usr/bin/env python3
"""Check module-based documentation, local Markdown links and preserved evidence."""

from __future__ import annotations

import html
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"!?\[[^\]\n]*\]\((<[^>\n]+>|[^)\n]+)\)")
HISTORICAL_PATH = re.compile(r"`(docs/多端联调_\d{8}_[^`]+\.md)`")


def prose(text: str) -> str:
    """Leave fenced examples out of link and heading validation."""
    lines = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker.group(1)
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            lines.append("")
        else:
            lines.append(line if fence is None else "")
    return "\n".join(lines)


def anchors(text: str) -> set[str]:
    body = prose(text)
    result = set(re.findall(r'<[^>]+\b(?:id|name)=["\']([^"\']+)["\']', body))
    counts: Counter[str] = Counter()
    for heading in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", body, re.MULTILINE):
        heading = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", heading)
        heading = html.unescape(re.sub(r"<[^>]*>", "", heading))
        slug = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
        index = counts[slug]
        result.add(slug if index == 0 else f"{slug}-{index}")
        counts[slug] += 1
    return result


def check(root: Path = ROOT) -> tuple[list[str], int, int]:
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z", "*.md"], cwd=root
    ).decode().split("\0")
    paths = {root / name for name in tracked if name}
    paths.update((root / "docs").rglob("*.md"))
    paths = {p for p in paths if p.is_file()}
    contents = {p.resolve(): p.read_text(encoding="utf-8") for p in paths}
    errors: list[str] = []
    links = 0
    preserved = set()
    index = (root / "docs/README.md").read_text(encoding="utf-8")
    indexed = {
        unquote(urlsplit(target.strip("<>")).path)
        for target in LINK.findall(prose(index))
    }
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        text = contents[path.resolve()]
        if path.is_relative_to(root / "docs") and path.name.startswith("多端联调_"):
            if path.parent != root / "docs":
                errors.append(f"{relative}: 联调正文按模块平铺，不新建每日归档目录")
            if re.search(r"\d{4}[-_]?\d{2}[-_]?\d{2}", path.stem):
                errors.append(f"{relative}: 联调按模块持续更新，文件名不得含日期")
            if path.name not in indexed:
                errors.append(f"{relative}: 未加入 docs/README.md 模块索引")
            original_paths = HISTORICAL_PATH.findall(text)
            if original_paths:
                commit = re.search(r"Git 提交 `([0-9a-f]{7,40})`", text)
                if not commit:
                    errors.append(f"{relative}: 历史来源缺少固定 Git 提交")
                else:
                    preserved.update((commit.group(1), name) for name in original_paths)
        for target in LINK.findall(prose(text)):
            url = urlsplit(target.strip("<>"))
            if url.scheme or url.netloc:
                continue
            destination = (path.parent / unquote(url.path)).resolve() if url.path else path.resolve()
            links += 1
            if not destination.exists():
                errors.append(f"{relative}: 链接目标不存在：{target}")
            elif url.fragment and destination.suffix == ".md":
                body = contents.get(destination)
                if body is None:
                    body = destination.read_text(encoding="utf-8")
                if unquote(url.fragment) not in anchors(body):
                    errors.append(f"{relative}: 章节锚点不存在：{target}")
    for commit, path in sorted(preserved):
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}:{path}"], cwd=root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if result.returncode:
            errors.append(f"历史原文无法读取：{commit}:{path}；请确认已获取该提交")
    return errors, links, len(preserved)


def main() -> int:
    errors, links, preserved = check()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Docs checks passed: {links} local links/anchors, {preserved} preserved sources.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

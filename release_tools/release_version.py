#!/usr/bin/env python3
"""发布版本计算：VERSION 优先；已发布则递增，不允许版本倒退。"""
import os
import re
import subprocess
from pathlib import Path

_PATTERN = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")


def parse_version(value):
    match = _PATTERN.fullmatch(value.strip())
    if not match:
        raise ValueError("版本号格式应为 1.2.3 或 v1.2.3")
    return tuple(int(part) for part in match.groups())


def choose_version(current, tags, requested=""):
    current_tuple = parse_version(current)
    published = set()
    for tag in tags:
        if not tag.startswith("v"):
            continue
        try:
            published.add(parse_version(tag))
        except ValueError:
            continue
    highest = max(published, default=(0, 0, 0))
    if requested.strip():
        chosen = parse_version(requested)
        if chosen in published:
            raise ValueError("目标标签已存在，请换一个版本号")
        if chosen < max(current_tuple, highest):
            raise ValueError("发布版本不能低于 VERSION 或已发布版本")
    else:
        chosen = max(current_tuple, highest)
        if chosen in published:
            chosen = (chosen[0], chosen[1], chosen[2] + 1)
    return ".".join(map(str, chosen))


def main():
    current = Path("VERSION").read_text(encoding="utf-8").strip()
    tags = subprocess.check_output(["git", "tag", "--list"], text=True).splitlines()
    version = choose_version(current, tags, os.environ.get("RELEASE_VERSION", ""))
    Path("VERSION").write_text(version + "\n", encoding="utf-8")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"version={version}\ntag=v{version}\n")
    print(f"发布版本：v{version}；VERSION 已同步为 {version}")


if __name__ == "__main__":
    main()

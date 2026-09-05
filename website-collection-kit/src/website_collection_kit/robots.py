"""Robots Exclusion Protocol parsing and sitemap provenance."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit


DEFAULT_PRODUCT_TOKEN = "website-collection-kit"


class RobotsState(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNREACHABLE = "unreachable"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class RobotsRules:
    """Rules selected for one crawler product token."""

    disallow: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    sitemaps: tuple[str, ...] = ()
    state: RobotsState = RobotsState.AVAILABLE

    @classmethod
    def unavailable(cls) -> RobotsRules:
        return cls(state=RobotsState.UNAVAILABLE)

    @classmethod
    def unreachable(cls) -> RobotsRules:
        return cls(state=RobotsState.UNREACHABLE)

    @classmethod
    def invalid(cls) -> RobotsRules:
        return cls(state=RobotsState.INVALID)

    def allows(self, url: str) -> bool:
        if self.state is RobotsState.UNREACHABLE or self.state is RobotsState.INVALID:
            return False
        path = urlsplit(url).path or "/"
        query = urlsplit(url).query
        if query:
            path += "?" + query
        matches = [
            (len(rule), False)
            for rule in self.disallow
            if robots_rule_matches(rule, path)
        ]
        matches.extend(
            (len(rule), True)
            for rule in self.allow
            if robots_rule_matches(rule, path)
        )
        if not matches:
            return True
        _, allowed = max(matches, key=lambda value: (value[0], value[1]))
        return allowed


@dataclass
class _Group:
    agents: list[str]
    disallow: list[str]
    allow: list[str]
    has_rules: bool = False


def parse_robots(
    body: bytes, *, product_token: str = DEFAULT_PRODUCT_TOKEN
) -> RobotsRules:
    """Parse robots groups and select all groups matching the product token."""

    if not isinstance(product_token, str) or not product_token.strip():
        raise ValueError("product_token must be a non-empty string")
    groups: list[_Group] = []
    sitemaps: list[str] = []
    current: _Group | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and current.agents:
            groups.append(current)
        current = None

    text = body.decode("utf-8", errors="replace")
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            flush()
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if current is None or current.has_rules:
                flush()
                current = _Group([], [], [])
            current.agents.append(value.lower())
        elif key == "disallow" and current is not None:
            current.has_rules = True
            if value:
                current.disallow.append(value)
        elif key == "allow" and current is not None:
            current.has_rules = True
            if value:
                current.allow.append(value)
        elif key == "sitemap" and value:
            sitemaps.append(value)
    flush()

    token = product_token.strip().lower()
    matching = [group for group in groups if token in group.agents]
    if not matching:
        matching = [group for group in groups if "*" in group.agents]
    return RobotsRules(
        disallow=tuple(
            dict.fromkeys(rule for group in matching for rule in group.disallow)
        ),
        allow=tuple(dict.fromkeys(rule for group in matching for rule in group.allow)),
        sitemaps=tuple(dict.fromkeys(sitemaps)),
        state=RobotsState.AVAILABLE,
    )


def robots_rule_matches(rule: str, path: str) -> bool:
    end_anchored = rule.endswith("$")
    expression = rule[:-1] if end_anchored else rule
    expression = re.escape(expression).replace(r"\*", ".*")
    suffix = "$" if end_anchored else ""
    return re.match(r"^" + expression + suffix, path) is not None

"""Conservative extraction of explicit prerequisite sections from sanitized Jira text.

Only named sections declare edges; unrelated references never become dependencies.
The authenticated inventory adapter still fetches every external dependency status.
"""

import re


def declared_dependencies(text):
    keys = set()
    active = False
    for raw in str(text).splitlines():
        line = re.sub(r"\*\*(.*?)\*\*", r"\1", raw.strip())
        header = re.match(
            r"^(?:#{1,6}\s*)?(?:prerequisites?|dependencies|depends on|blocked by)(?:\s*[:–—]\s*|\s+-\s+|\s*$)(.*)$",
            line,
            re.I,
        )
        if header:
            active = True
            line = header.group(1)
        elif not line:
            active = False
        elif not active or not re.match(r"^(?:[-*•]|\d+[.)])\s", line):
            active = False
        if active:
            keys.update(re.findall(r"\b[A-Z][A-Z0-9_]*-[1-9][0-9]*\b", line))
    return sorted(keys)

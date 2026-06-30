"""Security scanning for skills and external MCP tools (Phase B / G7).

Third-party skills and MCP tool definitions are an attack surface: a "skill" is
text that steers the model, and an MCP tool description is attacker-controlled
text that gets injected into the prompt and may carry hidden instructions (tool
poisoning). This module statically scans that text for dangerous patterns and
returns a graded :class:`ScanReport`, so the caller can **gate** install/use of
risky content the same way the broker gates risky actions.

It also wraps the public OSV.dev API to flag known-vulnerable dependencies.

Stdlib-only. Detection is heuristic + signature based (no model calls), so it is
fast, deterministic, offline for the static path, and unit-testable. It is a
*defense-in-depth* layer, not a guarantee — paired with the broker (consequential
actions still need approval) and content_guard (untrusted output quarantine).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# severity weights: a single CRITICAL fails the scan; MEDIUMs accumulate.
_SEV_SCORE = {"critical": 100, "high": 40, "medium": 15, "low": 5}
_FAIL_THRESHOLD = 40  # >= this total score (or any critical) => not clean


@dataclass
class Finding:
    severity: str
    rule: str
    detail: str
    excerpt: str = ""


@dataclass
class ScanReport:
    target: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def score(self) -> int:
        return sum(_SEV_SCORE.get(f.severity, 0) for f in self.findings)

    @property
    def clean(self) -> bool:
        if any(f.severity == "critical" for f in self.findings):
            return False
        return self.score < _FAIL_THRESHOLD

    @property
    def grade(self) -> str:
        if any(f.severity == "critical" for f in self.findings):
            return "F"
        s = self.score
        if s == 0:
            return "A"
        if s <= 15:
            return "B"
        if s < _FAIL_THRESHOLD:
            return "C"
        return "D"

    def summary(self) -> str:
        if not self.findings:
            return f"{self.target}: clean (grade A)"
        by = ", ".join(f"{f.severity}:{f.rule}" for f in self.findings[:6])
        return (f"{self.target}: grade {self.grade} score={self.score} "
                f"clean={self.clean} [{by}]")


# (severity, rule, compiled regex, human detail)
_PATTERNS: list[tuple[str, str, re.Pattern, str]] = [
    ("critical", "shell_pipe_exec",
     re.compile(r"(curl|wget)\s+[^\n|]*\|\s*(sh|bash|zsh|python)", re.I),
     "downloads and pipes remote content straight into an interpreter"),
    ("critical", "reverse_shell",
     re.compile(r"(bash\s+-i\s+>&|/dev/tcp/|nc\s+-e|ncat\s+-e|socat\s+.*exec)", re.I),
     "reverse-shell / remote-exec pattern"),
    ("critical", "secret_exfil",
     re.compile(r"(AWS_SECRET|API_KEY|PRIVATE_KEY|\.ssh/id_|/etc/shadow|"
                r"\.aws/credentials|\.env)[^\n]{0,40}(curl|wget|requests\.|http)", re.I),
     "reads a secret/credential file and sends it over the network"),
    ("high", "dangerous_eval",
     re.compile(r"\b(eval|exec)\s*\(\s*(base64|bytes\.fromhex|codecs\.decode|"
                r"__import__)", re.I),
     "executes obfuscated/decoded code"),
    ("high", "destructive_fs",
     re.compile(r"rm\s+-rf\s+(/|~|\$HOME|/\*)|shutil\.rmtree\(\s*['\"]?/", re.I),
     "destructive recursive delete of a root/home path"),
    ("high", "prompt_injection_directive",
     re.compile(r"ignore (all )?(previous|prior|above) (instructions|prompts)|"
                r"disregard (your )?(system|previous)|you are now (DAN|jailbroken)|"
                r"reveal (your )?(system prompt|instructions)", re.I),
     "embedded prompt-injection / jailbreak directive"),
    ("high", "hidden_tool_instruction",
     re.compile(r"<important>|do not tell the user|without (informing|telling) "
                r"the user|secretly (run|call|use)", re.I),
     "hidden instruction to act behind the user's back (tool poisoning)"),
    ("medium", "obfuscated_blob",
     re.compile(r"(base64\.b64decode|fromCharCode|\\x[0-9a-f]{2}(\\x[0-9a-f]{2}){8,})", re.I),
     "obfuscated payload (encoded/escaped blob)"),
    ("medium", "raw_ip_url",
     re.compile(r"https?://\d{1,3}(\.\d{1,3}){3}(:\d+)?/", re.I),
     "hardcoded raw-IP URL (common in droppers)"),
    ("medium", "sudo_privilege",
     re.compile(r"\bsudo\s+\w|chmod\s+777|chmod\s+\+s\b", re.I),
     "privilege escalation / over-broad permissions"),
    ("low", "crypto_miner_hint",
     re.compile(r"(xmrig|stratum\+tcp|minerd|coinhive)", re.I),
     "cryptominer signature"),
]


def scan_text(text: str, target: str = "content") -> ScanReport:
    """Statically scan arbitrary text (a skill body, MCP tool description, etc.)
    for dangerous patterns and return a graded report."""
    report = ScanReport(target=target)
    body = text or ""
    for severity, rule, pattern, detail in _PATTERNS:
        m = pattern.search(body)
        if m:
            excerpt = body[max(0, m.start() - 20): m.end() + 20].replace("\n", " ")
            report.findings.append(Finding(severity, rule, detail, excerpt[:120]))
    return report


def scan_skill(skill) -> ScanReport:
    """Scan a Skill object (name, trigger, body) for dangerous content."""
    parts = [getattr(skill, "body", "") or "",
             getattr(skill, "trigger", "") or "",
             getattr(skill, "name", "") or ""]
    rep = scan_text("\n".join(parts), target=f"skill:{getattr(skill, 'name', '?')}")
    return rep


def scan_mcp_tool(tool_def: dict) -> ScanReport:
    """Scan an MCP tool definition (name + description + schema text) for tool
    poisoning / hidden instructions."""
    name = tool_def.get("name", "?")
    blob = "\n".join(str(tool_def.get(k, "")) for k in
                     ("name", "description", "title"))
    # Include schema property descriptions (a common poisoning hiding spot).
    schema = tool_def.get("inputSchema") or tool_def.get("parameters") or {}
    props = (schema or {}).get("properties", {}) if isinstance(schema, dict) else {}
    for p in (props or {}).values():
        if isinstance(p, dict) and p.get("description"):
            blob += "\n" + str(p["description"])
    return scan_text(blob, target=f"mcp_tool:{name}")


def scan_mcp_tools(tool_defs: list[dict]) -> dict:
    """Scan many MCP tool defs; return {name: ScanReport} plus a clean flag."""
    reports = {td.get("name", f"tool{i}"): scan_mcp_tool(td)
               for i, td in enumerate(tool_defs)}
    return {
        "reports": reports,
        "clean": all(r.clean for r in reports.values()),
        "flagged": [n for n, r in reports.items() if not r.clean],
    }


# ------------------------------------------------------------------- OSV deps
def osv_check(packages: list[tuple[str, str]], ecosystem: str = "PyPI",
              timeout: float = 10.0) -> dict:
    """Query OSV.dev for known vulnerabilities in (name, version) packages.

    Returns {package: [vuln_ids]} for affected packages. Network-dependent;
    returns {"error": ...} on failure rather than raising (fail-open for the
    advisory path, but the caller can treat an error as "unknown" and warn).
    """
    import json
    import urllib.request

    affected: dict = {}
    for name, version in packages:
        query = {"package": {"name": name, "ecosystem": ecosystem},
                 "version": version}
        try:
            req = urllib.request.Request(
                "https://api.osv.dev/v1/query",
                data=json.dumps(query).encode(), method="POST",
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            vulns = [v.get("id") for v in data.get("vulns", []) if v.get("id")]
            if vulns:
                affected[name] = vulns
        except Exception as exc:  # noqa: BLE001
            return {"error": f"OSV query failed for {name}: {exc}",
                    "partial": affected}
    return {"affected": affected, "clean": not affected}

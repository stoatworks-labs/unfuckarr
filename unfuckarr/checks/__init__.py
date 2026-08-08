"""Check engine.

A check turns a file (plus whatever the *arr and Emby know about it) into a
list of ``Finding``s. Findings carry a category, which is what the policy
engine keys remediation off — corruption gets a redownload, an incompatible
but intact file gets transcoded, hygiene gets flagged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["error", "warning", "info"]
Category = Literal["integrity", "compat", "hygiene", "emby"]


@dataclass
class Finding:
    category: Category
    code: str
    severity: Severity
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category, "code": self.code,
            "severity": self.severity, "detail": self.detail, "data": self.data,
        }


@dataclass
class CheckResult:
    path: str
    findings: list[Finding] = field(default_factory=list)
    probe: dict[str, Any] | None = None
    error: str | None = None

    def add(self, *findings: Finding) -> None:
        self.findings.extend(findings)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def status(self) -> str:
        """Single word for the file list.

        Priority is deliberate: a corrupt file that is also incompatible is
        corrupt, because that is the finding that decides what happens to it.
        """
        if self.error:
            return "error"
        cats = {f.category for f in self.errors}
        if "integrity" in cats:
            return "corrupt"
        if "compat" in cats or "emby" in cats:
            return "incompatible"
        if any(f.severity == "warning" for f in self.findings):
            return "hygiene"
        return "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "error": self.error,
            "findings": [f.as_dict() for f in self.findings],
            "probe": self.probe,
        }


__all__ = ["Finding", "CheckResult", "Severity", "Category"]

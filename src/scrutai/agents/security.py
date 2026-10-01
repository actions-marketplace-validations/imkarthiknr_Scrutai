from __future__ import annotations

from typing import ClassVar

from .base import Specialist


class SecurityAgent(Specialist):
    name = "security"
    role = (
        "You hunt for injection, hardcoded secrets, unsafe deserialization, weak crypto "
        "and disabled transport security. Before reporting, use grep/read_file to check "
        "whether input is actually untrusted or already sanitized."
    )
    categories: ClassVar[dict[str, str]] = {
        "injection": "untrusted input reaches a shell, eval/exec, or similar sink",
        "sql_injection": "SQL assembled with string formatting instead of parameters",
        "hardcoded_secret": "a real credential or key committed in source",
        "unsafe_deserialization": "pickle/yaml.load/marshal on data that may be untrusted",
        "weak_crypto": "MD5/SHA1 or similar used where security matters",
        "tls_verify_disabled": "certificate verification turned off",
        "path_traversal": "user-controlled path reaches the filesystem unchecked",
    }
    kinds = ("code", "test")

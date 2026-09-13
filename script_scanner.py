"""Defensive static scanner for uploaded scripts. Never executes uploaded code."""
from __future__ import annotations
import ast
import re
from pathlib import Path
from typing import List, Tuple

Finding = Tuple[str, str]

SUSPICIOUS_PATTERNS = [
    (r"\bhping3\b", "packet-flood tooling", "high"),
    (r"\bmasscan\b", "mass network scanning tooling", "high"),
    (r"\bnmap\b[^\n]{0,80}(?:-p\s*0-65535|-p\s*1-65535)", "mass port scanning", "high"),
    (r"\bslowloris\b", "DoS pattern", "high"),
    (r"\b(?:LOIC|HOIC)\b", "known DDoS tool reference", "high"),
    (r"\b(?:SYN|UDP|ICMP)[ _-]?flood\b", "flood-attack pattern", "high"),
    (r"\bscapy\b[\s\S]{0,120}\b(?:send|sendp|sr1|srp|flood)\b", "raw packet crafting/flood", "high"),
    (r"\bsocket\.SOCK_RAW\b", "raw socket usage", "medium"),
    (r"\b(?:hydra|medusa|ncrack)\b", "credential brute-force tooling", "high"),
    (r"\bsqlmap\b", "SQL-injection exploitation tooling", "high"),
    (r"while\s+True\s*:[\s\S]{0,160}\brequests\.(?:get|post|put|delete)\b", "unbounded HTTP request loop", "medium"),
    (r"subprocess\.(?:Popen|run|call|check_output)\([\s\S]{0,220}\b(?:nc|netcat|bash|sh|cmd\.exe|powershell)\b", "remote shell execution pattern", "high"),
    (r"\bsocket\.socket\([\s\S]{0,180}\.connect\([\s\S]{0,180}(?:subprocess|os\.dup2|pty\.spawn)", "interactive reverse shell", "high"),
    (r"\b(?:exec|eval)\s*\(\s*(?:base64|bz2|zlib|codecs)\b", "obfuscated/encoded payload execution", "high"),
    (r"\bimport\s+pty\b[\s\S]{0,100}\bpty\.spawn\b", "pty spawn for remote shell", "high"),
    (r"(?:/bin/sh|/bin/bash|cmd\.exe)\b[^\n]{0,50}-i\b", "interactive shell redirection", "high"),
    (r"\bxmrig\b|stratum\+tcp://|\bcryptonight\b", "crypto-mining indicator", "high"),
    (r"/etc/(?:shadow|passwd)\b", "sensitive host file access", "medium"),
    (r"\bparamiko\b", "SSH client library usage", "medium"),
    (r"\btelnetlib(?:3)?\b", "raw telnet client usage", "medium"),
    (r"\b(?:botnet|C2[_ -]?server|command[_ -]?and[_ -]?control)\b", "botnet/C2 terminology", "medium"),
    (r"\b(?:mirai|gafgyt|qbot)\b", "known IoT-botnet family reference", "high"),
    (r"\brm\s+-rf\s+/(?:\s|['\"]|$)", "destructive filesystem wipe", "high"),
    (r"os\.system\([\s\S]{0,120}\bmkfs(?:\b|\.)", "disk formatting command", "high"),
    (r"\b(?:shodan|censys|zoomeye)\b", "internet-wide host search API usage", "medium"),
    (r"(?:wordlist|combolist|userlist|passlist|creds?_list)\s*=", "bulk credential-list usage", "medium"),
    (r"(?:admin|root)['\"]?\s*[,:]\s*['\"](?:admin|root|password|toor|12345)", "default-credential list", "medium"),
    (r"\bip_network\([\s\S]{0,260}(?:socket\.connect|\.connect_ex|paramiko|telnetlib)", "IP-range iteration plus connection attempt", "high"),
    (r"for\s+\w+\s+in\s+range\([\s\S]{0,160}\)\s*:[\s\S]{0,260}socket\.connect", "ranged loop plus raw socket connect", "medium"),
    (r"ThreadPoolExecutor[\s\S]{0,220}(?:socket\.connect|paramiko|telnetlib)", "multi-threaded mass connection attempts", "high"),
    (r"(?:Path\(\s*['\"]\/['\"]\s*\)|os\.walk\(\s*['\"]\/['\"]\s*\))", "recursive scan starting at filesystem root", "high"),
    (r"id_rsa[\s\S]{0,220}authorized_keys|authorized_keys[\s\S]{0,220}id_rsa", "SSH key/credential harvesting", "high"),
    (r"-----BEGIN[^\n]{0,30}PRIVATE KEY-----", "private-key content or search pattern", "high"),
    (r"\bAKIA[0-9A-Z]{16}\b|\bghp_[A-Za-z0-9]{20,}\b|\bxox[baprs]-[A-Za-z0-9-]{10,}\b|\bsk_live_[A-Za-z0-9]{20,}\b", "cloud/service credential value detected", "high"),
    (r"(?:sendDocument|discord\.com/api/webhooks)[\s\S]{0,350}(?:zipfile|zipf\.write|rglob|os\.walk)", "bulk file exfiltration to external chat/webhook", "high"),
    (r"\bexfil(?:trat|_dir|_targets)\b", "exfiltration-labeled code", "high"),
    (r"ctypes\.windll|ptrace\(", "anti-debugging/sandbox evasion technique", "high"),
    (r"\bsys\.settrace\b", "runtime trace-hooking", "medium"),
    (r"urllib\.request\.urlopen\([^\n]*raw\.githubusercontent\.com[^\n]*\.(?:exe|sh|py|elf)", "remote dropper script pattern", "high"),
]
SECRET_PATTERNS = [
    # Was "high" — but a simple bot hardcoding ITS OWN token is completely
    # normal (most beginner Telegram bots do this), so this alone
    # shouldn't trigger an instant flag+mute. "medium" means it only
    # matters combined with something else genuinely suspicious.
    (r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b", "Telegram-like bot token (normal for simple bots — only a real problem combined with other flags)", "medium"),
    (r"\bghp_[A-Za-z0-9]{20,}\b", "GitHub token", "high"),
    (r"\bsk_live_[A-Za-z0-9]{20,}\b", "Stripe secret key", "high"),
]
MAX_SCAN_BYTES = 2_000_000
MAX_FINDINGS = 50

# AST deep-scan (Python files only). Regex only sees TEXT — a pattern like
# `(exec|eval)\s*\(\s*(base64|bz2|zlib|codecs)\b` only matches when one of
# those names is the LITERAL first token; `import zlib as _zl` then
# `exec(compile(_zl.decompress(x)))` slides straight past it. This walks
# the actual syntax tree instead, so it catches "eval/exec called with a
# dynamically-computed argument" regardless of what the attacker names
# their variables or aliases their imports.
_SENSITIVE_DIRS = {"/", "/root", "/etc", "/home", "/proc", "/var", "/sys"}
_SUSPICIOUS_CLASS_WORDS = {"harvest", "steal", "exfil", "harvester", "collector", "grabber"}

def _ast_findings(text: str) -> List[Finding]:
    findings: List[Finding] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return findings
    except (ValueError, RecursionError, MemoryError):
        return [("code structure too complex/malformed to analyze safely", "medium")]

    import_aliases = {"importlib": {"importlib"}, "builtins": {"builtins"}}
    import_module_names = {"import_module"}
    dangerous_exec_names = {"eval", "exec"}
    dangerous_import_names = {"__import__"}
    compile_names = {"compile"}

    def dotted_name(node):
        if isinstance(node, ast.Name): return node.id
        if isinstance(node, ast.Attribute):
            base=dotted_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    # Resolve common import aliases so AST detection is not defeated by simple renaming.
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "importlib": import_aliases["importlib"].add(a.asname or a.name.split(".")[0])
                if a.name == "builtins": import_aliases["builtins"].add(a.asname or a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "importlib":
                for a in node.names:
                    if a.name == "import_module": import_module_names.add(a.asname or a.name)
            elif node.module == "builtins":
                for a in node.names:
                    if a.name in dangerous_exec_names: dangerous_exec_names.add(a.asname or a.name)
                    if a.name == "__import__": dangerous_import_names.add(a.asname or a.name)
                    if a.name == "compile": compile_names.add(a.asname or a.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func=node.func; fname=dotted_name(func)
            base=func.value.id if isinstance(func,ast.Attribute) and isinstance(func.value,ast.Name) else None
            attr=func.attr if isinstance(func,ast.Attribute) else None

            if fname in dangerous_exec_names or fname in {"builtins.eval","builtins.exec"} or (attr in dangerous_exec_names and base in import_aliases["builtins"]):
                if node.args and not (isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,(str,bytes))):
                    findings.append((f"{attr or fname.split('.')[-1]}() called with a dynamically-computed argument — hidden/decoded code execution","high"))
                elif node.args:
                    findings.append((f"{attr or fname.split('.')[-1]}() executes source code","medium"))

            if fname in dangerous_import_names or fname in {"builtins.__import__"} or (attr=="__import__" and base in import_aliases["builtins"]):
                if node.args:
                    arg0=node.args[0]
                    if isinstance(arg0,ast.Constant) and arg0.value in ("os","subprocess","ctypes","socket","pty","shutil","multiprocessing","importlib"):
                        findings.append((f"dynamic __import__('{arg0.value}') — avoids a plain import statement","medium"))
                    elif not (isinstance(arg0,ast.Constant) and isinstance(arg0.value,str)):
                        findings.append(("dynamic module import with a computed module name","medium"))

            if fname in {"importlib.import_module"} or (attr=="import_module" and base in import_aliases["importlib"]) or fname in import_module_names:
                if node.args and not (isinstance(node.args[0],ast.Constant) and isinstance(node.args[0].value,str)):
                    findings.append(("dynamic importlib module import with a computed module name","medium"))

            if fname in compile_names or fname in {"builtins.compile"}:
                if node.args:
                    src=node.args[0]
                    mode=node.args[2] if len(node.args)>2 else None
                    if not (isinstance(src,ast.Constant) and isinstance(src.value,(str,bytes))):
                        findings.append(("compile() called with dynamically-computed source","high"))
                    elif isinstance(mode,ast.Constant) and mode.value in ("exec","eval","single"):
                        findings.append((f"compile(..., mode='{mode.value}') creates executable code","medium"))

            if fname in ("pickle.loads","pickle.load","marshal.loads","marshal.load","dill.loads","dill.load"):
                findings.append((f"unsafe deserialization API: {fname}","high"))
            if fname=="getattr" and len(node.args)>=2:
                name=node.args[1]
                if isinstance(name,ast.Constant) and name.value in ("exec","eval","__import__","system"):
                    findings.append((f"getattr() resolves dangerous callable '{name.value}'","high"))

            if isinstance(func,ast.Attribute):
                if func.attr=="walk" and isinstance(func.value,ast.Name) and func.value.id=="os" and node.args:
                    arg0=node.args[0]
                    if isinstance(arg0,ast.Constant) and isinstance(arg0.value,str) and arg0.value in _SENSITIVE_DIRS:
                        findings.append((f"os.walk('{arg0.value}') — scanning a system directory, not its own folder","high"))
        if isinstance(node,ast.ClassDef):
            name_lower=node.name.lower()
            if any(w in name_lower for w in _SUSPICIOUS_CLASS_WORDS):
                findings.append((f"class name '{node.name}' suggests data harvesting/exfiltration","medium"))
    return findings

def _dedupe(findings: List[Finding]) -> List[Finding]:
    return list(dict.fromkeys(findings))[:MAX_FINDINGS]

def _scan_text_chunks(path: Path) -> Tuple[List[Finding], str]:
    all_patterns = SUSPICIOUS_PATTERNS + SECRET_PATTERNS
    findings: List[Finding] = []
    seen_labels = set()
    overlap = 512
    tail = ""
    try:
        with path.open("rb") as fp:
            while True:
                raw = fp.read(MAX_SCAN_BYTES)
                if not raw:
                    break
                chunk = tail + raw.decode("utf-8", errors="ignore")
                for pattern, label, severity in all_patterns:
                    if label in seen_labels:
                        continue
                    try:
                        if re.search(pattern, chunk, re.IGNORECASE):
                            findings.append((label, severity))
                            seen_labels.add(label)
                    except re.error:
                        continue
                tail = chunk[-overlap:]
    except Exception as exc:
        return [(f"scanner could not read file: {type(exc).__name__}", "high")], ""
    return findings, tail

def scan_file(path: Path):
    path = Path(path)
    try:
        if not path.is_file():
            return "flagged", [("uploaded path is not a regular file", "high")]
        file_size = path.stat().st_size
    except Exception as exc:
        return "flagged", [(f"scanner could not read file: {type(exc).__name__}", "high")]

    if file_size > MAX_SCAN_BYTES * 20:
        return "flagged", [(f"file too large to scan safely ({file_size} bytes) — sent for manual review", "high")]

    findings, _ = _scan_text_chunks(path)
    seen_labels = {label for label, _ in findings}

    # AST needs the complete source, but never allow a large source file to
    # become an unbounded second RAM allocation. Regex scanning above already
    # covered the complete file.
    if path.suffix.lower() == ".py":
        AST_MAX_BYTES = 8 * 1024 * 1024
        if file_size <= AST_MAX_BYTES:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
                try:
                    ast.parse(text, filename=str(path))
                except SyntaxError:
                    findings.append(("Python syntax error", "medium"))
                else:
                    for label, severity in _ast_findings(text):
                        if label not in seen_labels:
                            findings.append((label, severity))
                            seen_labels.add(label)
            except Exception as exc:
                findings.append((f"AST analysis unavailable: {type(exc).__name__}", "medium"))
        else:
            findings.append(("AST deep scan skipped for Python file larger than 8 MB; regex scan completed", "medium"))

    findings = _dedupe(findings)
    verdict = "flagged" if any(s == "high" for _, s in findings) or len(findings) >= 2 else "clear"
    return verdict, findings

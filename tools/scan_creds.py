"""提交前凭据泄露扫描。

排除 venv/（第三方库本身含大量示例串，属噪音）与 .tmp/，只看项目自有代码。

提交到公开仓库前建议跑一次，确认「高危」命中为 0。
第三方 mock 服务（mock_data/）里的 postgres / root:123456 是本地一次性
容器的故意默认值，会命中「疑似密码赋值」，可忽略。

用法：python tools/scan_creds.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {"venv", "node_modules", ".git", ".tmp", ".pytest_tmp", "__pycache__", "exports"}
TEXT_EXT = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".json", ".toml", ".yaml", ".yml",
    ".ini", ".cfg", ".env", ".md", ".txt", ".html", ".css", ".sql", ".sh", ".bat",
}

# (名称, 正则, 是否高危)
RULES = [
    ("Supabase publishable/anon key", re.compile(r"sb_publishable_[A-Za-z0-9_\-]{10,}"), True),
    ("Supabase secret key", re.compile(r"sb_secret_[A-Za-z0-9_\-]{10,}"), True),
    ("JWT / legacy anon key", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), True),
    ("OpenAI/Anthropic key", re.compile(r"\b(?:sk|sk-ant)-[A-Za-z0-9_\-]{20,}"), True),
    ("AWS Access Key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b"), True),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), True),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), True),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), True),
    ("私钥文件", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"), True),
    ("URL 内嵌密码", re.compile(r"://[^/\s:@]{1,40}:[^/\s:@]{6,}@"), True),
    ("service_role 字样", re.compile(r"service_role"), False),
    ("疑似密码赋值", re.compile(
        r"(?:password|passwd|pwd|secret|token|api_?key|access_?key)\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']",
        re.IGNORECASE), True),
    ("Influx token 赋值", re.compile(r"(?:token)\s*=\s*[\"'][A-Za-z0-9_\-=]{20,}[\"']"), True),
]

# 明确的占位/示例值或间接读取，命中也不算泄露
WHITELIST = re.compile(
    r"(your[_-]?|xxx|example|placeholder|change[_-]?me|<[^>]+>|\.\.\.|"
    r"os\.environ|getenv|st\.secrets|secrets\[|read_secret|process\.env)",
    re.IGNORECASE,
)

findings = []
scanned = 0

for dirpath, dirnames, filenames in os.walk(ROOT):
    dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
    for fn in filenames:
        ext = os.path.splitext(fn)[1].lower()
        if ext not in TEXT_EXT:
            continue
        full = os.path.join(dirpath, fn)
        rel = os.path.relpath(full, ROOT)
        # 本地 secrets 本来就该有密钥；它已被 .gitignore 排除，不参与扫描
        if rel.replace("\\", "/") == ".streamlit/secrets.toml":
            continue
        try:
            with open(full, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            continue
        scanned += 1
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            is_private_key_marker = "PRIVATE KEY" in stripped
            if stripped.startswith("#") and not is_private_key_marker:
                continue
            for name, rx, critical in RULES:
                for m in rx.finditer(line):
                    if WHITELIST.search(line):
                        continue
                    findings.append((rel, lineno, name, critical, m.group(0), stripped))

print(f"扫描文件数: {scanned}")
print("=" * 74)
if not findings:
    print("未发现明文凭据")
else:
    crit = [f for f in findings if f[3]]
    print(f"命中 {len(findings)} 处（其中高危 {len(crit)} 处）\n")
    shown = set()
    for rel, lineno, name, critical, frag, stripped in findings:
        key = (rel, lineno, name)
        if key in shown:
            continue
        shown.add(key)
        tag = "[高危]" if critical else "[提示]"
        preview = frag if len(frag) <= 46 else frag[:43] + "..."
        print(f"{tag} {rel}:{lineno}")
        print(f"        类型: {name}")
        print(f"        内容: {preview}")
        print(f"        整行: {stripped[:96]}")
        print()

print("=" * 74)
print("说明：排除 venv/ 与 .tmp/；.streamlit/secrets.toml 已单独排除。")
sys.exit(1 if any(f[3] for f in findings) else 0)

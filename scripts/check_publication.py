"""Audit tracked publication files without displaying detected secret values."""
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIVE_CONFIGS = {f'{x}/config.yaml' for x in ('master','slaver','robot_api','serve_dream','serve_real')}
LIVE_CONFIGS.add('serve_dream/dream_navigation_sop.yaml')
RULES = {
    'credential-token': re.compile(r'\b(?:sk-[A-Za-z0-9_.-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|hf_[A-Za-z0-9]{20,})'),
    'private-key': re.compile(r'-----BEGIN (?:OPENSSH |RSA |EC |DSA )?PRIVATE KEY-----'),
    'private-ip': re.compile(r'\b(?:192\.168\.[0-9]{1,3}\.[0-9]{1,3}|10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})\b'),
    'personal-path': re.compile(r'(?i)(?:/(?:home|Users)/[^/\s"\']+|[A-Z]:[\\/]+Users[\\/]+[^\\/\s"\']+)'),
    'url-credential': re.compile(r'https?://[^\s/:]+:[^\s/@]+@'),
}

def main():
    raw = subprocess.check_output(['git','ls-files','-z','--cached'], cwd=ROOT)
    paths = sorted(set(raw.decode('utf-8').split('\0'))-{''})
    if not paths:
        raise SystemExit('Stage the publication files before auditing.')
    issues=[]; python_count=0
    for rel in paths:
        path=ROOT/rel
        if path.is_symlink(): issues.append((rel,0,'symlink'))
        if rel in LIVE_CONFIGS or path.name=='.env' or '.bak' in path.name:
            issues.append((rel,0,'private-file'))
        if path.suffix=='.md' and rel!='README.md':
            issues.append((rel,0,'old-documentation'))
        # Inspect the index (the bytes that will actually be committed).
        blob=subprocess.check_output(['git','show',':'+rel],cwd=ROOT)
        if len(blob) > 5*1024*1024: issues.append((rel,0,'large-file'))
        try: text=blob.decode('utf-8-sig')
        except UnicodeError:
            issues.append((rel,0,'unexpected-binary')); continue
        for name, pattern in RULES.items():
            for match in pattern.finditer(text): issues.append((rel,text.count('\n',0,match.start())+1,name))
        if path.suffix=='.py':
            python_count+=1
            try: ast.parse(text, filename=rel)
            except SyntaxError as exc: issues.append((rel,exc.lineno,'python-syntax'))
    for issue in issues: print(json.dumps(dict(zip(('file','line','rule'),issue))))
    print(json.dumps({'tracked_files':len(paths),'python_files':python_count,'issues':len(issues)}))
    return 1 if issues else 0

if __name__=='__main__':
    sys.exit(main())

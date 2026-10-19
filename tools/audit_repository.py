"""Inspect only the Git index. Never read local diary databases or key files."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import PurePosixPath

PRIVATE_PARTS={'.test-output','.demo-data','.eval-results','data','backups','日记备份','__pycache__','.venv','node_modules'}
PRIVATE_NAMES={'ai-key.dat','ai-key.tmp','runtime.json','日记应用开发提示词.md'}
PRIVATE_DOCS={'docs/data-migration.md','docs/data-location-d.png'}
SECRET_PATTERNS=[
    re.compile(rb'\bsk-[A-Za-z0-9_-]{24,}\b'),
    re.compile(rb'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    re.compile(rb'\bgithub_pat_[A-Za-z0-9_]{40,}\b'),
    re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
]


def audit():
    result=subprocess.run(['git','ls-files','-z'],capture_output=True,check=True)
    files=[name.decode('utf-8') for name in result.stdout.split(b'\0') if name]
    if not files:
        raise ValueError('No tracked files to inspect; stage the intended public files first')
    problems=[]
    for filename in files:
        path=PurePosixPath(filename)
        if (set(path.parts)&PRIVATE_PARTS or path.name in PRIVATE_NAMES or filename in PRIVATE_DOCS
            or path.suffix.lower() in {'.sqlite','.sqlite3','.db','.dat','.pem','.p12'}
            or path.name.startswith(('.env','journal.sqlite'))):
            problems.append(filename+': local data or credential path is tracked')
            continue
        blob=subprocess.run(['git','show',':'+filename],capture_output=True,check=True).stdout
        if path.suffix.lower() not in {'.png','.jpg','.jpeg','.gif','.webp','.ico'}:
            if any(pattern.search(blob) for pattern in SECRET_PATTERNS):
                problems.append(filename+': credential-like content detected (value omitted)')
            if re.search(rb'[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s]+',blob):
                problems.append(filename+': personal absolute path detected')
    if problems:
        for problem in problems: print(problem,file=sys.stderr)
        return 1
    print('Repository content check passed: '+str(len(files))+' tracked files; no blocked paths or credential patterns found.')
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(audit())
    except (subprocess.CalledProcessError,ValueError) as error:
        print(str(error),file=sys.stderr)
        raise SystemExit(1)

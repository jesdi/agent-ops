"""Own unique evidence root; default CI evidence is disposable after clean exit."""
import atexit
import json
import os
from pathlib import Path
import shutil
import tempfile

configured=os.environ.get('T7_LOCK_ARTIFACTS')
ROOT=Path(configured) if configured else Path(tempfile.mkdtemp(prefix='t7-evidence-'))

def default_cleanup():
    if configured:return
    # Failed ownership cleanup retains its ledger instead of erasing evidence.
    ledgers=list(ROOT.rglob('*process-ledger.json'))
    cleanups=list(ROOT.rglob('cleanup.json'))+list(ROOT.rglob('proof.json'))
    proofs=[json.loads(path.read_text()) for path in cleanups]
    if any(proof.get('all_groups_absent') is False for proof in proofs):return
    for ledger in ledgers:
        data=json.loads(ledger.read_text());run=data.get('run_directory')
        if not run or Path(run).exists():return
        if not any(proof.get('all_groups_absent') is True and proof.get('run_directory')==run for proof in proofs):return
    shutil.rmtree(ROOT,ignore_errors=False)

atexit.register(default_cleanup)

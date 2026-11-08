"""Test sensitivity by disabling one guard in a fresh disposable DB per run."""
from pathlib import Path
import os
import subprocess
import sys
import json
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1]
CASES={
 'posting_balance':'tests/test_posting.py::test_balance_is_checked_at_actual_commit',
 'posting_precision':'tests/test_posting.py::test_symmetric_excess_scale_cannot_disappear',
 'posting_immutable':'tests/test_posting.py::test_history_immutable_even_to_owner[journal_line]',
 'posting_projection':'tests/test_posting.py::test_corrupt_projection_cannot_commit',
 'owner_rls':'tests/test_configuration.py::test_owner_is_subject_to_force_rls',
 'purpose':'tests/test_configuration.py::test_ledger_purpose_immutable_and_uuid7_default[SANDBOX]',
 'binding_fk':'tests/test_configuration.py::test_invalid_role_binding_rejected[forged_copy]',
 'period_overlap':'tests/test_configuration.py::test_period_adjacency_overlap_and_no_reopen',
 'definer_search_path':'tests/test_independent_review.py::test_caller_temporary_types_cannot_hijack_the_definer',
}
result=[]
dest=ROOT/'evidence/posting';dest.mkdir(parents=True,exist_ok=True)
for mutation,test in CASES.items():
    env=os.environ.copy();env['LV_GUARD_MUTATION']=mutation
    xml=dest/(mutation+'.xml')
    p=subprocess.run([sys.executable,'-m','pytest','-q',test,'--tb=short','--junitxml='+str(xml)],cwd=ROOT,env=env,capture_output=True,text=True)
    (dest/(mutation+'.txt')).write_text(p.stdout+p.stderr)
    cases=ET.parse(xml).findall('.//testcase')
    # A setup/teardown error is not evidence that the expected invariant caught it.
    detected=p.returncode==1 and len(cases)==1 and len(cases[0].findall('failure'))==1 and not cases[0].findall('error')
    result.append({'guard':mutation,'test':test,'expected_assertion_failure':detected,'returncode':p.returncode})
(dest/'guard-sensitivity.json').write_text(json.dumps(result,indent=2)+'\n')
print(json.dumps(result,indent=2))
raise SystemExit(0 if all(r['expected_assertion_failure'] for r in result) else 1)

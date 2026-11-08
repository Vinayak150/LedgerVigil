"""RG03 locked full recalculation; original simulation, exception and approval remain immutable."""
from ledgervigil.auth.crypto import token_hash
from ledgervigil.auth.boundary import source_key
from ledgervigil.auth.problems import ProblemError
from ledgervigil.simulation import bundle,engine
from ledgervigil.simulation.canonical import canonical
_before_evaluate=None
_after_evaluate=None

def refresh(ctx,request,pid,action,match,key,*,requested_revision=None):
 if not ctx.conn.execute("SELECT to_regprocedure('lv.semantic_needed(uuid)') IS NOT NULL").fetchone()[0]:return
 if not ctx.conn.execute('SELECT lv.semantic_needed(%s)',(pid,)).fetchone()[0]:return
 if requested_revision is not None:
  row=ctx.conn.execute('SELECT revision FROM lv.proposal WHERE id=%s',(pid,)).fetchone()
  if row is None or row[0]!=requested_revision:return
 args=(token_hash(ctx.session_token),pid,action,match,key,source_key(request),request.state.request_id)
 begun=ctx.conn.execute('SELECT lv.semantic_begin(%s,%s,%s,%s,%s,%s,%s)',args).fetchone()[0]
 if begun is None:return
 if _before_evaluate:_before_evaluate(ctx)
 try:captured=bundle._capture(ctx.conn,str(ctx.tenant_id),pid,begun['revision'],recheck_state=begun['state'])
 except bundle.CaptureRefused as e:raise ProblemError(e.status,e.code,e.detail) from None
 pinned=ctx.conn.execute("SELECT input_canonical::jsonb->'versions' FROM lv.simulation_run WHERE id=(SELECT latest_simulation_id FROM lv.proposal WHERE id=%s)",(pid,)).fetchone()[0]
 if captured['versions']!=pinned:raise ProblemError(409,'SIMULATION_VERSION_UNAVAILABLE','the original simulator and policy versions must remain installed')
 try:outcome=engine.evaluate(captured)
 except engine.VersionUnavailable:raise ProblemError(409,'SIMULATION_VERSION_UNAVAILABLE','the approved simulator and policy versions must remain installed') from None
 except engine.IntegrityFailure:raise ProblemError(500,'INTERNAL','current ledger facts do not verify; nothing was committed') from None
 if _after_evaluate:_after_evaluate(ctx,captured,outcome)
 old=ctx.conn.execute('SELECT outcome_canonical::jsonb FROM lv.simulation_run WHERE id=(SELECT latest_simulation_id FROM lv.proposal WHERE id=%s)',(pid,)).fetchone()[0]
 expected=outcome.get('status')=='COMPLETED' and projection(outcome)==projection(old)
 observed=ctx.conn.execute('SELECT lv.semantic_finish(%s,%s,%s,%s,%s,%s)',(args[0],begun['id'],canonical(captured),canonical(outcome),args[-2],args[-1])).fetchone()[0]
 if observed!=expected:raise ProblemError(500,'INTERNAL','independent semantic comparisons disagree; nothing was committed')

def projection(outcome):
 """Compare economic effects and checks; retain changing balances/traces in full stored outcomes."""
 import copy
 if outcome.get('status')!='COMPLETED':return None
 o=copy.deepcopy(outcome);seq=o['snapshot_seq']
 del o['input_hash'];del o['snapshot_seq']
 for row in o['account_deltas']:
  del row['before'];del row['after']
 for verdict in o['verdicts']:
  if verdict['policy_id']=='P-CLR-02':del verdict['facts']['clearing_balance_after']
 kernel=o['calculation']['kernel']
 for key in ['input_hash','snapshot_hash','envelope_sha256']:del kernel[key]
 for evidence in o['calculation']['evidence_observations']:
  if evidence['id']=='E-DUP-01':
   evidence['reason']=evidence['reason'].replace('snapshot seq '+seq,'snapshot seq CAPTURE')
   evidence['values']['scope']=evidence['values']['scope'].replace('snapshot seq '+seq,'snapshot seq CAPTURE')
   del evidence['trace']['batch_head'];del evidence['trace']['snapshot_seq']
 return o

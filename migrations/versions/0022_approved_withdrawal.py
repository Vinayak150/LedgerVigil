"""Accepted maker withdrawal from APPROVED; no financial posting or recapture."""
from alembic import op
revision='0022_approved_withdrawal'
down_revision='0021_native_execution'
branch_labels=None
depends_on=None
BEFORE="('DRAFT','BLOCKED','READY_FOR_REVIEW','STALE')"
AFTER="('DRAFT','BLOCKED','READY_FOR_REVIEW','STALE','APPROVED')"
FENCE=" IF EXISTS(SELECT 1 FROM lv.native_execution WHERE tenant_id=p.tenant_id AND proposal_id=p.id AND revision=p.revision) THEN PERFORM lv.proposal_refusal(409,'INVALID_STATE_TRANSITION','execution evidence prevents withdrawal',NULL,NULL); END IF;\n"

def once(text,before,after):
 if text.count(before)!=1:raise RuntimeError('owning predecessor drift: '+before)
 return text.replace(before,after,1)
def definition(signature):return op.get_bind().exec_driver_sql("SELECT pg_get_functiondef('lv."+signature+"'::regprocedure)").scalar_one()
def patch(forward):
 bind=op.get_bind();old,new=(BEFORE,AFTER) if forward else (AFTER,BEFORE)
 guard=definition('guard_proposal_update()');guard=once(guard,'OLD.state IN '+old+" AND NEW.state='WITHDRAWN'",'OLD.state IN '+new+" AND NEW.state='WITHDRAWN'");bind.exec_driver_sql(guard)
 withdraw=definition('workflow_withdraw(bytea,uuid,text,text,text,text,text)');withdraw=once(withdraw,'p.state NOT IN '+old,'p.state NOT IN '+new)
 if forward:withdraw=once(withdraw,' PERFORM 1 FROM lv.ledger WHERE',FENCE+' PERFORM 1 FROM lv.ledger WHERE')
 else:withdraw=once(withdraw,FENCE,'')
 bind.exec_driver_sql(withdraw)
def upgrade():patch(True)
def downgrade():
 # Constraint validation covers all tenant rows even under FORCE RLS.
 op.execute("ALTER TABLE lv.proposal_transition ADD CONSTRAINT downgrade_no_approved_withdraw CHECK(NOT(action='withdraw' AND from_state='APPROVED' AND to_state='WITHDRAWN'))")
 patch(False)
 op.execute('ALTER TABLE lv.proposal_transition DROP CONSTRAINT downgrade_no_approved_withdraw')

"""Immutable simulation runs with their pinned input bundles (Stage 2B Family 9 SimulationRun; Stage 2A §10.1–10.2).

* lv.simulation_run   one immutable row per simulation of one proposal revision: the canonical input bundle captured from
                      one consistent snapshot (text, with its SHA-256), the canonical outcome (COMPLETED result or REJECTED
                      problem, with its SHA-256 = result_hash), the snapshot sequence, the simulator version, the user and
                      the session that ran it. Rows are never updated or deleted (trigger), and the hashes are database
                      CHECKs over the stored text, so a stored bundle or outcome cannot silently diverge from its hash.
* lv.simulation_record(...)  the only write path: SECURITY DEFINER, pinned search_path. It re-binds the verified session
                      (lv.proposal_session), re-checks `propose`, requires the proposal to exist in the session's tenant
                      and be DRAFT, requires the revision to exist, and requires the bundle to name exactly that proposal,
                      revision, content hash, ledger, tenant and a committed sequence. The runtime role lv_app can only
                      SELECT the table.

Nothing here changes a proposal, a document, a posting, a balance, the ledger counter or a ledger event. Stage 2B: "Simulation
writes only simulation_run and verdict rows". No verdict rows exist: no policy set is installed (docs/SIMULATION.md, SM-G1).
"""
from alembic import op

revision = '0011_simulation_runs'
down_revision = '0010_proposal_drafts'
branch_labels = None
depends_on = None

POLICY = "tenant_id = nullif(current_setting('app.tenant_id',true),'')::uuid"
HEX64 = "'^[0-9a-f]{64}$'"

UPGRADE = rf"""
CREATE TABLE lv.simulation_run (
 tenant_id uuid NOT NULL, ledger_id uuid NOT NULL, id uuid NOT NULL DEFAULT uuidv7(),
 proposal_id uuid NOT NULL, revision integer NOT NULL CHECK (revision > 0),
 status text NOT NULL CHECK (status IN ('COMPLETED', 'REJECTED')),
 simulator_version text NOT NULL CHECK (length(simulator_version) BETWEEN 1 AND 64),
 snapshot_seq bigint NOT NULL CHECK (snapshot_seq >= 0),
 input_canonical text NOT NULL CHECK (octet_length(input_canonical) <= 8388608),
 input_hash char(64) NOT NULL CHECK (input_hash ~ {HEX64}),
 outcome_canonical text NOT NULL CHECK (octet_length(outcome_canonical) <= 8388608),
 result_hash char(64) NOT NULL CHECK (result_hash ~ {HEX64}),
 created_by uuid NOT NULL REFERENCES lv_identity.app_user(id),
 session_id uuid NOT NULL REFERENCES lv_identity.session(id),
 created_at timestamptz NOT NULL DEFAULT transaction_timestamp(),
 PRIMARY KEY (tenant_id, ledger_id, id),
 UNIQUE (id),
 FOREIGN KEY (tenant_id, ledger_id, proposal_id, revision) REFERENCES lv.proposal_revision(tenant_id, ledger_id, proposal_id, revision),
 CHECK (input_hash = encode(sha256(convert_to(input_canonical, 'UTF8')), 'hex')),
 CHECK (result_hash = encode(sha256(convert_to(outcome_canonical, 'UTF8')), 'hex')),
 CHECK ((outcome_canonical::jsonb ->> 'status') = status),
 CHECK ((input_canonical::jsonb -> 'proposal' ->> 'id') = proposal_id::text
        AND (input_canonical::jsonb -> 'proposal' ->> 'revision') = revision::text
        AND (input_canonical::jsonb -> 'ledger' ->> 'id') = ledger_id::text
        AND (input_canonical::jsonb ->> 'tenant_id') = tenant_id::text
        AND (input_canonical::jsonb -> 'snapshot' ->> 'seq') = snapshot_seq::text
        AND (input_canonical::jsonb -> 'versions' ->> 'simulator') = simulator_version)
);
CREATE INDEX simulation_run_proposal ON lv.simulation_run (tenant_id, proposal_id, revision, id);
CREATE TRIGGER immutable BEFORE UPDATE OR DELETE ON lv.simulation_run FOR EACH ROW EXECUTE FUNCTION lv.reject_history_change();

CREATE FUNCTION lv.simulation_record(p_token_hash bytea, p_proposal uuid, p_revision integer, p_input text, p_input_hash text,
                                     p_outcome text, p_result_hash text, p_status text, p_simulator text, p_seq bigint,
                                     p_source text, p_request text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE s lv_identity.session; p lv.proposal; r lv.proposal_revision; b jsonb; head bigint; new_id uuid;
BEGIN
 s := lv.proposal_session(p_token_hash, p_source, p_request);
 IF NOT lv.proposal_may(s.tenant_id, s.user_id, 'propose') THEN
  RETURN jsonb_build_object('status', 403, 'code', 'PERMISSION_DENIED', 'detail', 'requires propose');
 END IF;
 SELECT * INTO p FROM lv.proposal WHERE tenant_id = s.tenant_id AND id = p_proposal FOR SHARE;
 IF NOT FOUND THEN
  RETURN jsonb_build_object('status', 404, 'code', 'NOT_FOUND', 'detail', 'proposal not found');
 END IF;
 IF p.state <> 'DRAFT' THEN
  RETURN jsonb_build_object('status', 409, 'code', 'INVALID_STATE_TRANSITION', 'detail', 'only DRAFT revisions are simulated here',
                            'state', p.state::text);
 END IF;
 SELECT * INTO r FROM lv.proposal_revision WHERE tenant_id = s.tenant_id AND proposal_id = p_proposal AND revision = p_revision;
 IF NOT FOUND THEN
  RETURN jsonb_build_object('status', 404, 'code', 'NOT_FOUND', 'detail', 'proposal revision not found');
 END IF;
 b := p_input::jsonb;
 SELECT coalesce(max(last_seq), 0) INTO head FROM lv.ledger_counter WHERE tenant_id = s.tenant_id AND ledger_id = p.ledger_id;
 -- The service must have captured exactly this revision of this proposal, in this tenant and ledger, at a committed sequence.
 IF (b ->> 'tenant_id') IS DISTINCT FROM s.tenant_id::text OR (b -> 'ledger' ->> 'id') IS DISTINCT FROM p.ledger_id::text
    OR (b -> 'proposal' ->> 'id') IS DISTINCT FROM p.id::text OR (b -> 'proposal' ->> 'revision') IS DISTINCT FROM r.revision::text
    OR (b -> 'proposal' ->> 'content_hash') IS DISTINCT FROM r.content_hash::text
    OR p_seq IS NULL OR p_seq > head OR p_seq < 0 THEN
  RAISE EXCEPTION 'simulation bundle does not match the proposal revision or a committed sequence' USING ERRCODE = '22023';
 END IF;
 INSERT INTO lv.simulation_run(tenant_id, ledger_id, proposal_id, revision, status, simulator_version, snapshot_seq, input_canonical,
                               input_hash, outcome_canonical, result_hash, created_by, session_id)
 VALUES (s.tenant_id, p.ledger_id, p.id, r.revision, p_status, p_simulator, p_seq, p_input, p_input_hash, p_outcome, p_result_hash,
         s.user_id, s.id)
 RETURNING id INTO new_id;
 RETURN jsonb_build_object('status', 201, 'id', new_id::text);
END $$;
"""

FUNCTION = 'lv.simulation_record(bytea, uuid, integer, text, text, text, text, text, text, bigint, text, text)'


def upgrade():
    op.get_bind().exec_driver_sql(UPGRADE)
    op.execute('ALTER TABLE lv.simulation_run ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE lv.simulation_run FORCE ROW LEVEL SECURITY')
    op.execute(f'CREATE POLICY tenant_scope ON lv.simulation_run USING ({POLICY}) WITH CHECK ({POLICY})')
    op.execute('REVOKE ALL ON lv.simulation_run FROM PUBLIC, lv_app, lv_seed, lv_ingest')
    op.execute('GRANT SELECT ON lv.simulation_run TO lv_app')
    op.execute(f'REVOKE ALL ON FUNCTION {FUNCTION} FROM PUBLIC')
    op.execute(f'GRANT EXECUTE ON FUNCTION {FUNCTION} TO lv_app')


def downgrade():
    # Disposable test databases only (tools.dev_db refuses downgrades elsewhere). Stored simulation runs are removed.
    op.get_bind().exec_driver_sql(f'DROP FUNCTION {FUNCTION}; DROP TABLE lv.simulation_run;')

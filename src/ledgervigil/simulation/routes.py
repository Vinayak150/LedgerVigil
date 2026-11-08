"""Stage 2B Family 9 read subset for recorded simulations.

    GET  /api/v1/simulations/{simulation_id}          read   SimulationRun (pinned inputs summary, outcome, hashes)
    POST /api/v1/simulations/{simulation_id}/replay   read   {original_hash, replay_hash, identical}; 409 if a pinned version
                                                             is not installed. Read-only: recomputes from the stored bundle.

No route creates a simulation in this milestone (no /submit, no what-if POST /simulations): the service is the foundation for
the atomic submission milestone. Both routes run in READ ONLY transactions after the verified-session boundary; another
tenant's simulation id is the same 404 as an absent id.
"""
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field

from ledgervigil.auth.boundary import RequestContext, require_read_only
from ledgervigil.reads.ledgers import PROBLEM, READ_ERRORS, SANDBOX_LABEL, LIMIT, CURSOR
from ledgervigil.reads.paging import check_query
from ledgervigil.reads.representation import render

from . import service
from ledgervigil.auth.problems import ProblemError
from ledgervigil.reads.paging import CursorCodec,parse_limit,list_context,page

router = APIRouter(prefix='/api/v1', tags=['simulations'])
NOT_FOUND = {404: {'description': 'NOT_FOUND (absent or another tenant; indistinguishable)', **PROBLEM}}


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SnapshotRef(Strict):
    ledger_id: str
    seq: int = Field(ge=0)
    profile_version: str
    posting_rule_version: str
    policy_set_version: str | None = Field(description='sp1-policies/1.1 for verified simulator profiles (0.2.x and 0.3.x); null for legacy 0.1.0 runs (NOT_EVALUATED, never retrofitted)')
    provider_watermark: str | None
    reconciliation: str


class ReadSetEntry(Strict):
    entity_type: str = Field(description='DOCUMENT, PERIOD, or ACCOUNT (0.3.0 adds configuration/balance dependency versions)')
    entity_id: str
    version: int = Field(ge=1)


class SimulationRun(Strict):
    id: str
    proposal_id: str
    revision: int
    ledger_id: str
    data_label: str | None
    status: str = Field(description='COMPLETED or REJECTED (pre-simulation rejection with its problem)')
    simulator_version: str
    snapshot: SnapshotRef
    read_set: list[ReadSetEntry]
    versions: dict = Field(description='Every version pinned at capture; replay requires all of them to be installed')
    pinned_sources: dict = Field(description='The cited statement (with record and file SHA-256) and the cited evidence, as captured')
    input_hash: str = Field(description='SHA-256 of the canonical input bundle')
    result_hash: str = Field(description='SHA-256 of the canonical outcome')
    outcome: dict = Field(description='COMPLETED: predicted_postings, account_deltas, document_effects, calculation (kernel diagnostics, '
                                      'not verdicts), and for verified simulator profiles the SP-1 policy verdicts, the ADR-014 aggregate '
                                      '(CLEAR, BLOCKED_FAIL, BLOCKED_INSUFFICIENT) and verification EVALUATED; legacy 0.1.0 runs keep '
                                      'verdicts [] and verification NOT_EVALUATED. REJECTED: rejection problem, no verdicts. '
                                      'Accounting UNREVIEWED; not execution readiness')
    accounting_status: str
    created_by: str
    created_at: str


class ReplayResult(Strict):
    simulation_id: str
    input_hash: str
    original_hash: str
    replay_hash: str
    identical: bool
    versions: dict
    reloaded_current_state: bool = Field(description='Always false: replay uses only the stored bundle')


def representation(row) -> dict:
    captured = service._verified_bundle(row)
    snap = captured['snapshot']
    return {'id': row[0], 'proposal_id': row[2], 'revision': row[3], 'ledger_id': row[1],
            'data_label': SANDBOX_LABEL if captured['ledger']['purpose'] == 'SANDBOX' else None, 'status': row[4],
            'simulator_version': row[5], 'snapshot': {**{k: snap[k] for k in SnapshotRef.model_fields}, 'seq': int(snap['seq'])},
            'read_set': captured['read_set'], 'versions': captured['versions'], 'pinned_sources': captured['pinned_sources'],
            'input_hash': row[8].strip(), 'result_hash': row[10].strip(), 'outcome': json.loads(row[9]),
            'accounting_status': 'UNREVIEWED', 'created_by': row[11], 'created_at': row[12]}


@router.get('/simulations/{simulation_id}', response_model=SimulationRun, operation_id='getSimulation',
            responses={**READ_ERRORS, **NOT_FOUND}, openapi_extra={'x-lv-permission': 'read', 'x-lv-accounting-status': 'UNREVIEWED'})
def get_simulation(simulation_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))]):
    check_query(request, ())
    return render(request, SimulationRun, representation(service.load_run(ctx, simulation_id)))


@router.post('/simulations/{simulation_id}/replay', response_model=ReplayResult, operation_id='replaySimulation',
             responses={**READ_ERRORS, **NOT_FOUND,
                        409: {'description': 'SIMULATION_VERSION_UNAVAILABLE (a pinned version is not installed; code not named by '
                                             'the contract, gap SM-G3)', **PROBLEM}},
             openapi_extra={'x-lv-permission': 'read', 'x-lv-idempotency': 'not used (read-only recomputation)'})
def replay_simulation(simulation_id: UUID, request: Request, ctx: Annotated[RequestContext, Depends(require_read_only('read'))]):
    check_query(request, ())
    return render(request, ReplayResult, service.replay(ctx, simulation_id))


class ProposalSimulationPage(Strict):
    proposal_id:str
    items:list[SimulationRun]
    next_cursor:str|None

@router.get('/proposals/{proposal_id}/simulations',response_model=ProposalSimulationPage,operation_id='listProposalSimulations',responses={**READ_ERRORS,**NOT_FOUND},openapi_extra={'x-lv-permission':'read'})
def list_proposal_simulations(proposal_id:UUID,request:Request,ctx:Annotated[RequestContext,Depends(require_read_only('read'))],limit:LIMIT=None,cursor:CURSOR=None):
    check_query(request,('limit','cursor'))
    if not ctx.conn.execute('SELECT 1 FROM lv.proposal WHERE id=%s',(proposal_id,)).fetchone():raise ProblemError(404,'NOT_FOUND','proposal not found')
    limit=parse_limit(request.query_params.get('limit'));codec=CursorCodec(request.app.state.auth_settings.cursor_key)
    context=list_context('proposal.simulations',ctx.tenant_id,str(proposal_id),'revision.desc,id.desc')
    position=codec.decode(context,request.query_params['cursor'],2) if 'cursor' in request.query_params else None
    params=[proposal_id];tail=''
    if position is not None:
        try:revision=int(position[0]);assert revision>=1 and str(revision)==position[0]
        except (ValueError,AssertionError):raise ProblemError(400,'CURSOR_MISMATCH','invalid cursor') from None
        tail=' AND (revision,id)<(%s,%s::uuid)';params.extend([revision,position[1]])
    rows=ctx.conn.execute('SELECT '+service.RUN_COLUMNS+' FROM lv.simulation_run WHERE proposal_id=%s'+tail+' ORDER BY revision DESC,id DESC LIMIT %s',(*params,limit+1)).fetchall()
    selected,cursor=page(rows,limit,codec,context,lambda r:[str(r[3]),r[0]])
    return render(request,ProposalSimulationPage,{'proposal_id':str(proposal_id),'items':[representation(r) for r in selected],'next_cursor':cursor})

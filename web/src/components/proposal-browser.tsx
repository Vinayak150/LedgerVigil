"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  FileText,
  History,
  RefreshCw,
  ShieldCheck,
  AlertCircle,
  ExternalLink,
  Plus,
} from "lucide-react";
import { api } from "@/lib/api";
import type { components } from "@/lib/api.generated";
import { formatAmount } from "@/lib/money-format";
import { Button } from "./button";
import { ProposalWorkflow } from "./proposal-workflow";
import { SimulationHistory } from "./simulation-history";
import { ProposalDiscussion } from "./proposal-discussion";
import { EvidenceOverride } from "./evidence-override";
import { ProposalReviews } from "./proposal-reviews";
import { PolicyExceptions } from "./policy-exceptions";
import { DraftEditor } from "./draft-editor";
import { DraftCreator } from "./draft-creator";
import { DocumentInspector } from "./document-inspector";
type Proposal = components["schemas"]["Proposal"];
type Revision = components["schemas"]["ProposalRevision"];
function errorText(status: number): string {
  if (status === 401)
    return "Your session has ended. Refresh your session to continue.";
  if (status === 403)
    return "Your account no longer has access to these drafts.";
  if (status === 404) return "This draft is unavailable to your organization.";
  if (status === 400)
    return "This page link has expired. Reload the list to start again.";
  return "We couldn’t load the drafts. Please retry. No previous draft content is shown.";
}
const when = (value: string) =>
  new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Asia/Kolkata",
  }).format(new Date(value));
function ErrorPanel({
  message,
  retry,
  refresh,
}: {
  message: string;
  retry: () => void;
  refresh: () => void;
}) {
  return (
    <div className="card financial-empty" role="alert">
      <AlertCircle size={26} />
      <h2>Draft view unavailable</h2>
      <p>{message}</p>
      <div className="financial-actions">
        <Button variant="secondary" onClick={retry}>
          Retry
        </Button>
        <Button variant="ghost" onClick={refresh}>
          Refresh session
        </Button>
      </div>
    </div>
  );
}
export function ProposalBrowser({
  userId,
  tenantId,
  csrfToken,
  canPropose,
  canDiscuss,
  canOverride,
  onSessionRefresh,
}: {
  userId: string;
  tenantId: string;
  csrfToken: string;
  canPropose: boolean;
  canDiscuss: boolean;
  canOverride: boolean;
  onSessionRefresh: () => void;
}) {
  const [stateFilter, setStateFilter] = useState("DRAFT");
  const [creating, setCreating] = useState(false);
  const [rows, setRows] = useState<Proposal[]>([]),
    [cursor, setCursor] = useState<string | null>(null),
    [mine, setMine] = useState(false),
    [selected, setSelected] = useState("");
  const [loading, setLoading] = useState(true),
    [error, setError] = useState("");
  const request = useRef<AbortController | null>(null),
    generation = useRef(0),
    heading = useRef<HTMLHeadingElement>(null);
  const load = useCallback(
    async (next?: string) => {
      request.current?.abort();
      const controller = new AbortController();
      request.current = controller;
      const current = ++generation.current;
      setLoading(true);
      setError("");
      if (!next) {
        setRows([]);
        setCursor(null);
      }
      try {
        const r = await api.GET("/api/v1/proposals", {
          signal: controller.signal,
          params: {
            query: {
              ...(stateFilter ? { state: stateFilter } : {}),
              limit: "50",
              ...(mine ? { created_by: userId } : {}),
              ...(next ? { cursor: next } : {}),
            },
          },
        });
        if (controller.signal.aborted || current !== generation.current) return;
        if (!r.data) {
          setRows([]);
          setError(errorText(r.response.status));
          return;
        }
        const data = r.data;
        setRows((old) =>
          next
            ? [
                ...old,
                ...data.items.filter((n) => !old.some((o) => o.id === n.id)),
              ]
            : data.items,
        );
        setCursor(data.next_cursor);
      } catch {
        if (!controller.signal.aborted) {
          setRows([]);
          setError(
            "We couldn’t reach LedgerVigil. Check your connection and retry.",
          );
        }
      } finally {
        if (!controller.signal.aborted && current === generation.current)
          setLoading(false);
      }
    },
    [mine, userId, stateFilter],
  );
  useEffect(() => {
    void load();
    return () => request.current?.abort();
  }, [load]);
  function open(id: string) {
    setSelected(id);
    requestAnimationFrame(() => heading.current?.focus());
  }
  if (creating)
    return (
      <DraftCreator
        csrfToken={csrfToken}
        onClose={() => {
          setCreating(false);
          requestAnimationFrame(() =>
            document.getElementById("new-draft-button")?.focus(),
          );
        }}
        onCreated={(p) => {
          setCreating(false);
          setRows((old) => [p, ...old.filter((row) => row.id !== p.id)]);
          open(p.id);
        }}
      />
    );
  if (selected)
    return (
      <ProposalDetail
        key={selected}
        id={selected}
        userId={userId}
        tenantId={tenantId}
        csrfToken={csrfToken}
        canPropose={canPropose}
        canOverride={canOverride}
        canDiscuss={canDiscuss}
        onUpdated={(updated) =>
          setRows((old) =>
            old.map((row) => (row.id === updated.id ? updated : row)),
          )
        }
        onBack={() => {
          setSelected("");
          void load();
          requestAnimationFrame(() => heading.current?.focus());
        }}
        onSessionRefresh={onSessionRefresh}
      />
    );
  return (
    <section className="proposal-browser" aria-label="Proposals">
      <div className="drafts-intro">
        <div>
          <div className="eyebrow">HUMAN-OWNED PROPOSALS</div>
          <h2 ref={heading} tabIndex={-1}>
            Proposal workspace
          </h2>
          <p>
            Draft settlements, inspect pinned predictions and trace every saved
            revision.
          </p>
        </div>
        <div className="financial-actions">
          <Button
            variant="secondary"
            onClick={() => void load()}
            disabled={loading}
          >
            <RefreshCw size={15} />
            Refresh proposals
          </Button>
          {canPropose && (
            <Button id="new-draft-button" onClick={() => setCreating(true)}>
              <Plus size={16} />
              New settlement draft
            </Button>
          )}
        </div>
      </div>
      <div className="notice draft-boundary">
        <ShieldCheck size={20} />
        <span>
          Drafts, simulation and withdrawal have no financial effect. Submission
          reserves cited statements. Eligible independent reviewers can approve
          or reject; recorded decisions stay immutable. Independently approved,
          current native SANDBOX settlements can be explicitly executed below.
        </span>
      </div>
      <div className="draft-list-toolbar">
        <label htmlFor="proposal-state-filter">
          State
          <select
            id="proposal-state-filter"
            value={stateFilter}
            onChange={(e) => setStateFilter(e.target.value)}
          >
            <option value="DRAFT">Drafts</option>
            <option value="BLOCKED">Blocked</option>
            <option value="READY_FOR_REVIEW">Ready for review</option>
            <option value="APPROVED">Approved</option>
            <option value="EXECUTED">Executed</option>
            <option value="REJECTED">Rejected</option>
            <option value="WITHDRAWN">Withdrawn</option>
            <option value="STALE">Stale</option>
            <option value="">All states</option>
          </select>
        </label>
        <label>
          <input
            type="checkbox"
            checked={mine}
            onChange={(e) => setMine(e.target.checked)}
          />
          Created by me
        </label>
        <span className="muted small">
          {rows.length} {rows.length === 1 ? "proposal" : "proposals"} loaded ·
          newest first
        </span>
      </div>
      {error ? (
        <ErrorPanel
          message={error}
          retry={() => void load()}
          refresh={onSessionRefresh}
        />
      ) : loading && !rows.length ? (
        <div className="card financial-status" role="status">
          Loading your organization’s drafts…
        </div>
      ) : !rows.length ? (
        <section className="card financial-empty">
          <FileText size={30} />
          <h3>No proposals in this view {mine ? "created by you" : ""}</h3>
          <p>
            {canPropose
              ? "Create a settlement draft from an existing staged statement and payment records."
              : "Your role can inspect drafts. Creating one requires propose permission."}
          </p>
        </section>
      ) : (
        <section className="card draft-list" aria-label="Settlement proposals">
          {rows.map((p) => (
            <button
              className="draft-list-row"
              key={p.id}
              onClick={() => open(p.id)}
            >
              <span className="draft-row-icon">
                <FileText size={20} />
              </span>
              <span className="draft-row-main">
                <strong>
                  {p.operations[0]?.payout_id ?? "Settlement draft"}
                  {p.operations.length > 1
                    ? ` + ${p.operations.length - 1} operations`
                    : ""}
                </strong>
                <span>{p.rationale || "No rationale provided"}</span>
                <small>
                  Ledger {p.ledger_id.slice(-8)} · Updated {when(p.updated_at)}{" "}
                  IST
                </small>
              </span>
              <span className="draft-row-state">
                <span className="outline-tag">{p.state}</span>
                <small>Revision {p.revision}</small>
              </span>
              <ArrowRight size={17} />
            </button>
          ))}
        </section>
      )}
      {cursor && !error && (
        <div className="financial-status">
          <Button
            variant="secondary"
            disabled={loading}
            onClick={() => void load(cursor)}
          >
            {loading ? "Loading…" : "Load more proposals"}
          </Button>
        </div>
      )}
    </section>
  );
}
function ProposalDetail({
  id,
  userId,
  tenantId,
  csrfToken,
  canPropose,
  canDiscuss,
  canOverride,
  onUpdated,
  onBack,
  onSessionRefresh,
}: {
  id: string;
  userId: string;
  tenantId: string;
  csrfToken: string;
  canPropose: boolean;
  canDiscuss: boolean;
  canOverride: boolean;
  onUpdated: (proposal: Proposal) => void;
  onBack: () => void;
  onSessionRefresh: () => void;
}) {
  const [proposal, setProposal] = useState<Proposal | null>(null),
    [revisions, setRevisions] = useState<Revision[]>([]),
    [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true),
    [moreBusy, setMoreBusy] = useState(false),
    [error, setError] = useState(""),
    [historyError, setHistoryError] = useState("");
  const [inspecting, setInspecting] = useState<{
    id: string;
    amount: components["schemas"]["Money"];
  } | null>(null);
  const [historyNotice, setHistoryNotice] = useState("");
  const [editing, setEditing] = useState(false),
    [etag, setEtag] = useState(""),
    [savedNotice, setSavedNotice] = useState<{
      revision: number;
      text: string;
    } | null>(null);
  const [inspection, setInspection] = useState(0);
  const [version, setVersion] = useState(0),
    [selectedRevision, setSelectedRevision] = useState<number | null>(null);
  const moreRequest = useRef<AbortController | null>(null),
    heading = useRef<HTMLHeadingElement>(null);
  const contentHeading = useRef<HTMLHeadingElement>(null);
  function showRevision(revision: number | null) {
    setSelectedRevision(revision);
    requestAnimationFrame(() => contentHeading.current?.focus());
  }
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    setHistoryError("");
    setHistoryNotice("");
    setMoreBusy(false);
    setProposal(null);
    setEtag("");
    setRevisions([]);
    setCursor(null);
    setSelectedRevision(null);
    (async () => {
      try {
        const opts = {
          signal: controller.signal,
          params: { path: { proposal_id: id } },
        };
        const [detail, history] = await Promise.all([
          api.GET("/api/v1/proposals/{proposal_id}", opts),
          api.GET("/api/v1/proposals/{proposal_id}/revisions", opts),
        ]);
        if (controller.signal.aborted) return;
        if (!detail.data) {
          setError(errorText(detail.response.status));
          return;
        }
        if (detail.data.id !== id) {
          setError(
            "The response did not match the requested draft. Reload to continue.",
          );
          return;
        }
        setProposal(detail.data);
        setEtag(detail.response.headers.get("ETag") ?? "");
        if (history.data && history.data.proposal_id === id) {
          const head = detail.data.revision;
          const newer = history.data.items.some((r) => r.revision > head);
          setRevisions(history.data.items.filter((r) => r.revision <= head));
          setCursor(newer ? null : history.data.next_cursor);
          if (newer)
            setHistoryNotice(
              "A newer revision is available. Refresh this draft to load its current content.",
            );
        } else
          setHistoryError(
            "Revision history could not be loaded. Refresh this draft to retry.",
          );
      } catch {
        if (!controller.signal.aborted)
          setError("We couldn’t reach LedgerVigil. Retry to load this draft.");
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          requestAnimationFrame(() => heading.current?.focus());
        }
      }
    })();
    return () => {
      controller.abort();
      moreRequest.current?.abort();
    };
  }, [id, version]);
  async function more() {
    if (!cursor || moreBusy) return;
    const controller = new AbortController();
    moreRequest.current = controller;
    setMoreBusy(true);
    setHistoryError("");
    try {
      const r = await api.GET("/api/v1/proposals/{proposal_id}/revisions", {
        signal: controller.signal,
        params: { path: { proposal_id: id }, query: { cursor } },
      });
      if (controller.signal.aborted) return;
      if (!r.data || r.data.proposal_id !== id) {
        setHistoryError(
          "History pagination expired or failed. Refresh this draft to start again.",
        );
        return;
      }
      const data = r.data;
      const newer = data.items.some(
        (r) => r.revision > (proposal?.revision ?? 0),
      );
      const visible = data.items.filter(
        (r) => r.revision <= (proposal?.revision ?? 0),
      );
      if (newer)
        setHistoryNotice(
          "A newer revision is available. Refresh this draft to load its current content.",
        );
      setRevisions((old) => [
        ...old,
        ...visible.filter((n) => !old.some((o) => o.revision === n.revision)),
      ]);
      setCursor(newer ? null : data.next_cursor);
    } catch {
      if (!controller.signal.aborted)
        setHistoryError(
          "The next history page could not be loaded. Refresh this draft to retry.",
        );
    } finally {
      if (!controller.signal.aborted) setMoreBusy(false);
    }
  }
  const historical =
    selectedRevision === null
      ? null
      : revisions.find((r) => r.revision === selectedRevision);
  const content = historical ?? proposal;
  return (
    <section className="proposal-browser" aria-label="Proposal detail">
      <div className="draft-detail-toolbar">
        <Button variant="ghost" onClick={onBack}>
          <ArrowLeft size={16} />
          All proposals
        </Button>
        <Button
          variant="secondary"
          disabled={loading}
          onClick={() => setVersion((n) => n + 1)}
        >
          <RefreshCw size={15} />
          Refresh proposal
        </Button>
      </div>
      {savedNotice && proposal?.revision === savedNotice.revision && (
        <div className="notice" role="status">
          {savedNotice.text}
        </div>
      )}
      {loading ? (
        <div className="card financial-status" role="status">
          Loading draft and revision history…
        </div>
      ) : error ? (
        <ErrorPanel
          message={error}
          retry={() => setVersion((n) => n + 1)}
          refresh={onSessionRefresh}
        />
      ) : (
        proposal &&
        content && (
          <>
            <div className="drafts-intro">
              <div>
                <div className="eyebrow">SETTLEMENT PROPOSAL</div>
                <h2 ref={heading} tabIndex={-1}>
                  {proposal.operations[0]?.payout_id ?? "Settlement draft"}
                </h2>
                <p>
                  {proposal.state} · current revision {proposal.revision} ·
                  version {proposal.version}
                </p>
              </div>
              <div className="draft-detail-actions">
                <span className="outline-tag">{proposal.state}</span>
                {!historical && (
                  <Button
                    variant="ghost"
                    onClick={() =>
                      document.getElementById("discussion-title")?.focus()
                    }
                  >
                    Jump to discussion
                  </Button>
                )}
                {!historical && canPropose && (
                  <ProposalWorkflow
                    proposal={proposal}
                    etag={etag}
                    csrfToken={csrfToken}
                    userId={userId}
                    tenantId={tenantId}
                    onChanged={() => setVersion((n) => n + 1)}
                  />
                )}
                {!historical &&
                  canPropose &&
                  proposal.allowed_actions.includes("EDIT") &&
                  etag && (
                    <Button
                      variant="secondary"
                      onClick={() => setEditing(true)}
                    >
                      Edit draft
                    </Button>
                  )}
              </div>
            </div>
            <div className="notice draft-boundary">
              <ShieldCheck size={20} />
              <span>
                {proposal.data_label}. Accounting review:{" "}
                {proposal.accounting_status}.{" "}
                {proposal.state === "DRAFT"
                  ? "This is a saved draft. It has not been submitted, approved or executed."
                  : proposal.state === "WITHDRAWN"
                    ? "Withdrawn. Statement reservations have been released; saved predictions remain historical records."
                    : proposal.state === "STALE"
                      ? "Stale. Pinned dependencies changed. Statement reservations have been released; this prediction is historical. The maker can re-simulate the same saved content; current period and evidence gates still apply."
                      : "Submission pins a prediction and reserves cited statements. It does not approve or execute a settlement."}
              </span>
            </div>
            {editing && canPropose && (
              <DraftEditor
                proposal={proposal}
                etag={etag}
                csrfToken={csrfToken}
                onClose={() => {
                  setEditing(false);
                  setVersion((n) => n + 1);
                }}
                onSaved={(updated) => {
                  setEditing(false);
                  onUpdated(updated);
                  setSavedNotice({
                    revision: updated.revision,
                    text: `Draft revision ${updated.revision} saved. No financial records were changed.`,
                  });
                  setVersion((n) => n + 1);
                }}
              />
            )}
            {inspecting && (
              <DocumentInspector
                key={inspecting.id}
                id={inspecting.id}
                ledgerId={proposal.ledger_id}
                proposedAmount={inspecting.amount}
                onClose={() => setInspecting(null)}
              />
            )}
            <div className="draft-layout">
              <div className="draft-content">
                {proposal.latest_simulation_id &&
                  (!historical ||
                    historical.revision === proposal.revision) && (
                    <SimulationHistory
                      key={`${proposal.latest_simulation_id}:${inspection}`}
                      focusOnLoad={inspection > 0}
                      id={proposal.latest_simulation_id}
                      proposalId={proposal.id}
                      ledgerId={proposal.ledger_id}
                      revision={proposal.revision}
                      csrfToken={csrfToken}
                    />
                  )}
                {canOverride &&
                  proposal.created_by !== userId &&
                  !historical && (
                    <EvidenceOverride
                      key={proposal.id}
                      proposal={proposal}
                      etag={etag}
                      userId={userId}
                      tenantId={tenantId}
                      csrfToken={csrfToken}
                      onReload={() => setVersion((n) => n + 1)}
                      onSessionRefresh={onSessionRefresh}
                    />
                  )}
                {!historical && (
                  <ProposalReviews
                    key={`reviews:${proposal.id}`}
                    proposal={proposal}
                    etag={etag}
                    userId={userId}
                    tenantId={tenantId}
                    csrfToken={csrfToken}
                    onInspect={() => setInspection((n) => n + 1)}
                    onReload={() => setVersion((n) => n + 1)}
                    onSessionRefresh={onSessionRefresh}
                  />
                )}
                {proposal.latest_simulation_id && !historical && (
                  <PolicyExceptions
                    key={`${proposal.id}:${proposal.version}`}
                    proposalId={proposal.id}
                    ledgerId={proposal.ledger_id}
                    revision={proposal.revision}
                    simulationId={proposal.latest_simulation_id}
                  />
                )}
                <section className="card">
                  <div className="card-heading">
                    <FileText size={18} />
                    <h3 ref={contentHeading} tabIndex={-1}>
                      {historical
                        ? `Historical revision ${historical.revision}`
                        : `Current content · revision ${proposal.revision}`}
                    </h3>
                    {historical && (
                      <Button
                        variant="ghost"
                        onClick={() => showRevision(null)}
                      >
                        Return to current
                      </Button>
                    )}
                  </div>
                  <div className="card-content">
                    <h4>Rationale</h4>
                    <p className="draft-rationale">
                      {content.rationale || "No rationale provided."}
                    </p>
                    <div className="draft-identifiers">
                      <span>
                        Proposal <code>{proposal.id}</code>
                      </span>
                      <span>
                        Ledger <code>{proposal.ledger_id}</code>
                      </span>
                    </div>
                  </div>
                </section>
                {content.operations.map((op, index) => (
                  <section className="card" key={index}>
                    <div className="card-heading">
                      <h3>Operation {index + 1} · Record settlement</h3>
                      <span className="muted small">
                        Schema {op.schema_version}
                      </span>
                    </div>
                    <div className="card-content">
                      <dl className="draft-facts">
                        <div>
                          <dt>Processor</dt>
                          <dd>{op.processor_ref}</dd>
                        </div>
                        <div>
                          <dt>Payout reference</dt>
                          <dd>{op.payout_id}</dd>
                        </div>
                        <div>
                          <dt>Payout date</dt>
                          <dd>{op.payout_date}</dd>
                        </div>
                        <div>
                          <dt>Statement reference</dt>
                          <dd>
                            <code>{op.statement_document_id}</code>
                          </dd>
                        </div>
                      </dl>
                      <div className="draft-amounts">
                        {(
                          [
                            ["Gross", op.gross],
                            ["Fee", op.fee],
                            ["Net", op.net],
                          ] as const
                        ).map(([label, money]) => (
                          <div key={label}>
                            <span>
                              {label} · {money.currency}
                            </span>
                            <strong>
                              {formatAmount(money.amount, money.currency)}
                            </strong>
                          </div>
                        ))}
                      </div>
                      <h4>Referenced payment items</h4>
                      <ul className="draft-items">
                        {op.items.map((item, itemIndex) => (
                          <li key={item.document_id}>
                            <span>
                              <code>{item.document_id}</code>
                              <small>{item.kind}</small>
                              <Button
                                variant="ghost"
                                aria-label={`Inspect current payment record ${itemIndex + 1}`}
                                onClick={() =>
                                  setInspecting({
                                    id: item.document_id,
                                    amount: item.amount,
                                  })
                                }
                              >
                                Inspect current record{" "}
                                <ExternalLink size={13} />
                              </Button>
                            </span>
                            <strong>
                              {item.amount.currency}{" "}
                              {formatAmount(
                                item.amount.amount,
                                item.amount.currency,
                              )}
                            </strong>
                          </li>
                        ))}
                      </ul>
                    </div>
                    <div className="card-foot">
                      These are proposed amounts. Any pinned simulation result
                      is shown separately.{" "}
                      {proposal.state === "EXECUTED"
                        ? "This proposal has executed. Inspect the current ledger for committed balances."
                        : "Saved amounts are not a live execution status. Read the execution receipt and current ledger for committed effects."}
                    </div>
                  </section>
                ))}
                <section className="card">
                  <div className="card-heading">
                    <ExternalLink size={18} />
                    <h3>Evidence references</h3>
                  </div>
                  <div className="card-content">
                    {content.evidence_refs.length ? (
                      <ul className="draft-evidence">
                        {content.evidence_refs.map((e) => (
                          <li key={e.kind + e.id}>
                            <strong>
                              {e.kind.replaceAll("_", " ").toLowerCase()}
                            </strong>
                            <code>{e.id}</code>
                            {e.provenance && (
                              <span>Provenance: {e.provenance}</span>
                            )}
                            {e.sha256 && (
                              <code className="evidence-hash">
                                SHA-256 {e.sha256}
                              </code>
                            )}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p className="muted">No evidence references recorded.</p>
                    )}
                  </div>
                </section>
                {!historical && (
                  <ProposalDiscussion
                    proposalId={proposal.id}
                    ledgerId={proposal.ledger_id}
                    revision={proposal.revision}
                    version={proposal.version}
                    userId={userId}
                    tenantId={tenantId}
                    csrfToken={csrfToken}
                    canDiscuss={canDiscuss}
                    onReload={() => setVersion((n) => n + 1)}
                    onSessionRefresh={onSessionRefresh}
                  />
                )}
              </div>
              <aside className="draft-history" aria-label="Revision history">
                <section className="card">
                  <div className="card-heading">
                    <History size={18} />
                    <h3>Revision history</h3>
                  </div>
                  <div className="history-intro">
                    Immutable saved revisions · times shown in IST
                  </div>
                  {historyNotice && (
                    <p className="notice" role="status">
                      {historyNotice}
                    </p>
                  )}
                  {historyError && (
                    <p className="notice error" role="alert">
                      {historyError}
                    </p>
                  )}
                  <div className="revision-list">
                    {revisions.map((r) => (
                      <button
                        key={r.revision}
                        onClick={() => showRevision(r.revision)}
                        aria-pressed={selectedRevision === r.revision}
                      >
                        <strong>
                          Revision {r.revision}
                          <span>{r.change.toLowerCase()}</span>
                        </strong>
                        <time dateTime={r.created_at}>
                          {when(r.created_at)} IST
                        </time>
                        <small>Author {r.authored_by.slice(-8)}</small>
                        <code title={r.content_hash}>
                          SHA-256 {r.content_hash.slice(0, 16)}…
                        </code>
                      </button>
                    ))}
                  </div>
                  {cursor && (
                    <div className="financial-status">
                      <Button
                        variant="secondary"
                        disabled={moreBusy}
                        onClick={() => void more()}
                      >
                        Load more revisions
                      </Button>
                    </div>
                  )}
                </section>
                <section className="card draft-people">
                  <h3>Accountability</h3>
                  <p>Maker</p>
                  <code>{proposal.created_by}</code>
                  <p>Recorded editors</p>
                  {proposal.editors.map((editor) => (
                    <code key={editor}>{editor}</code>
                  ))}
                  <small>
                    The maker and recorded editors cannot approve or reject.
                    Eligible independent reviewers receive decision controls
                    after the server verifies their access and current review
                    target.
                  </small>
                </section>
              </aside>
            </div>
          </>
        )
      )}
    </section>
  );
}

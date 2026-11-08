"use client";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ShieldX } from "lucide-react";
import { api } from "@/lib/api";
import {
  problemCode,
  problemReason,
  type Proposal,
  type Simulation,
} from "@/lib/proposal-workflow";
import {
  rejectionStorageKey,
  readRejectionRecovery,
  validRejectionComment,
  rejectionRetryWindow,
  canStartRejection,
  rejectionRequestBody,
  rejectionReceipt,
  definitiveRejectionRefusal,
  type RejectionRecovery,
} from "@/lib/rejection-recovery";
import { Button } from "./button";
import { approvalStorageKey } from "@/lib/approval-recovery";
import { executionStorageKey } from "@/lib/execution-recovery";
import { peerReviewBlocked } from "@/lib/review-interlock";

export function ProposalRejection({
  proposal,
  etag,
  userId,
  tenantId,
  csrfToken,
  onInspect,
  onReload,
  onSessionRefresh,
  peerBlocked,
  onLockChange,
}: {
  proposal: Proposal;
  etag: string;
  userId: string;
  tenantId: string;
  csrfToken: string;
  onInspect: () => void;
  onReload: () => void;
  onSessionRefresh: () => void;
  peerBlocked: boolean;
  onLockChange: (locked: boolean) => void;
}) {
  const scope = {
      tenantId,
      userId,
      proposalId: proposal.id,
      ledgerId: proposal.ledger_id,
    },
    storage = rejectionStorageKey(scope);
  const [recovery, setRecovery] = useState<RejectionRecovery>({
    schema: 1,
    decision: "REJECT",
    ...scope,
    comment: "",
    attempt: null,
    blockedVersion: null,
  });
  const [ready, setReady] = useState(false),
    [corrupt, setCorrupt] = useState(false),
    [storageError, setStorageError] = useState(false),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  const [run, setRun] = useState<Simulation | null>(null),
    [runLoading, setRunLoading] = useState(false),
    [runError, setRunError] = useState(false),
    [retryRead, setRetryRead] = useState(0),
    [ack, setAck] = useState(false);
  const [receipt, setReceipt] = useState<{
      revision: number;
      version: number;
      replayed: boolean;
    } | null>(null),
    [current, setCurrent] = useState<{
      state: string;
      version: number;
      records: number;
      more: boolean;
    } | null>(null);
  const sending = useRef(false),
    active = useRef<AbortController | null>(null),
    status = useRef<HTMLDivElement>(null);
  const offered = proposal.allowed_actions.includes("REJECT");
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(storage),
        saved = readRejectionRecovery(raw, scope);
      if (saved) {
        setRecovery(saved);
        if (saved.attempt)
          setMessage(
            "An earlier rejection request is unconfirmed. Its original revision, simulation, comment, version and key are preserved. Read current state or retry only that original request.",
          );
      } else if (raw !== null) setCorrupt(true);
    } catch {
      setStorageError(true);
    }
    setReady(true);
    return () => active.current?.abort();
    // Verified organization/user and proposal scope own this component's lifetime.
  }, [storage, tenantId, userId, proposal.id, proposal.ledger_id]);
  useEffect(() => {
    if (!offered || !proposal.latest_simulation_id) {
      setRun(null);
      setRunError(false);
      setRunLoading(false);
      return;
    }
    const controller = new AbortController();
    setRun(null);
    setRunError(false);
    setRunLoading(true);
    setAck(false);
    void (async () => {
      try {
        const r = await api.GET("/api/v1/simulations/{simulation_id}", {
          signal: controller.signal,
          params: { path: { simulation_id: proposal.latest_simulation_id! } },
        });
        if (controller.signal.aborted) return;
        if (
          !r.data ||
          r.data.id !== proposal.latest_simulation_id ||
          r.data.proposal_id !== proposal.id ||
          r.data.ledger_id !== proposal.ledger_id ||
          r.data.revision !== proposal.revision
        )
          throw Error();
        setRun(r.data);
      } catch {
        if (!controller.signal.aborted) setRunError(true);
      } finally {
        if (!controller.signal.aborted) setRunLoading(false);
      }
    })();
    return () => controller.abort();
  }, [
    offered,
    proposal.latest_simulation_id,
    proposal.id,
    proposal.ledger_id,
    proposal.revision,
    retryRead,
  ]);
  function remember(value: RejectionRecovery) {
    setRecovery(value);
    try {
      if (value.comment || value.attempt || value.blockedVersion !== null)
        sessionStorage.setItem(storage, JSON.stringify(value));
      else sessionStorage.removeItem(storage);
      setStorageError(false);
      return true;
    } catch {
      setStorageError(true);
      return false;
    }
  }
  function announce(value: string) {
    setMessage(value);
    requestAnimationFrame(() => status.current?.focus());
  }
  const candidate = canStartRejection(proposal, run, userId, etag),
    pending = recovery.attempt !== null,
    blocked = recovery.blockedVersion === proposal.version;
  useEffect(() => {
    onLockChange(
      busy || pending || !!receipt || corrupt || storageError || blocked,
    );
  }, [busy, pending, receipt, corrupt, storageError, blocked, onLockChange]);
  async function readCurrent() {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setCurrent(null);
    const controller = new AbortController();
    active.current = controller;
    try {
      const [head, history] = await Promise.all([
        api.GET("/api/v1/proposals/{proposal_id}", {
          signal: controller.signal,
          params: { path: { proposal_id: proposal.id } },
        }),
        api.GET("/api/v1/proposals/{proposal_id}/reviews", {
          signal: controller.signal,
          params: {
            path: { proposal_id: proposal.id },
            query: { limit: "200" },
          },
        }),
      ]);
      if (controller.signal.aborted) return;
      if (
        !head.data ||
        head.data.id !== proposal.id ||
        head.data.ledger_id !== proposal.ledger_id ||
        !history.data ||
        history.data.proposal_id !== proposal.id ||
        history.data.items.some(
          (d) =>
            d.proposal_id !== proposal.id || d.ledger_id !== proposal.ledger_id,
        )
      )
        throw Error();
      setCurrent({
        state: head.data.state,
        version: head.data.version,
        records: history.data.items.length,
        more: history.data.next_cursor !== null,
      });
      announce(
        receipt
          ? "The recorded receipt remains confirmed. This separate read shows current proposal state; it does not authorize execution."
          : "Current state was read. It does not settle an original request still in flight or authorize a replacement key. Use the original retry to obtain its recorded response.",
      );
    } catch {
      if (!controller.signal.aborted)
        announce(
          receipt
            ? "The receipt remains confirmed, but current state could not be read. Refresh your session or connection."
            : "Current state could not be verified. The original rejection request stays preserved. Refresh your session or connection.",
        );
    } finally {
      sending.current = false;
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  async function send(event: FormEvent) {
    event.preventDefault();
    if (
      !ready ||
      sending.current ||
      corrupt ||
      receipt ||
      !validRejectionComment(recovery.comment) ||
      (!pending && (!candidate || blocked || !ack))
    )
      return;
    if (!pending) {
      try {
        if (
          peerBlocked ||
          peerReviewBlocked(
            sessionStorage.getItem(executionStorageKey(scope)),
            "execution",
            scope,
            proposal.version,
          ) ||
          peerReviewBlocked(
            sessionStorage.getItem(approvalStorageKey(scope)),
            "approval",
            scope,
            proposal.version,
          )
        ) {
          announce(
            "An approval request is unresolved or blocked in this tab. Resolve its original response or refresh the proposal before starting a rejection.",
          );
          return;
        }
      } catch {
        announce(
          "The other decision recovery record could not be checked. No new rejection was sent. Restore tab storage before continuing.",
        );
        return;
      }
    }
    const attempt = recovery.attempt ?? {
      key: crypto.randomUUID(),
      etag,
      revision: proposal.revision,
      simulationId: run!.id,
      inputHash: run!.input_hash,
      resultHash: run!.result_hash,
      startedAt: Date.now(),
    };
    if (!rejectionRetryWindow(attempt)) {
      announce(
        "The safe retry window has ended. No retry or replacement key will be sent. An operator must reconcile this preserved request before another action.",
      );
      return;
    }
    const value = { ...recovery, attempt };
    if (!remember(value)) {
      announce(
        "Tab storage is unavailable. No rejection request was sent. Restore storage before continuing so the original request can survive reload.",
      );
      return;
    }
    sending.current = true;
    setBusy(true);
    setCurrent(null);
    const controller = new AbortController();
    active.current = controller;
    try {
      const r = await api.POST("/api/v1/proposals/{proposal_id}/reject", {
        signal: controller.signal,
        params: { path: { proposal_id: proposal.id } },
        body: rejectionRequestBody(value),
        headers: {
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": attempt.key,
          "If-Match": attempt.etag,
        },
      });
      if (controller.signal.aborted) return;
      if (
        r.response.status === 200 &&
        rejectionReceipt(r.data, scope, attempt)
      ) {
        setReceipt({
          revision: r.data!.revision,
          version: r.data!.version,
          replayed: r.response.headers.get("Idempotent-Replayed") === "true",
        });
        remember({
          ...value,
          comment: "",
          attempt: null,
          blockedVersion: null,
        });
        announce(
          "The original rejection receipt is confirmed. This is a point-in-time workflow decision, not execution or accounting sign-off. Read or refresh the current proposal before any further action.",
        );
      } else {
        const code = problemCode(r.error),
          reason = problemReason(r.error);
        if (definitiveRejectionRefusal(r.response.status, code)) {
          remember({
            ...value,
            attempt: null,
            blockedVersion: proposal.version,
          });
          setAck(false);
          announce(
            code === "ALREADY_DECIDED"
              ? "This request was refused because the revision already has a decision. Refresh the proposal and inspect its recorded history."
              : code === "PROPOSAL_STALE"
                ? "Dependencies changed. The server marked this proposal STALE and released its reservations; this request recorded no rejection. Refresh the proposal. The maker must use the supported re-simulation flow."
                : reason === "FULL_CAPTURE_CHANGED"
                  ? "The full ledger capture changed. This request was refused without rejection. The form cannot force a recapture; the maker must use the supported withdrawal and fresh-proposal path."
                  : r.response.status === 412
                    ? "The proposal version or revision changed. This request was refused. Refresh and inspect the current content before a new explicit rejection."
                    : "The server refused this request under the supported state or validation gates. Refresh the proposal before another explicit attempt.",
          );
        } else
          announce(
            reason === "REVIEW_COOLING_OFF"
              ? "The server requires the review-grant waiting period. The original request remains preserved; a permission refusal cannot settle a possible earlier commit."
              : r.response.status === 401 || r.response.status === 403
                ? "Session or separation-of-duties verification failed. Refresh your session. The original key, revision and comment remain preserved because an earlier commit may still be unconfirmed."
                : "The rejection result is unconfirmed. Read current state or retry the exact original request. Its comment, revision, version and key remain locked.",
          );
      }
    } catch {
      if (!controller.signal.aborted)
        announce(
          "The response was interrupted. Rejection may have committed. Read current state or retry only the exact original request; no replacement key is created.",
        );
    } finally {
      sending.current = false;
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  if (!ready) return null;
  if (!pending && !receipt && !offered && !recovery.comment && !corrupt)
    return null;
  const target = recovery.attempt;
  return (
    <section
      className="card evidence-override proposal-rejection"
      aria-labelledby="rejection-title"
    >
      <div className="card-heading">
        <ShieldX size={18} />
        <h3 id="rejection-title" tabIndex={-1}>
          Reject reviewed revision
        </h3>
        <span className="outline-tag">No execution</span>
      </div>
      <div className="card-content">
        <p className="muted small">
          Rejection is a terminal decision on one saved revision and pinned
          simulation. It releases this proposal’s statement reservations. It
          does not post a settlement, change original verdicts or establish
          accounting accuracy. The server rechecks reviewer independence, the
          grant waiting period and current dependencies.
        </p>
        <Button
          type="button"
          variant="secondary"
          disabled={busy}
          onClick={onInspect}
        >
          Inspect current pinned result
        </Button>
        <p className="small">
          {receipt
            ? "Confirmed response"
            : target
              ? "Original request"
              : "Review target"}
          : revision{" "}
          {receipt?.revision ?? target?.revision ?? proposal.revision} · version{" "}
          {receipt ? `"v${receipt.version}"` : (target?.etag ?? etag)}.
        </p>
        {(target || run) && (
          <details className="record-technical">
            <summary>Rejection target and pinned result</summary>
            <p>
              Simulation <code>{target?.simulationId ?? run?.id}</code>
            </p>
            <p>
              Input SHA-256 <code>{target?.inputHash ?? run?.input_hash}</code>
            </p>
            <p>
              Result SHA-256{" "}
              <code>{target?.resultHash ?? run?.result_hash}</code>
            </p>
            {!target && run && (
              <p>
                Original result: {String(run.outcome.aggregate)}. Any recorded
                evidence exception stays separate from this rejection.
              </p>
            )}
          </details>
        )}
        {runLoading && !pending && !receipt && (
          <p role="status">Verifying the pinned review target…</p>
        )}
        {runError && !pending && !receipt && (
          <div role="alert">
            <p>
              The pinned target could not be verified. No new rejection can be
              sent.
            </p>
            <Button
              variant="secondary"
              onClick={() => setRetryRead((n) => n + 1)}
            >
              Retry target read
            </Button>
          </div>
        )}
        {!candidate && !pending && !receipt && !runLoading && !runError && (
          <p className="notice">
            The server does not currently offer an eligible rejection for this
            view. Any saved comment is retained. Refresh the proposal or session
            to request current access.
          </p>
        )}
        {corrupt && (
          <p role="alert">
            Saved rejection recovery data could not be verified. No request will
            be sent under a replacement key. Ask an operator to reconcile this
            tab’s earlier attempt.
          </p>
        )}
        {storageError && (
          <div role="alert">
            <p>
              {receipt
                ? "Recovery storage could not be updated. The recorded receipt remains confirmed."
                : "Tab storage is unavailable. No rejection request will be sent until its recovery data can be saved."}
            </p>
            <Button
              type="button"
              variant="secondary"
              disabled={busy}
              onClick={() => remember(recovery)}
            >
              Retry tab storage
            </Button>
          </div>
        )}
        {peerBlocked && !pending && !receipt && (
          <p className="notice">
            The approval panel has an unresolved or confirmed decision, or
            requires recovery. No new rejection can start until that is resolved
            or the proposal is refreshed. Your unsent comment stays separate.
          </p>
        )}
        {message && (
          <div ref={status} role="status" className="notice" tabIndex={-1}>
            {message}
          </div>
        )}
        {receipt ? (
          <>
            <p className="override-receipt">
              Recorded response: REJECTED · revision {receipt.revision} ·
              version {receipt.version}
              {receipt.replayed ? " · idempotent replay" : ""}. This receipt
              records this decision; it does not establish current financial or
              source eligibility.
            </p>
            <Button disabled={busy} onClick={onReload}>
              Close and refresh proposal
            </Button>
          </>
        ) : (
          <form onSubmit={(e) => void send(e)}>
            <label htmlFor="rejection-comment">
              Rejection reason (required)
            </label>
            <textarea
              id="rejection-comment"
              rows={4}
              required
              aria-required="true"
              value={recovery.comment}
              disabled={busy || corrupt}
              readOnly={pending}
              aria-describedby="rejection-help"
              onChange={(e) => {
                if (Array.from(e.target.value).length <= 4000) {
                  remember({ ...recovery, comment: e.target.value });
                  setAck(false);
                }
              }}
            />
            {recovery.comment !== "" &&
              !validRejectionComment(recovery.comment) && (
                <p role="alert">
                  A nonblank reason of at most 4,000 well-formed characters is
                  required, with no NUL character.
                </p>
              )}
            <p id="rejection-help" className="muted small">
              Plain text · {Array.from(recovery.comment).length}/4,000
              characters. Comment and original request details stay in this tab
              without session credentials. Keep the tab open until confirmed;
              closing it can lose recovery details.
            </p>
            {!pending && (
              <label className="override-confirm">
                <input
                  type="checkbox"
                  checked={ack}
                  disabled={
                    busy || corrupt || blocked || peerBlocked || !candidate
                  }
                  onChange={(e) => setAck(e.target.checked)}
                />
                <span>
                  I reviewed revision {proposal.revision} and its pinned
                  simulation. I understand rejection is terminal and releases
                  this proposal’s reservations. It does not execute a settlement
                  or establish accounting accuracy.
                </span>
              </label>
            )}
            {pending && !rejectionRetryWindow(target!) && (
              <p role="alert">
                Safe retry window ended. Operator reconciliation is required; no
                replacement key will be generated.
              </p>
            )}
            <div className="override-actions">
              <Button
                type="submit"
                disabled={
                  busy ||
                  storageError ||
                  corrupt ||
                  !validRejectionComment(recovery.comment) ||
                  (!pending &&
                    (!candidate || blocked || peerBlocked || !ack)) ||
                  (pending && !rejectionRetryWindow(target!))
                }
              >
                {busy
                  ? "Waiting for confirmation…"
                  : pending
                    ? "Retry original rejection"
                    : "Reject reviewed revision"}
              </Button>
            </div>
          </form>
        )}
        {current && (
          <p className="override-receipt">
            Latest read: {current.state} · version {current.version} ·{" "}
            {current.records} review records loaded
            {current.more ? " (more history exists)" : ""}. Current state is
            separate from the original receipt.
          </p>
        )}
        <div className="override-actions">
          <Button
            type="button"
            variant="secondary"
            disabled={busy}
            onClick={() => void readCurrent()}
          >
            Read current state
          </Button>
          <Button
            type="button"
            variant="ghost"
            disabled={busy}
            onClick={onSessionRefresh}
          >
            Refresh session
          </Button>
          {blocked && !receipt && (
            <Button
              type="button"
              variant="secondary"
              disabled={busy}
              onClick={onReload}
            >
              Refresh proposal
            </Button>
          )}
        </div>
      </div>
    </section>
  );
}

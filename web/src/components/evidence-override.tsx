"use client";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { ShieldAlert } from "lucide-react";
import { api } from "@/lib/api";
import {
  problemCode,
  problemReason,
  type Proposal,
  type Simulation,
} from "@/lib/proposal-workflow";
import {
  overrideCandidate,
  overrideReceipt,
  overrideRetryWindow,
  overrideStorageKey,
  readOverrideRecovery,
  validReason,
  definitiveOverrideRefusal,
  type OverrideRecovery,
} from "@/lib/override-recovery";
import { Button } from "./button";

export function EvidenceOverride({
  proposal,
  etag,
  userId,
  tenantId,
  csrfToken,
  onReload,
  onSessionRefresh,
}: {
  proposal: Proposal;
  etag: string;
  userId: string;
  tenantId: string;
  csrfToken: string;
  onReload: () => void;
  onSessionRefresh: () => void;
}) {
  const scope = {
    tenantId,
    userId,
    proposalId: proposal.id,
    ledgerId: proposal.ledger_id,
  };
  const storage = overrideStorageKey(scope);
  const [recovery, setRecovery] = useState<OverrideRecovery>({
    schema: 1,
    ...scope,
    reason: "",
    attempt: null,
    blockedVersion: null,
  });
  const [ready, setReady] = useState(false),
    [storageError, setStorageError] = useState(false),
    [corrupt, setCorrupt] = useState(false),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  const [run, setRun] = useState<Simulation | null>(null),
    [runError, setRunError] = useState(false),
    [runLoading, setRunLoading] = useState(true),
    [refresh, setRefresh] = useState(0),
    [ack, setAck] = useState(false);
  const [receipt, setReceipt] = useState<{
      version: number;
      revision: number;
      replayed: boolean;
    } | null>(null),
    [current, setCurrent] = useState<{
      state: string;
      version: number;
      records: number;
    } | null>(null);
  const active = useRef<AbortController | null>(null),
    sending = useRef(false),
    status = useRef<HTMLDivElement>(null);
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(storage);
      const saved = readOverrideRecovery(raw, scope);
      if (saved) {
        setRecovery(saved);
        if (saved.attempt)
          setMessage(
            "An earlier request is unconfirmed. Its exact reason, key and original version are preserved. Read current state or retry only that original request.",
          );
      } else if (raw !== null) setCorrupt(true);
    } catch {
      setStorageError(true);
    }
    setReady(true);
    return () => active.current?.abort();
    // The verified organization/user and proposal own this component's lifetime.
  }, [storage, tenantId, userId, proposal.id, proposal.ledger_id]);
  useEffect(() => {
    const controller = new AbortController();
    setRun(null);
    setRunError(false);
    setRunLoading(true);
    void (async () => {
      try {
        if (!proposal.latest_simulation_id) return;
        const r = await api.GET("/api/v1/simulations/{simulation_id}", {
          signal: controller.signal,
          params: { path: { simulation_id: proposal.latest_simulation_id } },
        });
        if (controller.signal.aborted) return;
        if (
          !r.data ||
          r.data.id !== proposal.latest_simulation_id ||
          r.data.proposal_id !== proposal.id ||
          r.data.ledger_id !== proposal.ledger_id ||
          r.data.revision !== proposal.revision
        ) {
          setRunError(true);
          return;
        }
        setRun(r.data);
      } catch {
        if (!controller.signal.aborted) setRunError(true);
      } finally {
        if (!controller.signal.aborted) setRunLoading(false);
      }
    })();
    return () => controller.abort();
  }, [
    proposal.latest_simulation_id,
    proposal.id,
    proposal.ledger_id,
    proposal.revision,
    refresh,
  ]);
  function remember(value: OverrideRecovery) {
    setRecovery(value);
    try {
      if (value.reason || value.attempt)
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
  const candidate = overrideCandidate(proposal, run, userId, etag),
    pending = recovery.attempt !== null,
    blocked = recovery.blockedVersion === proposal.version;
  async function readCurrent() {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    const controller = new AbortController();
    active.current = controller;
    try {
      const opts = {
        signal: controller.signal,
        params: { path: { proposal_id: proposal.id } },
      };
      const [head, history] = await Promise.all([
        api.GET("/api/v1/proposals/{proposal_id}", opts),
        api.GET("/api/v1/proposals/{proposal_id}/reviews", {
          ...opts,
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
          (i) =>
            i.proposal_id !== proposal.id || i.ledger_id !== proposal.ledger_id,
        )
      )
        throw Error();
      setCurrent({
        state: head.data.state,
        version: head.data.version,
        records: history.data.items.length,
      });
      announce(
        "Current state was read. This does not confirm whether the original request committed, or authorize a new request key. Use its original retry to obtain the recorded response.",
      );
    } catch {
      if (!controller.signal.aborted)
        announce(
          "Current state could not be verified. Refresh your session or connection. The original request remains preserved.",
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
      !validReason(recovery.reason) ||
      (!pending && (!candidate || blocked || !ack))
    )
      return;
    const a = recovery.attempt ?? {
      key: crypto.randomUUID(),
      etag,
      revision: proposal.revision,
      simulationId: proposal.latest_simulation_id!,
      startedAt: Date.now(),
    };
    if (!overrideRetryWindow(a)) {
      announce(
        "The safe retry window has ended. No retry or replacement key will be sent. Ask an operator to reconcile this preserved request before another action.",
      );
      return;
    }
    const value = { ...recovery, attempt: a };
    if (!remember(value)) {
      announce(
        "Tab storage is unavailable. No request was sent. Restore tab storage before continuing so the original request can survive a reload.",
      );
      return;
    }
    sending.current = true;
    setBusy(true);
    setCurrent(null);
    const controller = new AbortController();
    active.current = controller;
    try {
      const r = await api.POST("/api/v1/proposals/{proposal_id}/overrides", {
        signal: controller.signal,
        params: { path: { proposal_id: proposal.id } },
        body: { policy_ids: ["P-EVD-01"], reason: value.reason },
        headers: {
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": a.key,
          "If-Match": a.etag,
        },
      });
      if (controller.signal.aborted) return;
      if (r.response.status === 200 && overrideReceipt(r.data, scope, a)) {
        setReceipt({
          version: r.data!.version,
          revision: r.data!.revision,
          replayed: r.response.headers.get("Idempotent-Replayed") === "true",
        });
        remember({ ...value, attempt: null, reason: "", blockedVersion: null });
        announce(
          "The recorded exception is confirmed. READY_FOR_REVIEW is its point-in-time response. Original verdicts and financial records stay unchanged; this is not approval or execution.",
        );
      } else {
        const code = problemCode(r.error),
          reason = problemReason(r.error);
        if (definitiveOverrideRefusal(r.response.status, code)) {
          remember({
            ...value,
            attempt: null,
            blockedVersion: proposal.version,
          });
          setAck(false);
          announce(
            reason === "FULL_CAPTURE_CHANGED"
              ? "The ledger capture changed. This request was refused. The maker must use the supported withdrawal and fresh-proposal path; this form cannot force a recapture."
              : reason === "FAIL_CANNOT_BE_OVERRIDDEN"
                ? "FAIL cannot be overridden. This request was refused."
                : code === "PROPOSAL_STALE"
                  ? "Pinned dependencies changed. This request was refused. The maker must revalidate before continuing."
                  : r.response.status === 412
                    ? "The proposal version changed. This request was refused. Refresh and inspect the current proposal before another explicit attempt."
                    : "This request was refused by the supported policy or state gates. Refresh the proposal and inspect its evidence before another explicit attempt.",
          );
        } else
          announce(
            r.response.status === 401 || r.response.status === 403
              ? "Session or permission verification failed. This does not settle an earlier request. Refresh your session; its original key, version and reason remain preserved."
              : "The result is unconfirmed. Read current state, then explicitly retry the exact original request. Its reason, version and key remain locked.",
          );
      }
    } catch {
      if (!controller.signal.aborted)
        announce(
          "The response was interrupted. The request may have committed. Read current state or retry the exact original request; no replacement key is created.",
        );
    } finally {
      sending.current = false;
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  if (!ready) return null;
  if (
    !pending &&
    !receipt &&
    !candidate &&
    !runError &&
    !runLoading &&
    !recovery.reason
  )
    return null;
  if (!pending && !receipt && proposal.state !== "BLOCKED" && !recovery.reason)
    return null;
  return (
    <section
      className="card evidence-override"
      aria-labelledby="override-title"
    >
      <div className="card-heading">
        <ShieldAlert size={18} />
        <h3 id="override-title" tabIndex={-1}>
          Controller evidence exception
        </h3>
        <span className="outline-tag">No approval or posting</span>
      </div>
      <div className="card-content">
        <p className="muted small">
          Only the sole P-EVD-01 insufficiency on this native sandbox settlement
          is supported. A FAIL cannot be overridden. Your reason becomes an
          immutable controller record; synthetic evidence and the original
          verdict stay unchanged.
        </p>
        <p className="small">
          Revision {recovery.attempt?.revision ?? proposal.revision} · version{" "}
          {recovery.attempt?.etag ?? etag}. The server rechecks permissions,
          proposal version and captured dependencies.
        </p>
        {runLoading && !pending && !receipt ? (
          <p role="status">Checking the pinned policy result…</p>
        ) : runError && !pending && !receipt ? (
          <div role="alert">
            <p>
              The pinned result could not be verified. No new exception can be
              sent.
            </p>
            <Button
              variant="secondary"
              onClick={() => setRefresh((n) => n + 1)}
            >
              Retry policy read
            </Button>
          </div>
        ) : null}
        {!candidate && !pending && !receipt && !runLoading && !runError && (
          <p className="notice">
            This proposal is outside the supported exception scope. Your saved
            reason is retained; no new request can be sent.
          </p>
        )}
        {corrupt && (
          <p role="alert">
            Saved recovery data could not be verified. No request will be sent
            with a new key. Ask an operator to reconcile this tab’s earlier
            attempt.
          </p>
        )}
        {storageError && (
          <p role="alert">
            Tab storage is unavailable. New requests are blocked until their
            recovery details can be saved.
          </p>
        )}
        {message && (
          <div ref={status} className="notice" role="status" tabIndex={-1}>
            {message}
          </div>
        )}
        {receipt ? (
          <>
            <p className="override-receipt">
              Recorded response: READY_FOR_REVIEW · revision {receipt.revision}{" "}
              · version {receipt.version}
              {receipt.replayed ? " · idempotent replay" : ""}. Current state
              may have changed since this receipt.
            </p>
            <Button onClick={onReload}>Close and refresh proposal</Button>
          </>
        ) : (
          <form onSubmit={(e) => void send(e)}>
            <label htmlFor="override-reason">
              Written reason for accepting insufficient evidence
            </label>
            <textarea
              id="override-reason"
              rows={4}
              value={recovery.reason}
              readOnly={pending}
              disabled={busy || corrupt}
              aria-describedby="override-help"
              onChange={(e) => {
                if (Array.from(e.target.value).length <= 4000) {
                  remember({ ...recovery, reason: e.target.value });
                  setAck(false);
                }
              }}
            />
            <p id="override-help" className="muted small">
              Plain text · {Array.from(recovery.reason).length}/4,000
              characters. Reason and retry details stay in this browser tab
              without session credentials. Unconfirmed requests lock their
              original text. Keep this tab open until confirmed; closing it can
              lose recovery details.
            </p>
            {!pending && (
              <label className="override-confirm">
                <input
                  type="checkbox"
                  checked={ack}
                  disabled={busy || !candidate || blocked || corrupt}
                  onChange={(e) => setAck(e.target.checked)}
                />
                <span>
                  I reviewed this revision and understand this records an
                  evidence exception only. It does not approve or post the
                  settlement.
                </span>
              </label>
            )}
            {pending && !overrideRetryWindow(recovery.attempt!) && (
              <p role="alert">
                Safe retry window ended. Reconciliation is required; no
                replacement key will be generated.
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
                type="submit"
                disabled={
                  busy ||
                  corrupt ||
                  (!pending && (!candidate || blocked || !ack)) ||
                  !validReason(recovery.reason) ||
                  (pending && !overrideRetryWindow(recovery.attempt!))
                }
              >
                {busy
                  ? "Waiting for response…"
                  : pending
                    ? "Retry original exception"
                    : "Record evidence exception"}
              </Button>
            </div>
          </form>
        )}
        {current && (
          <p className="override-receipt">
            Latest read: {current.state} · version {current.version} ·{" "}
            {current.records} exception records loaded. This is separate from
            the original request’s receipt.
          </p>
        )}
        <div className="override-actions">
          <Button
            type="button"
            variant="ghost"
            disabled={busy}
            onClick={onSessionRefresh}
          >
            Refresh session
          </Button>
          {blocked && (
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

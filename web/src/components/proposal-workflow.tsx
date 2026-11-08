"use client";
import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  ArrowUpRight,
  RotateCcw,
  ShieldCheck,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import {
  attemptStorage,
  parseAttempt,
  retryWindow,
  uncertainResponse,
  workflowError,
  problemCode,
  problemReason,
  record,
  type Action,
  type Attempt,
  type Proposal,
} from "@/lib/proposal-workflow";
import { Button } from "./button";

export function ProposalWorkflow({
  proposal,
  etag,
  csrfToken,
  userId,
  tenantId,
  onChanged,
}: {
  proposal: Proposal;
  etag: string;
  csrfToken: string;
  userId: string;
  tenantId: string;
  onChanged: () => void;
}) {
  const [action, setAction] = useState<Action | null>(null),
    [restored, setRestored] = useState<Attempt | null>(null);
  const [recoveryChecked, setRecoveryChecked] = useState(false);
  const storage = attemptStorage(userId, tenantId, proposal.id);
  useEffect(() => {
    try {
      const a = parseAttempt(sessionStorage.getItem(storage), proposal.id);
      setRestored(a);
    } catch {
      /* Private mode may deny storage. In-memory retry still works. */
    } finally {
      setRecoveryChecked(true);
    }
  }, [storage, proposal.id]);
  const can = (a: string) => !!etag && proposal.allowed_actions.includes(a);
  function close() {
    setAction(null);
    try {
      setRestored(parseAttempt(sessionStorage.getItem(storage), proposal.id));
    } catch {
      setRestored(null);
    }
    onChanged();
  }
  return (
    <>
      {can("SUBMIT") && (
        <Button
          disabled={!recoveryChecked || !!action || !!restored}
          onClick={() => setAction("submit")}
        >
          <ArrowUpRight size={16} />
          Submit for checks
        </Button>
      )}
      {(
        [
          ["RESIMULATE", "resimulate", "Re-simulate saved content"],
          ["REVALIDATE", "revalidate", "Check dependencies"],
        ] as const
      ).map(
        ([permission, verb, label]) =>
          can(permission) && (
            <Button
              key={verb}
              variant="secondary"
              disabled={!recoveryChecked || !!action || !!restored}
              onClick={() => setAction(verb)}
            >
              <RotateCcw size={15} />
              {label}
            </Button>
          ),
      )}
      {can("WITHDRAW") && (
        <Button
          variant="secondary"
          disabled={!recoveryChecked || !!action || !!restored}
          onClick={() => setAction("withdraw")}
        >
          <RotateCcw size={15} />
          Withdraw proposal
        </Button>
      )}
      {restored && !action && (
        <Button variant="secondary" onClick={() => setAction(restored.action)}>
          Resume unconfirmed {restored.action}
        </Button>
      )}
      {action && (
        <WorkflowDialog
          action={action}
          proposal={proposal}
          etag={etag}
          csrfToken={csrfToken}
          storage={storage}
          restored={restored}
          onClose={close}
        />
      )}
    </>
  );
}
function WorkflowDialog({
  action,
  proposal,
  etag,
  csrfToken,
  storage,
  restored,
  onClose,
}: {
  action: Action;
  proposal: Proposal;
  etag: string;
  csrfToken: string;
  storage: string;
  restored: Attempt | null;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null),
    focus = useRef<HTMLDivElement>(null),
    busyRef = useRef(false),
    request = useRef<AbortController | null>(null);
  const [attempt, setAttempt] = useState<Attempt | null>(restored),
    [busy, setBusy] = useState(false),
    [checking, setChecking] = useState(false);
  const [phase, setPhase] = useState<
    "confirm" | "uncertain" | "refused" | "done"
  >(restored ? "uncertain" : "confirm");
  const [message, setMessage] = useState(
    restored
      ? "A previous attempt was not confirmed in this tab. Its original retry key and version are preserved. Check the current proposal first."
      : "",
  );
  const [code, setCode] = useState(""),
    [current, setCurrent] = useState<Proposal | null>(null),
    [accepted, setAccepted] = useState<{
      state: string;
      version: number;
      replayed: boolean;
      freshness?: string;
      checkId?: string;
    } | null>(null);
  const [storageWarning, setStorageWarning] = useState(false);
  useEffect(() => {
    const opener = document.activeElement;
    dialog.current?.showModal();
    return () => {
      request.current?.abort();
      dialog.current?.close();
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
    };
  }, []);
  useEffect(() => {
    if (!attempt || phase === "done" || phase === "refused") return;
    const warn = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [attempt, phase]);
  function announce(value: string) {
    setMessage(value);
    requestAnimationFrame(() => focus.current?.focus());
  }
  function forget() {
    try {
      sessionStorage.removeItem(storage);
    } catch {
      /* no persisted data */
    }
  }
  async function check() {
    if (busyRef.current || checking) return;
    setChecking(true);
    const controller = new AbortController();
    request.current = controller;
    try {
      const r = await api.GET("/api/v1/proposals/{proposal_id}", {
        signal: controller.signal,
        params: { path: { proposal_id: proposal.id } },
      });
      if (controller.signal.aborted) return;
      if (
        !r.data ||
        r.data.id !== proposal.id ||
        r.data.ledger_id !== proposal.ledger_id
      ) {
        announce(
          "The current proposal could not be verified. Reconnect or refresh your session before continuing.",
        );
        return;
      }
      setCurrent(r.data);
      announce(
        `Current proposal: ${r.data.state}, version ${r.data.version}. This read does not settle the outcome of a request still running. Retry only the original attempt to obtain its recorded response.`,
      );
    } catch {
      if (!controller.signal.aborted)
        announce(
          "The current state could not be loaded. Your original retry key is retained.",
        );
    } finally {
      if (!controller.signal.aborted) setChecking(false);
    }
  }
  async function send() {
    if (busyRef.current || checking || phase === "done" || phase === "refused")
      return;
    const a = attempt ?? {
      action,
      proposalId: proposal.id,
      etag,
      key: crypto.randomUUID(),
      startedAt: Date.now(),
    };
    if (!retryWindow(a)) {
      announce(
        "The safe retry window has ended. No further retry will be sent from this dialog. Inspect current state and ask an operator to reconcile this attempt before any new action.",
      );
      return;
    }
    busyRef.current = true;
    setBusy(true);
    setAttempt(a);
    try {
      sessionStorage.setItem(storage, JSON.stringify(a));
    } catch {
      setStorageWarning(true);
    }
    const controller = new AbortController();
    request.current = controller;
    try {
      const opts = {
        signal: controller.signal,
        params: { path: { proposal_id: a.proposalId } },
        headers: {
          "If-Match": a.etag,
          "Idempotency-Key": a.key,
          "X-CSRF-Token": csrfToken,
        },
      };
      const r =
        a.action === "submit"
          ? await api.POST("/api/v1/proposals/{proposal_id}/submit", opts)
          : a.action === "resimulate"
            ? await api.POST("/api/v1/proposals/{proposal_id}/resimulate", opts)
            : a.action === "revalidate"
              ? await api.POST(
                  "/api/v1/proposals/{proposal_id}/revalidate",
                  opts,
                )
              : await api.POST(
                  "/api/v1/proposals/{proposal_id}/withdraw",
                  opts,
                );
      if (controller.signal.aborted) return;
      const p = r.data && ("proposal" in r.data ? r.data.proposal : r.data);
      const response = record(r.data),
        freshness = record(response.freshness);
      const baseVersion = Number(a.etag.slice(2, -1));
      const confirmed =
        a.action === "revalidate"
          ? typeof response.check_id === "string" &&
            ((freshness.status === "FRESH" &&
              p?.version === baseVersion &&
              ["SUBMITTED", "BLOCKED", "READY_FOR_REVIEW", "APPROVED"].includes(
                p.state,
              )) ||
              (freshness.status === "STALE" &&
                p?.version === baseVersion + 1 &&
                p.state === "STALE"))
          : p &&
            p.version === baseVersion + 1 &&
            (a.action === "withdraw"
              ? p.state === "WITHDRAWN"
              : ["BLOCKED", "READY_FOR_REVIEW"].includes(p.state));
      if (
        p &&
        p.id === proposal.id &&
        p.ledger_id === proposal.ledger_id &&
        confirmed
      ) {
        setAccepted({
          freshness:
            a.action === "revalidate" ? String(freshness.status) : undefined,
          checkId:
            typeof response.check_id === "string"
              ? response.check_id
              : undefined,
          state: p.state,
          version: p.version,
          replayed: r.response.headers.get("Idempotent-Replayed") === "true",
        });
        setPhase("done");
        forget();
        // Never install a cached head: it may predate a later withdrawal in another tab.
        const live = await api.GET("/api/v1/proposals/{proposal_id}", {
          signal: controller.signal,
          params: { path: { proposal_id: proposal.id } },
        });
        if (controller.signal.aborted) return;
        if (
          live.data &&
          live.data.id === proposal.id &&
          live.data.ledger_id === proposal.ledger_id
        ) {
          setCurrent(live.data);
          announce(
            `Attempt confirmed. Current state: ${live.data.state}, version ${live.data.version}. Financial records were not changed.`,
          );
        } else
          announce(
            "The attempt was confirmed, but the current proposal could not be refreshed. Close and reload before any further action.",
          );
      } else {
        const uncertain = uncertainResponse(r.response.status, r.error);
        setPhase(uncertain ? "uncertain" : "refused");
        setCode(
          [problemCode(r.error), problemReason(r.error)]
            .filter(Boolean)
            .join(" · "),
        );
        if (!uncertain) forget();
        announce(workflowError(r.response.status, r.error));
      }
    } catch {
      if (!controller.signal.aborted) {
        // A confirmed success remains confirmed even if its following current-state read fails.
        setPhase((old) => (old === "done" ? old : "uncertain"));
        announce(
          "The connection ended. Check current state; an unconfirmed attempt keeps its original retry key. A confirmed response is retained above.",
        );
      }
    } finally {
      busyRef.current = false;
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="draft-editor-dialog workflow-dialog"
      aria-labelledby="workflow-title"
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        const controls = [
          ...event.currentTarget.querySelectorAll<HTMLButtonElement>(
            "button:not([disabled])",
          ),
        ].filter((el) => el.getClientRects().length > 0);
        const first = controls[0],
          last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }}
      onCancel={(e) => {
        e.preventDefault();
        if (!busy && !checking) onClose();
      }}
    >
      <header className="editor-header">
        <div>
          <span className="eyebrow">
            {phase === "done"
              ? "ATTEMPT CONFIRMED"
              : "EXPLICIT WORKFLOW ACTION"}
          </span>
          <h2 id="workflow-title">
            {action === "submit"
              ? "Submit saved revision for checks"
              : action === "resimulate"
                ? "Re-simulate saved content"
                : action === "revalidate"
                  ? "Check pinned dependencies"
                  : "Withdraw this proposal"}
          </h2>
          <p>
            Revision {proposal.revision} · based on version{" "}
            {attempt?.etag ?? etag}
          </p>
        </div>
        <button
          className="icon-button"
          aria-label="Close workflow dialog"
          disabled={busy || checking}
          onClick={onClose}
        >
          <X size={20} />
        </button>
      </header>
      <div className="editor-body">
        <div className="notice">
          <ShieldCheck size={20} />
          <span>
            {action === "submit"
              ? "This pins a prediction and reserves cited statements. It does not post a settlement. The saved revision is submitted; no unsaved edits are included."
              : action === "resimulate"
                ? "This creates a new immutable revision with exactly the same saved content and checks current supported dependencies. Previous runs remain unchanged. Closed-period and evidence gates still apply; this does not post a settlement."
                : action === "revalidate"
                  ? "This explicitly compares supported native dependencies with the pinned result. Changed inputs mark the proposal STALE and release its statement reservations atomically. An unchanged result is a point-in-time check, never approval or execution eligibility."
                  : "This ends the proposal and releases its statement reservations atomically. It cannot be reopened as a draft. Any existing approval, simulation and immutable revisions remain available. Withdrawal does not reverse an executed settlement."}
          </span>
        </div>
        <div className="workflow-summary">
          <strong>{proposal.operations[0]?.payout_id}</strong>
          <span>
            {proposal.operations.length} operation ·{" "}
            {proposal.evidence_refs.length} evidence references
          </span>
          <p>
            SANDBOX · Accounting {proposal.accounting_status}. This action does
            not approve or execute a settlement.
          </p>
        </div>
        {message && (
          <div
            ref={focus}
            tabIndex={-1}
            className={`notice ${phase === "refused" ? "error" : ""}`}
            role={phase === "refused" ? "alert" : "status"}
          >
            <AlertCircle size={18} />
            <span>
              {message}
              {code && <code className="workflow-code">{code}</code>}
            </span>
          </div>
        )}
        {accepted && (
          <p className="workflow-receipt">
            Recorded response: {accepted.state} · version {accepted.version}
            {accepted.replayed ? " · idempotent replay" : ""}. This is the
            attempt’s point-in-time response.
            {accepted.freshness && (
              <>
                {" "}
                Dependency check: <strong>{accepted.freshness}</strong>. It is
                not a continuing freshness guarantee. Replaying this attempt
                returns the original check.
              </>
            )}
            {accepted.checkId && (
              <code className="workflow-code">Check {accepted.checkId}</code>
            )}
          </p>
        )}
        {current && (
          <p className="workflow-receipt">
            <strong>
              Latest read: {current.state} · version {current.version}
            </strong>
            {current.state === "WITHDRAWN"
              ? " · no statement reservation remains owned by this proposal."
              : " · current state is separate from the recorded response."}
          </p>
        )}
        {storageWarning && (
          <p role="alert">
            Browser storage is unavailable. Keep this dialog open until the
            attempt is confirmed; reload recovery cannot be saved.
          </p>
        )}
        {phase === "uncertain" && (
          <p className="muted">
            Checking state sends no workflow action. Retrying uses the original
            key, original version and empty body. Closing preserves only retry
            metadata in this tab, without session tokens or financial content.
          </p>
        )}
      </div>
      <footer className="editor-footer">
        <span>
          {busy
            ? "Waiting for confirmation…"
            : checking
              ? "Reading current state…"
              : "No financial posting"}
        </span>
        <div>
          <Button
            variant="secondary"
            disabled={busy || checking}
            onClick={onClose}
          >
            {phase === "done" || phase === "refused"
              ? "Close and refresh"
              : "Close"}
          </Button>
          {phase !== "confirm" && (
            <Button
              variant="secondary"
              disabled={busy || checking}
              onClick={() => void check()}
            >
              Check current state
            </Button>
          )}
          {(phase === "confirm" || phase === "uncertain") && (
            <Button
              disabled={
                busy || checking || (!!attempt && !retryWindow(attempt))
              }
              onClick={() => void send()}
            >
              {busy
                ? "Waiting…"
                : phase === "uncertain"
                  ? "Retry original attempt"
                  : action === "submit"
                    ? "Submit saved revision"
                    : action === "resimulate"
                      ? "Create revision and re-simulate"
                      : action === "revalidate"
                        ? "Check dependencies now"
                        : "Confirm withdrawal"}
            </Button>
          )}
        </div>
      </footer>
    </dialog>
  );
}

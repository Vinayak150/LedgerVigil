"use client";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { CheckCircle2, LockKeyhole } from "lucide-react";
import { api } from "@/lib/api";
import {
  problemCode,
  problemReason,
  type Proposal,
  type Simulation,
} from "@/lib/proposal-workflow";
import { formatAmount } from "@/lib/money-format";
import { approvalStorageKey } from "@/lib/approval-recovery";
import { rejectionStorageKey } from "@/lib/rejection-recovery";
import { peerReviewBlocked } from "@/lib/review-interlock";
import {
  executionStorageKey,
  readExecutionRecovery,
  executionRetryWindow,
  executionBody,
  canStartExecution,
  executionReceipt,
  originalExecutionReceipt,
  receiptMatchesHead,
  definitiveExecutionRefusal,
  type ExecutionReceipt,
  type ExecutionRecovery,
} from "@/lib/execution-recovery";
import { Button } from "./button";
import { ExecutionProofDownload } from "./execution-proof-download";

type Props = {
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
};
export function ProposalExecution({
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
}: Props) {
  const scope = {
      tenantId,
      userId,
      proposalId: proposal.id,
      ledgerId: proposal.ledger_id,
    },
    storage = executionStorageKey(scope);
  const [recovery, setRecovery] = useState<ExecutionRecovery>({
    schema: 1,
    ...scope,
    attempt: null,
    refusal: null,
  });
  const [ready, setReady] = useState(false),
    [corrupt, setCorrupt] = useState(false),
    [storageError, setStorageError] = useState(false),
    [busy, setBusy] = useState(false),
    [ack, setAck] = useState(false),
    [message, setMessage] = useState("");
  const [run, setRun] = useState<Simulation | null>(null),
    [targetError, setTargetError] = useState(false),
    [targetLoading, setTargetLoading] = useState(false),
    [targetTick, setTargetTick] = useState(0);
  const [receipt, setReceipt] = useState<ExecutionReceipt | null>(null),
    [receiptSource, setReceiptSource] = useState(""),
    [readReady, setReadReady] = useState(false);
  const sending = useRef(false),
    active = useRef<AbortController | null>(null),
    status = useRef<HTMLDivElement>(null);
  const offered = proposal.allowed_actions.includes("EXECUTE"),
    pending = !!recovery.attempt,
    candidate = canStartExecution(proposal, run, userId, etag);
  function announce(text: string) {
    setMessage(text);
    requestAnimationFrame(() => status.current?.focus());
  }
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(storage),
        saved = readExecutionRecovery(raw, scope);
      if (saved) setRecovery(saved);
      else if (raw !== null) setCorrupt(true);
    } catch {
      setStorageError(true);
    }
    setReady(true);
    return () => active.current?.abort();
    // The verified user/tenant/proposal key remounts this panel.
  }, [storage, tenantId, userId, proposal.id, proposal.ledger_id]);
  useEffect(() => {
    if (!offered || !proposal.latest_simulation_id) {
      setRun(null);
      setTargetLoading(false);
      setTargetError(false);
      return;
    }
    const controller = new AbortController();
    setRun(null);
    setAck(false);
    setTargetLoading(true);
    setTargetError(false);
    void api
      .GET("/api/v1/simulations/{simulation_id}", {
        signal: controller.signal,
        params: { path: { simulation_id: proposal.latest_simulation_id } },
      })
      .then((r) => {
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
      })
      .catch(() => {
        if (!controller.signal.aborted) setTargetError(true);
      })
      .finally(() => {
        if (!controller.signal.aborted) setTargetLoading(false);
      });
    return () => controller.abort();
  }, [
    offered,
    proposal.latest_simulation_id,
    proposal.id,
    proposal.ledger_id,
    proposal.revision,
    targetTick,
  ]);
  useEffect(() => {
    onLockChange(
      busy ||
        (pending && !recovery.refusal) ||
        !!receipt ||
        corrupt ||
        storageError,
    );
  }, [
    busy,
    pending,
    recovery.refusal,
    receipt,
    corrupt,
    storageError,
    onLockChange,
  ]);
  function remember(value: ExecutionRecovery) {
    try {
      const raw = sessionStorage.getItem(storage),
        previous = readExecutionRecovery(raw, scope);
      if (raw !== null && !previous) {
        setCorrupt(true);
        return false;
      }
      if (previous?.attempt && previous.attempt.key !== value.attempt?.key) {
        setRecovery(previous);
        announce(
          "Another original execution request is already saved in this tab. It has not been replaced. Read its receipt before continuing.",
        );
        return false;
      }
      sessionStorage.setItem(storage, JSON.stringify(value));
      setRecovery(value);
      setStorageError(false);
      return true;
    } catch {
      setStorageError(true);
      return false;
    }
  }
  function peersClear() {
    try {
      return (
        !peerBlocked &&
        !peerReviewBlocked(
          sessionStorage.getItem(approvalStorageKey(scope)),
          "approval",
          scope,
          proposal.version,
        ) &&
        !peerReviewBlocked(
          sessionStorage.getItem(rejectionStorageKey(scope)),
          "rejection",
          scope,
          proposal.version,
        )
      );
    } catch {
      setStorageError(true);
      return false;
    }
  }
  async function readCurrent() {
    if (sending.current) return;
    sending.current = true;
    setBusy(true);
    setReadReady(false);
    const controller = new AbortController();
    active.current = controller;
    try {
      const [head, found] = await Promise.all([
        api.GET("/api/v1/proposals/{proposal_id}", {
          signal: controller.signal,
          params: { path: { proposal_id: proposal.id } },
        }),
        api.GET("/api/v1/proposals/{proposal_id}/execution", {
          signal: controller.signal,
          params: { path: { proposal_id: proposal.id } },
        }),
      ]);
      if (controller.signal.aborted) return;
      if (
        !head.data ||
        head.data.id !== proposal.id ||
        head.data.ledger_id !== proposal.ledger_id
      )
        throw Error();
      if (found.data && receiptMatchesHead(found.data, scope, head.data)) {
        setReceipt(found.data);
        setReceiptSource("readback");
        if (head.data.version !== proposal.version) onReload();
        announce(
          "Committed settlement verified from its permanent receipt. Readback does not identify which concurrent request won. The saved original request is retained and no further execution will be sent.",
        );
      } else if (
        found.response.status === 404 &&
        head.data.state !== "EXECUTED"
      ) {
        const a = recovery.attempt;
        const retry =
          !!a &&
          !recovery.refusal &&
          executionRetryWindow(a) &&
          head.data.state === "APPROVED" &&
          head.data.revision === a.revision &&
          head.data.latest_simulation_id === a.simulationId &&
          head.data.version === Number(a.etag.slice(2, -1)) &&
          head.data.allowed_actions.includes("EXECUTE");
        setReadReady(retry);
        announce(
          retry
            ? "No committed receipt was found and the original target is still offered by the server. This does not prove an earlier request is absent or finished. You may explicitly retry only the saved original key, body and If-Match."
            : "No committed receipt was found in this read. No replacement request is authorized. Refresh the proposal or session; an expired or refused request must be reconciled before further action.",
        );
      } else throw Error();
    } catch {
      if (!controller.signal.aborted)
        announce(
          "The execution receipt could not be verified. Any saved original request remains locked. Restore your session or connection and read again; no replacement key is created.",
        );
    } finally {
      sending.current = false;
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  useEffect(() => {
    if (ready && (proposal.state === "EXECUTED" || pending)) void readCurrent();
    // Initial recovery and new server versions trigger readback, never a POST.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, proposal.id, proposal.version]);
  async function send(event: FormEvent) {
    event.preventDefault();
    if (
      !ready ||
      sending.current ||
      corrupt ||
      storageError ||
      receipt ||
      recovery.refusal ||
      (!pending && (!candidate || !ack)) ||
      (pending && (!readReady || !executionRetryWindow(recovery.attempt!)))
    )
      return;
    if (!peersClear()) {
      announce(
        "An approval or rejection request is unresolved in this tab. Resolve its original result and refresh before execution.",
      );
      return;
    }
    sending.current = true;
    setBusy(true);
    setReadReady(false);
    const controller = new AbortController();
    active.current = controller;
    let value = recovery;
    try {
      if (!pending) {
        const [head, pinned] = await Promise.all([
          api.GET("/api/v1/proposals/{proposal_id}", {
            signal: controller.signal,
            params: { path: { proposal_id: proposal.id } },
          }),
          api.GET("/api/v1/simulations/{simulation_id}", {
            signal: controller.signal,
            params: { path: { simulation_id: proposal.latest_simulation_id! } },
          }),
        ]);
        if (controller.signal.aborted) return;
        if (
          !head.data ||
          !pinned.data ||
          head.data.id !== proposal.id ||
          head.data.ledger_id !== proposal.ledger_id ||
          head.data.version !== proposal.version ||
          !canStartExecution(
            head.data,
            pinned.data,
            userId,
            head.response.headers.get("ETag") ?? "",
          ) ||
          pinned.data.input_hash !== run?.input_hash ||
          pinned.data.result_hash !== run?.result_hash
        ) {
          setAck(false);
          announce(
            "The current execution target could not be confirmed. No posting request was sent. Refresh the proposal and inspect the current revision.",
          );
          return;
        }
        if (!peersClear()) {
          announce(
            "Another review request needs recovery. No execution was sent.",
          );
          return;
        }
        value = {
          ...recovery,
          attempt: {
            key: crypto.randomUUID(),
            etag,
            revision: proposal.revision,
            simulationId: pinned.data.id,
            inputHash: pinned.data.input_hash,
            resultHash: pinned.data.result_hash,
            startedAt: Date.now(),
          },
          refusal: null,
        };
      }
      if (
        !value.attempt ||
        !executionRetryWindow(value.attempt) ||
        !remember(value)
      ) {
        announce(
          "The exact request could not be safely saved in tab storage. No execution request was sent. Restore storage before continuing.",
        );
        return;
      }
      const a = value.attempt;
      const r = await api.POST("/api/v1/proposals/{proposal_id}/execute", {
        signal: controller.signal,
        params: { path: { proposal_id: proposal.id } },
        body: executionBody(a),
        headers: {
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": a.key,
          "If-Match": a.etag,
        },
      });
      if (controller.signal.aborted) return;
      if (
        r.response.status === 200 &&
        originalExecutionReceipt(r.data, scope, a) &&
        executionReceipt(r.data, scope)
      ) {
        setReceipt(r.data);
        setReceiptSource(
          r.response.headers.get("Idempotent-Replayed") === "true"
            ? "replay"
            : "response",
        );
        announce(
          "Execution confirmed. The native settlement and ledger changes committed together. The original request is retained for traceability. Accounting remains UNREVIEWED.",
        );
        onReload();
      } else {
        const code = problemCode(r.error),
          reason = problemReason(r.error);
        if (definitiveExecutionRefusal(r.response.status, code)) {
          remember({
            ...value,
            refusal: { status: r.response.status, code, reason },
          });
          setAck(false);
          announce(
            code === "PROPOSAL_STALE"
              ? "The server marked this proposal STALE and released its claims. This request posted no settlement. Refresh the proposal; the maker must use the supported re-simulation flow."
              : reason === "FULL_CAPTURE_CHANGED"
                ? "The full ledger capture changed. This execution request posted no settlement. The maker can withdraw the approved proposal to release its statement, then create and submit a fresh proposal. Existing approval and simulation evidence are retained."
                : "The server definitively refused this original request under its version, state or validation gates. It will not be retried or replaced automatically. Read the receipt and refresh the proposal.",
          );
        } else
          announce(
            r.response.status === 401 || r.response.status === 403
              ? "Session or execution authorization failed. An earlier commit may still be unconfirmed. The original request is retained; refresh the session, then read the receipt."
              : "The execution outcome is unconfirmed. It may already have committed. Read the permanent receipt before considering a retry of the exact original request. No new key will be generated.",
          );
      }
    } catch {
      if (!controller.signal.aborted)
        announce(
          value.attempt
            ? "The response was interrupted. Execution may have committed. The saved original request remains locked. Read the receipt before any exact retry."
            : "The target read was interrupted. No execution request was sent. Refresh the proposal before continuing.",
        );
    } finally {
      sending.current = false;
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  function archiveRefusal() {
    if (
      busy ||
      !recovery.refusal ||
      !recovery.attempt ||
      !candidate ||
      proposal.version <= Number(recovery.attempt.etag.slice(2, -1))
    )
      return;
    try {
      const raw = sessionStorage.getItem(storage),
        saved = readExecutionRecovery(raw, scope);
      if (!saved || JSON.stringify(saved) !== JSON.stringify(recovery))
        throw Error();
      sessionStorage.setItem(
        storage + ":refused:" + recovery.attempt.key,
        raw!,
      );
      sessionStorage.removeItem(storage);
      setRecovery({ schema: 1, ...scope, attempt: null, refusal: null });
      setAck(false);
      setReadReady(false);
      setStorageError(false);
      announce(
        "The definitively refused request was archived in this tab. Inspect this newer target and confirm it explicitly before a new request is prepared.",
      );
    } catch {
      setStorageError(true);
      announce(
        "The refused request could not be archived safely. It remains locked; no new request was prepared.",
      );
    }
  }
  if (
    !ready ||
    (!offered &&
      !pending &&
      !receipt &&
      proposal.state !== "EXECUTED" &&
      !corrupt)
  )
    return null;
  const operation = proposal.operations[0],
    a = recovery.attempt;
  return (
    <section
      className="card evidence-override proposal-execution"
      aria-labelledby="execution-title"
    >
      <div className="card-heading">
        <LockKeyhole size={18} />
        <h3 id="execution-title">Native settlement execution</h3>
        <span className="outline-tag">Sandbox</span>
      </div>
      <div className="card-content">
        <p className="muted small">
          Execution posts the approved settlement to this native SANDBOX ledger.
          The server rechecks the exact approval, your independence and grant
          waiting period, current evidence and open period. Accounting remains
          UNREVIEWED.
        </p>
        {receipt ? (
          <>
            <p className="override-receipt">
              <CheckCircle2 size={18} aria-hidden="true" /> Committed · EXECUTED
              · ledger sequence {receipt.commit_seq}
            </p>
            <p>
              {receiptSource === "replay"
                ? "Original request replay confirmed."
                : receiptSource === "response"
                  ? "Original execution response confirmed."
                  : "Permanent settlement receipt verified by readback; request attribution is not inferred."}
            </p>
            <dl className="record-facts">
              <div>
                <dt>Revision / resulting version</dt>
                <dd>
                  {receipt.revision} / {receipt.resulting_version}
                </dd>
              </div>
              <div>
                <dt>Executed by</dt>
                <dd>
                  <code>{receipt.executed_by}</code>
                </dd>
              </div>
              <div>
                <dt>Posting comparison</dt>
                <dd>
                  MATCHED · {String(receipt.comparison.line_count)} lines ·
                  accounting UNREVIEWED
                </dd>
              </div>
            </dl>
            <details className="record-technical">
              <summary>Immutable execution record</summary>
              <p>
                Execution <code>{receipt.id}</code>
              </p>
              <p>
                Settlement <code>{receipt.settlement_document_id}</code>
              </p>
              <p>
                Journal entry <code>{receipt.entry_id}</code>
              </p>
              <p>
                Simulation <code>{receipt.simulation_id}</code>
              </p>
              <p>
                Predicted / derived SHA-256{" "}
                <code>{String(receipt.comparison.predicted_hash)}</code>
              </p>
            </details>
            <ExecutionProofDownload
              key={`${tenantId}:${receipt.id}`}
              disabled={busy}
              target={{
                tenantId,
                ledgerId: receipt.ledger_id,
                proposalId: receipt.proposal_id,
                revision: receipt.revision,
                executionId: receipt.id,
                simulationId: receipt.simulation_id,
                commitSeq: receipt.commit_seq,
              }}
            />
            <p className="small muted">
              Current balances are available in the command center for this
              ledger. The pinned simulation remains a historical prediction.
              This receipt does not establish independent accounting accuracy or
              delivery of the completion notification.
            </p>
          </>
        ) : (
          <>
            {operation && (
              <dl className="record-facts">
                <div>
                  <dt>Saved payout</dt>
                  <dd>
                    {operation.payout_id} · {operation.payout_date}
                  </dd>
                </div>
                {(["gross", "fee", "net"] as const).map((k) => (
                  <div key={k}>
                    <dt>
                      {k === "gross"
                        ? "Gross"
                        : k === "fee"
                          ? "Processor fee"
                          : "Net payout"}
                    </dt>
                    <dd>
                      {formatAmount(operation[k].amount, operation[k].currency)}{" "}
                      {operation[k].currency}
                    </dd>
                  </div>
                ))}
              </dl>
            )}
            <p className="small">
              {pending ? "Original request" : "Execution target"}: revision{" "}
              {a?.revision ?? proposal.revision} · version {a?.etag ?? etag}.
              Saved amounts above belong to the displayed revision; an older
              pending request retains its own revision and hashes.
            </p>
            {(a || run) && (
              <details className="record-technical">
                <summary>Pinned execution target</summary>
                <p>
                  Simulation <code>{a?.simulationId ?? run?.id}</code>
                </p>
                <p>
                  Input SHA-256 <code>{a?.inputHash ?? run?.input_hash}</code>
                </p>
                <p>
                  Result SHA-256{" "}
                  <code>{a?.resultHash ?? run?.result_hash}</code>
                </p>
                {a && (
                  <p>
                    Original request saved in this tab. No session credentials
                    are stored. Keep this tab open until resolved.
                  </p>
                )}
              </details>
            )}
            {targetLoading && !pending && (
              <p role="status">Verifying the pinned execution target…</p>
            )}
            {targetError && !pending && (
              <p role="alert">
                The pinned target could not be verified. Execution is
                unavailable.{" "}
                <Button
                  variant="secondary"
                  onClick={() => setTargetTick((n) => n + 1)}
                >
                  Retry target read
                </Button>
              </p>
            )}
            {recovery.refusal && (
              <p role="alert">
                Original request refused: {recovery.refusal.code}
                {recovery.refusal.reason ? ` · ${recovery.refusal.reason}` : ""}
                . It will not be retried automatically.
              </p>
            )}
            {pending && !executionRetryWindow(a!) && (
              <p role="alert">
                The safe retry window has ended. No posting retry or replacement
                key will be sent. The permanent receipt can still be read.
              </p>
            )}
            {!pending && peerBlocked && (
              <p className="notice">
                A review request needs recovery or refresh before execution can
                start.
              </p>
            )}
            {!pending &&
              offered &&
              !candidate &&
              !targetLoading &&
              !targetError && (
                <p className="notice">
                  The exact native target could not be verified. Refresh the
                  proposal before execution.
                </p>
              )}
            <form onSubmit={(e) => void send(e)}>
              {!pending && (
                <label className="override-confirm">
                  <input
                    type="checkbox"
                    checked={ack}
                    disabled={
                      busy ||
                      corrupt ||
                      storageError ||
                      peerBlocked ||
                      !candidate
                    }
                    onChange={(e) => setAck(e.target.checked)}
                  />
                  <span>
                    I reviewed revision {proposal.revision}, the pinned result
                    and its recorded approval. I authorize this settlement to
                    post to the native SANDBOX ledger.
                  </span>
                </label>
              )}
              <div className="override-actions">
                <Button
                  type="submit"
                  disabled={
                    busy ||
                    corrupt ||
                    storageError ||
                    peerBlocked ||
                    !!recovery.refusal ||
                    (pending
                      ? !readReady || !executionRetryWindow(a!)
                      : !candidate || !ack)
                  }
                >
                  {busy
                    ? "Verifying…"
                    : pending
                      ? "Retry original execution"
                      : "Execute approved settlement"}
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy}
                  onClick={onInspect}
                >
                  Inspect pinned result
                </Button>
              </div>
            </form>
            {recovery.refusal &&
              candidate &&
              a &&
              proposal.version > Number(a.etag.slice(2, -1)) && (
                <Button
                  variant="secondary"
                  disabled={busy || corrupt}
                  onClick={archiveRefusal}
                >
                  Archive refused request and review newer target
                </Button>
              )}
          </>
        )}
        {corrupt && (
          <p role="alert">
            Saved execution recovery data could not be verified. It has not been
            overwritten. No posting request will be sent; reconcile this tab’s
            earlier request first.
          </p>
        )}
        {storageError && (
          <div role="alert">
            <p>
              Tab recovery storage is unavailable. No new execution request will
              be sent. A displayed verified receipt remains valid.
            </p>
            <Button
              variant="secondary"
              disabled={busy}
              onClick={() => remember(recovery)}
            >
              Retry tab storage
            </Button>
          </div>
        )}
        {message && (
          <div ref={status} role="status" className="notice" tabIndex={-1}>
            {message}
          </div>
        )}
        <div className="override-actions">
          <Button
            variant="secondary"
            disabled={busy}
            onClick={() => void readCurrent()}
          >
            Read execution receipt
          </Button>
          <Button variant="secondary" disabled={busy} onClick={onReload}>
            Refresh proposal
          </Button>
          <Button variant="ghost" disabled={busy} onClick={onSessionRefresh}>
            Refresh session
          </Button>
        </div>
      </div>
    </section>
  );
}

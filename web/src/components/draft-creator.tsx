"use client";
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  FilePlus2,
  LockKeyhole,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type { components } from "@/lib/api.generated";
import {
  createBody,
  createFailure,
  evidenceKey,
  type Ledger,
  type Statement,
  type Payment,
  type Evidence,
  type CreateBody,
} from "@/lib/draft-create";
import {
  fieldIssues,
  issueLabel,
  issueText,
  type FieldIssue,
} from "@/lib/draft-edit";
import { formatAmount } from "@/lib/money-format";
import { LedgerPicker, StatementPicker, PaymentPicker } from "./draft-sources";
import { Button } from "./button";
type Batch = components["schemas"]["ImportBatch"];
type Attempt = { key: string; body: CreateBody };
export function DraftCreator({
  csrfToken,
  onClose,
  onCreated,
}: {
  csrfToken: string;
  onClose: () => void;
  onCreated: (p: components["schemas"]["Proposal"]) => void;
}) {
  const [ledger, setLedger] = useState<Ledger | null>(null),
    [pendingLedger, setPendingLedger] = useState<Ledger | null>(null);
  const [batch, setBatch] = useState<Batch | null>(null),
    [statement, setStatement] = useState<Statement | null>(null),
    [payments, setPayments] = useState<Payment[]>([]);
  const [refs, setRefs] = useState<string[]>([]),
    [rationale, setRationale] = useState("");
  const [review, setReview] = useState(false),
    [pending, setPending] = useState(false),
    [uncertain, setUncertain] = useState(false),
    [discard, setDiscard] = useState(false);
  const [error, setError] = useState(""),
    [issues, setIssues] = useState<FieldIssue[]>([]),
    [retryAt, setRetryAt] = useState(0),
    [now, setNow] = useState(0);
  const bodyView = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLDialogElement>(null),
    announcement = useRef<HTMLDivElement>(null),
    discardFocus = useRef<HTMLDivElement>(null),
    title = useRef<HTMLHeadingElement>(null);
  const active = useRef<AbortController | null>(null),
    inFlight = useRef(false),
    attempt = useRef<Attempt | null>(null);
  const dirty = !!statement || !!payments.length || !!rationale;
  const options: Evidence[] = [
    ...(statement
      ? [statement.evidence_ref, statement.source_record_evidence_ref]
      : []),
    ...payments.map((p) => p.evidence_ref),
  ];
  const evidence = options.filter((e) => refs.includes(evidenceKey(e)));
  const locked = pending || uncertain;
  useEffect(() => {
    dialog.current?.showModal();
    return () => {
      active.current?.abort();
      dialog.current?.close();
    };
  }, []);
  useEffect(() => {
    if (!dirty && !pending && !uncertain) return;
    const protect = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [dirty, pending, uncertain]);
  useEffect(() => {
    if (!retryAt) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [retryAt]);
  useEffect(() => {
    if (bodyView.current) bodyView.current.scrollTop = 0;
  }, [review]);
  function announce(message: string) {
    setError(message);
    requestAnimationFrame(() => announcement.current?.focus());
  }
  function close() {
    if (pending) return;
    if (uncertain) {
      announce(
        "The create result is still unknown. Retry the identical request before closing. Reloading or switching organization loses this window’s recovery request.",
      );
      return;
    }
    if (dirty) {
      setDiscard(true);
      requestAnimationFrame(() => discardFocus.current?.focus());
    } else onClose();
  }
  function changeLedger(next: Ledger) {
    setLedger(next);
    setBatch(null);
    setStatement(null);
    setPayments([]);
    setRefs([]);
    setRationale("");
    setReview(false);
    setIssues([]);
    setError("");
    attempt.current = null;
    setPendingLedger(null);
  }
  function chooseStatement(s: Statement) {
    setStatement(s);
    setRefs([
      evidenceKey(s.evidence_ref),
      evidenceKey(s.source_record_evidence_ref),
      ...payments.map((p) => evidenceKey(p.evidence_ref)),
    ]);
    setError("");
    setIssues([]);
  }
  function togglePayment(p: Payment) {
    const remove = payments.some((i) => i.id === p.id);
    setPayments((old) =>
      remove ? old.filter((i) => i.id !== p.id) : [...old, p],
    );
    setRefs((old) =>
      remove
        ? old.filter((i) => i !== evidenceKey(p.evidence_ref))
        : [...old, evidenceKey(p.evidence_ref)],
    );
    setError("");
    setIssues([]);
  }
  function inspect(event: FormEvent) {
    event.preventDefault();
    if (locked || !ledger || !statement) return;
    try {
      createBody(ledger, statement, payments, evidence, rationale);
      setReview(true);
      setError("");
      setIssues([]);
      requestAnimationFrame(() => title.current?.focus());
    } catch (e) {
      announce(e instanceof Error ? e.message : "Review your selections.");
    }
  }
  async function save() {
    if (
      inFlight.current ||
      pending ||
      !review ||
      !ledger ||
      !statement ||
      Date.now() < retryAt
    )
      return;
    if (!attempt.current) {
      try {
        attempt.current = {
          key: crypto.randomUUID(),
          body: createBody(ledger, statement, payments, evidence, rationale),
        };
      } catch (e) {
        announce(e instanceof Error ? e.message : "Review your selections.");
        return;
      }
    }
    inFlight.current = true;
    setPending(true);
    setError("");
    setIssues([]);
    const controller = new AbortController();
    active.current = controller;
    try {
      const r = await api.POST("/api/v1/proposals", {
        signal: controller.signal,
        headers: {
          "Idempotency-Key": attempt.current.key,
          "X-CSRF-Token": csrfToken,
        },
        body: attempt.current.body,
      });
      if (controller.signal.aborted) return;
      if (r.data) {
        attempt.current = null;
        onCreated(r.data);
        return;
      }
      const code =
        r.error && typeof r.error === "object" && "code" in r.error
          ? String(r.error.code)
          : "";
      const unknown =
        r.response.status >= 500 ||
        r.response.status === 409 ||
        code === "IDEMPOTENCY_KEY_REUSED";
      setUncertain(unknown);
      if (unknown) {
        const retry = r.response.headers.get("Retry-After");
        const seconds = retry && /^\d{1,6}$/.test(retry) ? Number(retry) : 0;
        setRetryAt(Date.now() + seconds * 1000);
        announce(
          code === "IDEMPOTENCY_KEY_REUSED"
            ? "This request key was refused. Keep the recovery request; do not create another draft to work around it. An administrator must inspect the request."
            : createFailure(r.response.status),
        );
      } else {
        attempt.current = null;
        setRetryAt(0);
        setReview(false);
        setIssues(fieldIssues(r.error));
        announce(createFailure(r.response.status));
      }
    } catch {
      if (!controller.signal.aborted) {
        setUncertain(true);
        announce(
          "The create result is unknown. Your selections are locked. Retry the identical request with its original key; it can recover a draft already created without creating another.",
        );
      }
    } finally {
      inFlight.current = false;
      if (!controller.signal.aborted) setPending(false);
    }
  }
  return (
    <dialog
      ref={dialog}
      className="draft-editor-dialog draft-creator-dialog"
      aria-labelledby="create-title"
      onCancel={(e) => {
        e.preventDefault();
        close();
      }}
    >
      <form onSubmit={inspect}>
        <header className="editor-header">
          <div>
            <div className="eyebrow">
              {review ? "REVIEW YOUR SOURCES" : "SOURCE-DRIVEN DRAFT"}
            </div>
            <h2 ref={title} tabIndex={-1} id="create-title">
              {review ? "Review settlement draft" : "New settlement draft"}
            </h2>
            <p>
              {review
                ? "Confirm the records and stated amounts before saving."
                : "Choose real source records. Keep every decision traceable."}
            </p>
          </div>
          <Button
            type="button"
            variant="ghost"
            aria-label="Close draft creator"
            disabled={pending}
            onClick={close}
          >
            <X size={20} />
          </Button>
        </header>
        <div className="editor-body" ref={bodyView}>
          <div className="notice">
            <LockKeyhole size={18} />
            <span>
              SANDBOX — not accounting records. A draft has no financial effect.
              Accounting remains UNREVIEWED.
            </span>
          </div>
          {error && (
            <div
              className="editor-error"
              role="alert"
              tabIndex={-1}
              ref={announcement}
            >
              <AlertCircle size={18} />
              <div>
                <p>{error}</p>
                {issues.length > 0 && (
                  <ul>
                    {issues.map((i, n) => (
                      <li key={n}>
                        <strong>{issueLabel(i.pointer)}</strong> ·{" "}
                        {issueText(i.code)}
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          )}
          <fieldset
            disabled={locked}
            className="creator-fields"
            hidden={review}
          >
            <LedgerPicker
              value={ledger?.id ?? ""}
              onChange={(next) => {
                if (next.id === ledger?.id) return;
                if (dirty) setPendingLedger(next);
                else changeLedger(next);
              }}
            />
            {pendingLedger && (
              <div className="editor-discard" role="alert">
                <p>
                  Changing ledger clears this statement, payment selection,
                  evidence and rationale.
                </p>
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => setPendingLedger(null)}
                >
                  Keep current ledger
                </Button>
                <Button
                  type="button"
                  onClick={() => changeLedger(pendingLedger)}
                >
                  Clear selections and change ledger
                </Button>
              </div>
            )}
            {ledger && (
              <>
                <StatementPicker
                  key={`statements-${ledger.id}`}
                  ledgerId={ledger.id}
                  batch={batch}
                  statement={statement}
                  onBatch={(b) => {
                    setBatch(b);
                    setStatement(null);
                    setRefs(payments.map((p) => evidenceKey(p.evidence_ref)));
                  }}
                  onSelect={chooseStatement}
                />
                <PaymentPicker
                  key={`payments-${ledger.id}`}
                  ledgerId={ledger.id}
                  selected={payments}
                  onToggle={togglePayment}
                />
                <section className="source-section">
                  <div className="source-section-title">
                    <span>03</span>
                    <div>
                      <h3>Keep the evidence trail</h3>
                      <p>
                        Source selections add their references below. Uncheck
                        evidence you do not want to cite. Only kind and id are
                        sent.
                      </p>
                    </div>
                  </div>
                  {!options.length && (
                    <p className="source-empty">
                      Choose a statement or payment to see its evidence.
                    </p>
                  )}
                  {options.map((e) => (
                    <label
                      className="source-option source-evidence"
                      key={evidenceKey(e)}
                    >
                      <input
                        type="checkbox"
                        checked={refs.includes(evidenceKey(e))}
                        onChange={() =>
                          setRefs((old) =>
                            old.includes(evidenceKey(e))
                              ? old.filter((k) => k !== evidenceKey(e))
                              : [...old, evidenceKey(e)],
                          )
                        }
                      />
                      <span className="source-option-content">
                        <strong>{e.kind.replaceAll("_", " ")}</strong>
                        <code>{e.id}</code>
                        {e.provenance && <span>Provenance {e.provenance}</span>}
                        {e.sha256 && (
                          <span>
                            SHA-256 <code>{e.sha256}</code>
                          </span>
                        )}
                      </span>
                    </label>
                  ))}
                </section>
                <section className="source-section">
                  <label htmlFor="create-rationale">
                    Rationale <span className="muted">· optional</span>
                  </label>
                  <textarea
                    id="create-rationale"
                    rows={3}
                    maxLength={4000}
                    value={rationale}
                    placeholder="Explain the proposed settlement and any concerns to review."
                    onChange={(e) => setRationale(e.target.value)}
                  />
                  <p className="editor-count">{rationale.length} / 4,000</p>
                </section>
              </>
            )}
          </fieldset>
          {review && ledger && statement && (
            <section className="creator-review">
              <div className="source-review-heading">
                <FilePlus2 size={24} />
                <div>
                  <h3>{statement.payout_id}</h3>
                  <p>
                    {ledger.name} · {statement.processor_ref} ·{" "}
                    {statement.payout_date}
                  </p>
                </div>
                <span className="outline-tag">DRAFT</span>
              </div>
              <div className="creator-totals">
                {(
                  [
                    ["Gross", statement.gross],
                    ["Fee", statement.fee],
                    ["Net", statement.net],
                  ] as const
                ).map(([label, m]) => (
                  <div key={label}>
                    <span>{label} · as stated</span>
                    <strong>
                      {m.currency} {formatAmount(m.amount, m.currency)}
                    </strong>
                  </div>
                ))}
              </div>
              <p className="source-review-note">
                Amounts come from the selected statement. These records have not
                been matched, reconciled or simulated. Saving does not establish
                eligibility.
              </p>
              <h4>
                {payments.length} payment{payments.length === 1 ? "" : "s"}{" "}
                selected
              </h4>
              <ul className="source-review-payments">
                {payments.map((p) => (
                  <li key={p.id}>
                    <span>
                      <strong>{p.processor_item_id ?? p.source_ref}</strong>
                      <small>
                        {p.party?.name} · Version {p.version} · {p.status}
                      </small>
                    </span>
                    <span>
                      {p.amount?.currency}{" "}
                      {p.amount &&
                        formatAmount(p.amount.amount, p.amount.currency)}
                    </span>
                  </li>
                ))}
              </ul>
              {payments.some((p) => p.status === "SETTLED") && (
                <p className="source-warning">
                  A selected payment is already SETTLED. The current draft API
                  accepts this; eligibility must be resolved before submission
                  is implemented.
                </p>
              )}
              <h4>
                {evidence.length} evidence reference
                {evidence.length === 1 ? "" : "s"}
              </h4>
              <ul className="source-review-evidence">
                {evidence.map((e) => (
                  <li key={evidenceKey(e)}>
                    {e.kind}
                    <code>{e.id}</code>
                  </li>
                ))}
              </ul>
              <h4>Rationale</h4>
              <blockquote>{rationale || "No rationale provided."}</blockquote>
              <p className="muted small">
                Your session supplies the maker identity. Saving creates
                revision 1, and later edits remain in immutable revision
                history.
              </p>
            </section>
          )}
          {discard && (
            <div className="editor-discard" tabIndex={-1} ref={discardFocus}>
              <p>Discard these unsaved selections?</p>
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  setDiscard(false);
                  requestAnimationFrame(() => title.current?.focus());
                }}
              >
                Keep selecting
              </Button>
              <Button type="button" onClick={onClose}>
                Discard and close
              </Button>
            </div>
          )}
        </div>
        <footer className="editor-footer">
          <span>
            {review
              ? "Draft only · no ledger changes"
              : `${payments.length} payments · ${evidence.length} evidence`}
          </span>
          <div>
            {review ? (
              <Button
                type="button"
                variant="secondary"
                disabled={locked}
                onClick={() => {
                  setReview(false);
                  requestAnimationFrame(() => title.current?.focus());
                }}
              >
                <ArrowLeft size={15} />
                Back to sources
              </Button>
            ) : (
              <Button
                type="button"
                variant="secondary"
                disabled={pending}
                onClick={close}
              >
                Cancel
              </Button>
            )}
            {review ? (
              <Button
                type="button"
                disabled={pending || (!!retryAt && now < retryAt)}
                onClick={() => void save()}
              >
                {pending
                  ? "Creating draft…"
                  : uncertain
                    ? now < retryAt
                      ? "Waiting for retry window…"
                      : "Retry identical create request"
                    : "Create draft"}
              </Button>
            ) : (
              <Button
                type="submit"
                disabled={!statement || !payments.length || !!pendingLedger}
              >
                Review draft
                <ArrowRight size={15} />
              </Button>
            )}
          </div>
        </footer>
      </form>
    </dialog>
  );
}

"use client";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ArrowRight, Check, Link2, Trash2 } from "lucide-react";
import type { components } from "@/lib/api.generated";
import type {
  DraftContent,
  EvidenceIdentity,
  Operation,
  Statement,
} from "@/lib/draft-references";
import { replaceReferences } from "@/lib/draft-references";
import { evidenceKey, type Payment } from "@/lib/draft-create";
import { formatAmount } from "@/lib/money-format";
import { StatementPicker, PaymentPicker } from "./draft-sources";
import { Button } from "./button";
type Evidence = components["schemas"]["EvidenceRef"];
type Batch = components["schemas"]["ImportBatch"];
type EvidenceOption = EvidenceIdentity &
  Partial<Pick<Evidence, "provenance" | "sha256">>;
export function DraftReferenceEditor({
  content,
  index,
  ledgerId,
  metadata,
  cancelSignal,
  onDirtyChange,
  onApply,
  onCancel,
}: {
  content: DraftContent;
  index: number;
  ledgerId: string;
  metadata: Evidence[];
  cancelSignal: number;
  onDirtyChange: (dirty: boolean) => void;
  onApply: (content: DraftContent) => void;
  onCancel: () => void;
}) {
  const original = content.operations[index];
  const [items, setItems] = useState<Operation["items"]>(() =>
    structuredClone(original.items),
  );
  const [batch, setBatch] = useState<Batch | null>(null),
    [statement, setStatement] = useState<Statement | null>(null),
    [adopt, setAdopt] = useState(false);
  const [seenPayments, setSeenPayments] = useState<Payment[]>([]),
    [extraEvidence, setExtraEvidence] = useState<Evidence[]>([]);
  const [selected, setSelected] = useState(() =>
    content.evidence_refs.map(evidenceKey),
  );
  const [review, setReview] = useState<DraftContent | null>(null),
    [error, setError] = useState(""),
    [discard, setDiscard] = useState(false);
  const lastCancel = useRef(cancelSignal),
    errorFocus = useRef<HTMLDivElement>(null),
    discardFocus = useRef<HTMLDivElement>(null),
    body = useRef<HTMLDivElement>(null),
    heading = useRef<HTMLHeadingElement>(null);
  const pool = [
    ...new Map<string, EvidenceOption>(
      [...content.evidence_refs, ...metadata, ...extraEvidence].map(
        (e) => [evidenceKey(e), e] as const,
      ),
    ).values(),
  ];
  const refs: EvidenceIdentity[] = pool
    .filter((e) => selected.includes(evidenceKey(e)))
    .map(({ kind, id }) => ({ kind, id }));
  const dirty =
    JSON.stringify(items) !== JSON.stringify(original.items) ||
    JSON.stringify(refs) !== JSON.stringify(content.evidence_refs) ||
    !!batch ||
    !!(statement && adopt);
  useEffect(() => onDirtyChange(dirty), [dirty, onDirtyChange]);
  useEffect(() => {
    if (lastCancel.current === cancelSignal) return;
    lastCancel.current = cancelSignal;
    if (dirty) {
      setDiscard(true);
      requestAnimationFrame(() => discardFocus.current?.focus());
    } else onCancel();
  }, [cancelSignal, dirty, onCancel]);
  useEffect(() => {
    if (body.current) body.current.scrollTop = 0;
    heading.current?.focus();
  }, [review]);
  function cancel() {
    if (dirty) {
      setDiscard(true);
      requestAnimationFrame(() => discardFocus.current?.focus());
    } else onCancel();
  }
  function chooseStatement(s: Statement) {
    setStatement(s);
    setExtraEvidence((old) => [
      ...old,
      s.evidence_ref,
      s.source_record_evidence_ref,
    ]);
    setError("");
  }
  function togglePayment(p: Payment) {
    if (
      p.ledger_id !== ledgerId ||
      !p.amount ||
      p.amount.currency !== original.gross.currency
    ) {
      setError("The payment does not match this ledger and currency.");
      return;
    }
    setSeenPayments((old) => [...old.filter((x) => x.id !== p.id), p]);
    setExtraEvidence((old) => [...old, p.evidence_ref]);
    setItems((old) =>
      old.some((i) => i.document_id === p.id)
        ? old.filter((i) => i.document_id !== p.id)
        : [
            ...old,
            { document_id: p.id, kind: "PAYMENT", amount: { ...p.amount! } },
          ],
    );
    setError("");
  }
  function inspect() {
    if (batch && !statement) return;
    try {
      setReview(
        replaceReferences(
          content,
          index,
          ledgerId,
          items,
          refs,
          statement,
          adopt,
        ),
      );
      setError("");
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Review these reference selections.",
      );
      requestAnimationFrame(() => errorFocus.current?.focus());
    }
  }
  const next = review?.operations[index];
  return (
    <>
      <div className="editor-body reference-picker" ref={body}>
        <h3 className="reference-step-heading" ref={heading} tabIndex={-1}>
          {review
            ? "Review local reference changes"
            : `Operation ${index + 1} · source references`}
        </h3>
        <div className="notice">
          <Link2 size={18} />
          <span>
            These selections stay local until you save a draft revision. No
            simulation, matching or financial action is performed.
          </span>
        </div>
        {error && (
          <div
            className="editor-error"
            role="alert"
            ref={errorFocus}
            tabIndex={-1}
          >
            {error}
          </div>
        )}
        {!review ? (
          <>
            <section className="reference-attached">
              <h4>Attached statement</h4>
              <code>{original.statement_document_id}</code>
              <p>
                The saved draft reference is retained unless you choose a
                replacement below.
              </p>
              {batch && (
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() => {
                    setBatch(null);
                    setStatement(null);
                    setAdopt(false);
                  }}
                >
                  Keep attached statement
                </Button>
              )}
            </section>
            <StatementPicker
              ledgerId={ledgerId}
              batch={batch}
              onBatch={(b) => {
                setBatch(b);
                setStatement(null);
                setAdopt(false);
              }}
              statement={statement}
              onSelect={chooseStatement}
              description="Choosing a statement changes its reference only. Replacing proposed header fields and amounts requires the explicit choice below."
            />
            {statement && (
              <label className="reference-adopt">
                <input
                  type="checkbox"
                  checked={adopt}
                  onChange={(e) => setAdopt(e.target.checked)}
                />
                <span>
                  <strong>
                    Replace proposed header and amounts with this statement’s
                    values
                  </strong>
                  <small>
                    Processor, payout reference/date, gross, fee and net are
                    replaced exactly. Unchecked keeps your current proposed
                    values.
                  </small>
                </span>
              </label>
            )}
            <section className="source-section">
              <h4>Selected payment items · {items.length}</h4>
              <p className="muted small">
                Saved amounts remain proposed inputs. Removing a payment does
                not remove its evidence citation.
              </p>
              <ul className="reference-items">
                {items.map((item, n) => {
                  const current = seenPayments.find(
                    (p) => p.id === item.document_id,
                  );
                  return (
                    <li key={item.document_id}>
                      <div>
                        <strong>
                          {current?.processor_item_id ??
                            `Attached payment ${n + 1}`}
                        </strong>
                        <code>{item.document_id}</code>
                        <small>
                          {current
                            ? `${current.status} · read version ${current.version}`
                            : "Saved draft reference · amount from this revision"}
                        </small>
                      </div>
                      <span>
                        {item.amount.currency}{" "}
                        {formatAmount(item.amount.amount, item.amount.currency)}
                      </span>
                      <Button
                        type="button"
                        variant="ghost"
                        aria-label={`Remove selected payment ${n + 1}`}
                        onClick={() =>
                          setItems((old) =>
                            old.filter(
                              (i) => i.document_id !== item.document_id,
                            ),
                          )
                        }
                      >
                        <Trash2 size={15} />
                      </Button>
                    </li>
                  );
                })}
              </ul>
              {!items.length && (
                <p className="source-empty">
                  Select at least one payment before applying references.
                </p>
              )}
            </section>
            <PaymentPicker
              ledgerId={ledgerId}
              selected={items.map((i) => ({ id: i.document_id }))}
              onToggle={togglePayment}
            />
            <section className="source-section">
              <div className="source-section-title">
                <span>03</span>
                <div>
                  <h3>Evidence across this draft</h3>
                  <p>
                    This list is shared by all operations. Choose citations
                    explicitly; browsing or removing a payment does not silently
                    change evidence.
                  </p>
                </div>
              </div>
              {pool.map((e) => (
                <label
                  className="source-option source-evidence"
                  key={evidenceKey(e)}
                >
                  <input
                    type="checkbox"
                    checked={selected.includes(evidenceKey(e))}
                    onChange={() =>
                      setSelected((old) =>
                        old.includes(evidenceKey(e))
                          ? old.filter((k) => k !== evidenceKey(e))
                          : pool
                              .map(evidenceKey)
                              .filter(
                                (k) => k === evidenceKey(e) || old.includes(k),
                              ),
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
              {!pool.length && (
                <p className="source-empty">
                  Choose a statement or payment to discover its evidence. No
                  UUID entry is required.
                </p>
              )}
              <p className="muted small">
                {refs.length} / 100 citations · Only kind and id will be sent.
              </p>
            </section>
          </>
        ) : (
          next && (
            <section className="reference-review">
              <p className="reference-review-note">
                Only operation {index + 1} and the draft’s evidence list will
                change. Rationale and other operations stay as edited.
              </p>
              <h4>Statement reference</h4>
              <dl className="reference-comparison">
                <div>
                  <dt>Before</dt>
                  <dd>
                    <code>{original.statement_document_id}</code>
                  </dd>
                </div>
                <div>
                  <dt>After</dt>
                  <dd>
                    <code>{next.statement_document_id}</code>
                  </dd>
                </div>
              </dl>
              <p className="reference-review-note">
                {statement && adopt
                  ? "The chosen statement’s exact header and amount values will replace the proposed values."
                  : "Proposed header fields and amounts are kept. Statement agreement has not been checked."}
              </p>
              <div className="reference-money-review">
                {(
                  [
                    ["Gross", "gross"],
                    ["Fee", "fee"],
                    ["Net", "net"],
                  ] as const
                ).map(([label, field]) => (
                  <div key={field}>
                    <h4>{label}</h4>
                    <span>
                      Before · {original[field].currency}{" "}
                      {formatAmount(
                        original[field].amount,
                        original[field].currency,
                      )}
                    </span>
                    <strong>
                      After · {next[field].currency}{" "}
                      {formatAmount(next[field].amount, next[field].currency)}
                    </strong>
                  </div>
                ))}
              </div>
              {statement && adopt && (
                <dl className="draft-facts">
                  <div>
                    <dt>Processor</dt>
                    <dd>{next.processor_ref}</dd>
                  </div>
                  <div>
                    <dt>Payout reference</dt>
                    <dd>{next.payout_id}</dd>
                  </div>
                  <div>
                    <dt>Payout date</dt>
                    <dd>{next.payout_date}</dd>
                  </div>
                </dl>
              )}
              <h4>Payment items · {next.items.length}</h4>
              <ul className="reference-review-list">
                {next.items.map((i) => (
                  <li key={i.document_id}>
                    <code>{i.document_id}</code>
                    <span>
                      {i.amount.currency}{" "}
                      {formatAmount(i.amount.amount, i.amount.currency)}
                    </span>
                  </li>
                ))}
              </ul>
              <h4>Evidence across the draft · {review.evidence_refs.length}</h4>
              <ul className="reference-review-list">
                {review.evidence_refs.map((e) => (
                  <li key={evidenceKey(e)}>
                    <span>{e.kind}</span>
                    <code>{e.id}</code>
                  </li>
                ))}
              </ul>
              {items.some(
                (i) =>
                  seenPayments.find((p) => p.id === i.document_id)?.status ===
                  "SETTLED",
              ) && (
                <p className="source-warning">
                  A selected payment is already SETTLED. Draft acceptance does
                  not establish settlement eligibility.
                </p>
              )}
            </section>
          )
        )}
        {discard && (
          <div
            className="editor-discard"
            role="alert"
            ref={discardFocus}
            tabIndex={-1}
          >
            <strong>Discard this reference selection?</strong>
            <p>
              Your earlier draft edits remain in the editor. These pending
              selections will be lost.
            </p>
            <div>
              <Button
                type="button"
                variant="secondary"
                onClick={() => {
                  setDiscard(false);
                  heading.current?.focus();
                }}
              >
                Keep selecting
              </Button>
              <Button type="button" onClick={onCancel}>
                Discard selection
              </Button>
            </div>
          </div>
        )}
      </div>
      <footer className="editor-footer">
        <span>Local selection · no server save</span>
        <div>
          {review ? (
            <Button
              type="button"
              variant="secondary"
              onClick={() => setReview(null)}
              disabled={discard}
            >
              <ArrowLeft size={15} />
              Back to selection
            </Button>
          ) : (
            <Button type="button" variant="secondary" onClick={cancel}>
              Cancel selection
            </Button>
          )}
          {review ? (
            <Button
              type="button"
              disabled={discard}
              onClick={() => onApply(review)}
            >
              <Check size={16} />
              Apply to editor
            </Button>
          ) : (
            <Button
              type="button"
              onClick={inspect}
              disabled={
                !items.length ||
                items.length > 1000 ||
                refs.length > 100 ||
                !!(batch && !statement) ||
                discard
              }
            >
              Review references
              <ArrowRight size={15} />
            </Button>
          )}
        </div>
      </footer>
    </>
  );
}

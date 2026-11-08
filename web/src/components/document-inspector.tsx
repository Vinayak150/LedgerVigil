"use client";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, FileText, History, ShieldCheck, X } from "lucide-react";
import { api } from "@/lib/api";
import type { components } from "@/lib/api.generated";
import { sourceError } from "@/lib/use-source-page";
import { Button } from "./button";
import {
  MoneyValue,
  RecordStatus,
  ReadFailure,
  ReferenceFacts,
  Fact,
} from "./record-ui";
type Document = components["schemas"]["Document"];
type Money = components["schemas"]["Money"];
export function DocumentInspector({
  id,
  ledgerId,
  onClose,
  proposedAmount,
}: {
  id: string;
  ledgerId: string;
  onClose: () => void;
  proposedAmount?: Money;
}) {
  const [trail, setTrail] = useState([id]),
    [refresh, setRefresh] = useState(0);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const target =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const modal = dialog.current;
    modal?.showModal();
    return () => {
      modal?.close();
      requestAnimationFrame(() => {
        if (target?.isConnected) target.focus();
      });
    };
  }, []);
  const current = trail[trail.length - 1];
  return (
    <dialog
      className="draft-editor-dialog record-inspector"
      aria-labelledby="record-title"
      ref={dialog}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <header className="editor-header">
        <div>
          <div className="eyebrow">READ-ONLY DOCUMENT</div>
          <h2 id="record-title" tabIndex={-1}>
            Record inspection
          </h2>
          <p>Current version · no financial changes</p>
        </div>
        <Button
          type="button"
          variant="ghost"
          aria-label="Close record inspection"
          onClick={onClose}
        >
          <X size={20} />
        </Button>
      </header>
      <div className="record-inspector-body" key={`${current}:${refresh}`}>
        {trail.length > 1 && (
          <Button
            type="button"
            variant="ghost"
            onClick={() => setTrail((old) => old.slice(0, -1))}
          >
            <ArrowLeft size={15} />
            Back to previous record
          </Button>
        )}
        <DocumentRead
          id={current}
          ledgerId={ledgerId}
          proposedAmount={trail.length === 1 ? proposedAmount : undefined}
          retry={() => setRefresh((n) => n + 1)}
          openLinked={(next) => {
            setTrail((old) => [...old, next]);
            setRefresh(0);
          }}
        />
      </div>
      <footer className="editor-footer">
        <span>Accounting UNREVIEWED</span>
        <Button
          type="button"
          variant="secondary"
          onClick={() => setRefresh((n) => n + 1)}
        >
          Refresh current record
        </Button>
      </footer>
    </dialog>
  );
}
function DocumentRead({
  id,
  ledgerId,
  retry,
  openLinked,
  proposedAmount,
}: {
  id: string;
  ledgerId: string;
  retry: () => void;
  openLinked: (id: string) => void;
  proposedAmount?: Money;
}) {
  const [data, setData] = useState<Document | null>(null),
    [error, setError] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    const c = new AbortController();
    (async () => {
      try {
        const r = await api.GET("/api/v1/documents/{document_id}", {
          signal: c.signal,
          params: { path: { document_id: id } },
        });
        if (c.signal.aborted) return;
        if (!r.data) throw sourceError(r.response.status);
        if (r.data.id !== id || r.data.ledger_id !== ledgerId)
          throw new Error(
            "The response did not match this ledger and record. No record is shown.",
          );
        setData(r.data);
        requestAnimationFrame(() => heading.current?.focus());
      } catch (e) {
        if (!c.signal.aborted)
          setError(
            e instanceof Error ? e.message : "The record could not be loaded.",
          );
      }
    })();
    return () => c.abort();
  }, [id, ledgerId]);
  if (error) return <ReadFailure message={error} retry={retry} />;
  if (!data)
    return (
      <p className="record-loading" role="status">
        Loading current record…
      </p>
    );
  return (
    <>
      <div className="record-detail-heading">
        <FileText size={25} />
        <div>
          <h3 ref={heading} tabIndex={-1}>
            {data.processor_item_id ?? data.payout_id ?? data.source_ref}
          </h3>
          <p>
            {data.type.replaceAll("_", " ")} · Version {data.version} ·{" "}
            {data.date ?? "Date unavailable"}
          </p>
        </div>
        <RecordStatus value={data.status} />
      </div>
      <div className="notice record-notice">
        <ShieldCheck size={18} />
        <span>
          {data.data_label ?? data.purpose}. Accounting {data.accounting_status}
          . This is the current record; historical document versions are not
          available here.
        </span>
      </div>
      {proposedAmount && (
        <div className="record-proposed">
          <span>Amount proposed in the displayed draft revision</span>
          <strong>
            <MoneyValue value={proposedAmount} />
          </strong>
          <p>
            The current record below is a separate read. Its version is not
            assumed to be the version used by that draft.
          </p>
        </div>
      )}
      <dl className="record-facts">
        <Fact label="Current amount">
          <MoneyValue value={data.amount} />
        </Fact>
        <Fact label="Party">{data.party?.name ?? "Not applicable"}</Fact>
        <Fact label="Processor">{data.processor_ref ?? "Not applicable"}</Fact>
        <Fact label="Source reference">{data.source_ref}</Fact>
        <Fact label="Provenance">{data.provenance}</Fact>
        <Fact label="Commit origin">{data.origin}</Fact>
        <Fact label="Created at sequence">{data.created_seq}</Fact>
        <Fact label="Current version sequence">{data.version_seq}</Fact>
      </dl>
      {data.payment && (
        <section className="record-detail-section">
          <h4>Payment position</h4>
          <dl className="record-facts">
            <Fact label="Applied">
              <MoneyValue value={data.payment.applied} />
            </Fact>
            <Fact label="Unapplied">
              <MoneyValue value={data.payment.unapplied} />
            </Fact>
          </dl>
          <p className="muted small">
            Applications are not implemented in this release. Open amount does
            not establish settlement eligibility.
          </p>
          {data.payment.settlement_document_id && (
            <Button
              type="button"
              variant="secondary"
              onClick={() => openLinked(data.payment!.settlement_document_id!)}
            >
              Inspect linked settlement
            </Button>
          )}
        </section>
      )}
      {data.settlement && (
        <section className="record-detail-section">
          <h4>Recorded settlement</h4>
          <dl className="record-facts">
            <Fact label="Gross">
              <MoneyValue value={data.settlement.gross} />
            </Fact>
            <Fact label="Fee">
              <MoneyValue value={data.settlement.fee} />
            </Fact>
            <Fact label="Net">
              <MoneyValue value={data.settlement.net} />
            </Fact>
          </dl>
          <ul className="record-linked-items">
            {data.settlement.items.map((item) => (
              <li key={item.line_no}>
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() => openLinked(item.document_id)}
                >
                  Inspect payment {item.line_no}
                </Button>
                <span>
                  <MoneyValue value={item.amount} /> · Included version{" "}
                  {item.payment_version}
                </span>
                <code>{item.document_id}</code>
              </li>
            ))}
          </ul>
        </section>
      )}
      <section className="record-detail-section">
        <h4>
          <History size={16} /> Posting references
        </h4>
        {data.postings.length ? (
          <ul className="record-postings">
            {data.postings.map((p) => (
              <li key={p.entry_id}>
                <strong>
                  Entry {p.entry_no} · Sequence {p.seq}
                </strong>
                <span>
                  {p.kind} · {p.effective_date}
                </span>
                <span>{p.posting_rule ?? "No posting rule reference"}</span>
                <code>{p.entry_id}</code>
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted small">
            No journal-entry references returned for this document.
          </p>
        )}
        <p className="muted small">
          These are entry references. Full journal-line inspection is not
          available.
        </p>
      </section>
      {(data.description_untrusted || data.reason) && (
        <section className="record-detail-section">
          <h4>Recorded context</h4>
          <p className="record-text">{data.reason}</p>
          {data.description_untrusted && (
            <>
              <span className="record-source-label">
                Source description · untrusted text
              </span>
              <p className="record-text">{data.description_untrusted}</p>
            </>
          )}
        </section>
      )}
      <section className="record-detail-section">
        <h4>Current-version evidence</h4>
        <ReferenceFacts
          id={data.current_version_id}
          kind={data.evidence_ref.kind}
          hash={data.financial_fingerprint}
        />
        <p className="muted small">
          Hash is the current financial fingerprint. It is not a source-file
          hash or an accounting verdict.
        </p>
        <details className="record-technical">
          <summary>Document identifiers</summary>
          <ReferenceFacts id={data.id} />
          <p>Ledger</p>
          <code>{data.ledger_id}</code>
        </details>
      </section>
    </>
  );
}

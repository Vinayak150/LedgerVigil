"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { FileInput, ShieldCheck, X } from "lucide-react";
import { api } from "@/lib/api";
import type { components } from "@/lib/api.generated";
import { sourceError, useSourcePage } from "@/lib/use-source-page";
import { Button } from "./button";
import { checkSourceText, type TextIntegrity } from "@/lib/source-integrity";
import {
  MoneyValue,
  RecordStatus,
  ReadFailure,
  RecordEmpty,
  PageActions,
  ReferenceFacts,
  Fact,
} from "./record-ui";
type Batch = components["schemas"]["ImportBatch"];
export function ImportInspector({
  id,
  ledgerId,
  onClose,
}: {
  id: string;
  ledgerId: string;
  onClose: () => void;
}) {
  const [refresh, setRefresh] = useState(0);
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
  return (
    <dialog
      className="draft-editor-dialog record-inspector"
      aria-labelledby="import-title"
      ref={dialog}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <header className="editor-header">
        <div>
          <div className="eyebrow">SOURCE PROVENANCE</div>
          <h2 id="import-title" tabIndex={-1}>
            Import inspection
          </h2>
          <p>Stored outcome · read-only source evidence</p>
        </div>
        <Button
          type="button"
          variant="ghost"
          aria-label="Close import inspection"
          onClick={onClose}
        >
          <X size={20} />
        </Button>
      </header>
      <div className="record-inspector-body" key={refresh}>
        <ImportRead
          id={id}
          ledgerId={ledgerId}
          retry={() => setRefresh((n) => n + 1)}
        />
      </div>
      <footer className="editor-footer">
        <span>Ledger effect NONE · no upload actions</span>
        <Button
          type="button"
          variant="secondary"
          onClick={() => setRefresh((n) => n + 1)}
        >
          Refresh import
        </Button>
      </footer>
    </dialog>
  );
}
function ImportRead({
  id,
  ledgerId,
  retry,
}: {
  id: string;
  ledgerId: string;
  retry: () => void;
}) {
  const [data, setData] = useState<Batch | null>(null),
    [error, setError] = useState(""),
    [view, setView] = useState<"overview" | "statements" | "records">(
      "overview",
    );
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    const c = new AbortController();
    (async () => {
      try {
        const r = await api.GET("/api/v1/imports/{import_id}", {
          signal: c.signal,
          params: { path: { import_id: id } },
        });
        if (c.signal.aborted) return;
        if (!r.data) throw sourceError(r.response.status);
        if (r.data.id !== id || r.data.ledger_id !== ledgerId)
          throw new Error(
            "The response did not match this ledger and import. No source is shown.",
          );
        setData(r.data);
        requestAnimationFrame(() => heading.current?.focus());
      } catch (e) {
        if (!c.signal.aborted)
          setError(
            e instanceof Error ? e.message : "The import could not be loaded.",
          );
      }
    })();
    return () => c.abort();
  }, [id, ledgerId]);
  if (error) return <ReadFailure message={error} retry={retry} />;
  if (!data)
    return (
      <p className="record-loading" role="status">
        Loading import provenance…
      </p>
    );
  return (
    <>
      <div className="record-detail-heading">
        <FileInput size={25} />
        <div>
          <h3 ref={heading} tabIndex={-1}>
            Import · {data.created_at.slice(0, 10)}
          </h3>
          <p>
            {data.parser} · Parser version {data.parser_version}
          </p>
        </div>
        <RecordStatus value={data.status} />
      </div>
      <div className="notice record-notice">
        <ShieldCheck size={18} />
        <span>
          {data.data_label ?? data.purpose}. This stored import outcome is not
          reconciliation or accounting approval. Ledger effect{" "}
          {data.ledger_effect}.
        </span>
      </div>
      <div className="record-view-switch" aria-label="Import views">
        {(
          [
            ["overview", "Provenance"],
            ["statements", "Staged statements"],
            ["records", "Source records"],
          ] as const
        ).map(([key, label]) => (
          <Button
            type="button"
            variant={view === key ? "primary" : "ghost"}
            aria-pressed={view === key}
            key={key}
            onClick={() => setView(key)}
          >
            {label}
          </Button>
        ))}
      </div>
      {view === "overview" ? (
        <>
          <dl className="record-facts">
            <Fact label="Source format">
              {data.source_file.declared_format} ·{" "}
              {data.source_file.declared_version}
            </Fact>
            <Fact label="Media type">{data.source_file.media_type}</Fact>
            <Fact label="Bytes received">
              {data.source_file.byte_size.toLocaleString("en-GB")}
            </Fact>
            <Fact label="Content stored">
              {data.source_file.content_stored ? "Yes" : "Hash only"}
            </Fact>
            <Fact label="Classification">
              {data.source_file.data_classification}
            </Fact>
            <Fact label="Received at">{data.source_file.received_at}</Fact>
            <Fact label="Batch-derived valid records">
              {data.record_counts.VALID}
            </Fact>
            <Fact label="Batch-derived invalid records">
              {data.record_counts.INVALID}
            </Fact>
            <Fact label="Staged documents">{data.staged_document_count}</Fact>
            <Fact label="Ledger effect">{data.ledger_effect}</Fact>
          </dl>
          <section className="record-detail-section">
            <h4>Received-file provenance</h4>
            <ReferenceFacts
              id={data.source_file.id}
              hash={data.source_file.sha256}
            />
            <p className="muted small">
              SHA-256 identifies the received source bytes. It does not certify
              their contents.
            </p>
          </section>
          {data.failure_code && (
            <section className="record-detail-section">
              <h4>Stored refusal</h4>
              <code>{data.failure_code}</code>
              <p className="record-text">
                {data.failure_detail ?? "No detail supplied."}
              </p>
            </section>
          )}
          {data.related_staged_document_id && (
            <section className="record-detail-section">
              <h4>Related staged statement</h4>
              <ReferenceFacts id={data.related_staged_document_id} />
              <p className="muted small">
                Related by the stored duplicate or conflict result. This is not
                an automatic match.
              </p>
            </section>
          )}
          <details className="record-technical">
            <summary>Import identifiers</summary>
            <ReferenceFacts id={data.id} />
            <p>Ledger</p>
            <code>{data.ledger_id}</code>
          </details>
        </>
      ) : view === "statements" ? (
        <StagedStatements key={id} id={id} ledgerId={ledgerId} />
      ) : (
        <SourceRecords key={id} id={id} ledgerId={ledgerId} />
      )}
    </>
  );
}
function StagedStatements({ id, ledgerId }: { id: string; ledgerId: string }) {
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await api.GET("/api/v1/imports/{import_id}/staged-documents", {
        signal,
        params: {
          path: { import_id: id },
          query: { limit: "10", ...(cursor ? { cursor } : {}) },
        },
      });
      if (!r.data) throw sourceError(r.response.status);
      if (
        r.data.import_batch_id !== id ||
        r.data.items.some(
          (i) => i.ledger_id !== ledgerId || i.import_batch_id !== id,
        )
      )
        throw new Error(
          "Source scope could not be verified. No statements are shown.",
        );
      return r.data;
    },
    [id, ledgerId],
  );
  const page = useSourcePage(loader);
  if (page.error)
    return <ReadFailure message={page.error} retry={page.reload} />;
  return (
    <section className="record-detail-section">
      <p className="muted small">
        As stated by the source. Amounts and item references have not been
        matched to ledger records.
      </p>
      {page.items.map((s) => (
        <article className="record-statement" key={s.id}>
          <div className="record-detail-heading">
            <div>
              <h4>{s.payout_id}</h4>
              <p>
                {s.processor_ref} · {s.payout_date} · {s.provenance}
              </p>
            </div>
            <span>
              Validation <RecordStatus value={s.validation_status} />
            </span>
          </div>
          <dl className="record-facts">
            <Fact label="Workflow status">
              {s.status} · version {s.version}
            </Fact>
            {s.reserved_by_proposal_id && (
              <Fact label="Reserved by proposal">
                <code>{s.reserved_by_proposal_id}</code>
              </Fact>
            )}
            <Fact label="Stated gross">
              <MoneyValue value={s.gross} />
            </Fact>
            <Fact label="Stated fee">
              <MoneyValue value={s.fee} />
            </Fact>
            <Fact label="Stated net">
              <MoneyValue value={s.net} />
            </Fact>
            <Fact label="Duplicate / conflicting batches">
              {s.duplicate_batch_count} / {s.conflicting_batch_count}
            </Fact>
          </dl>
          <details className="record-technical">
            <summary>Statement items · {s.item_count}</summary>
            <ul className="record-statement-items">
              {s.items.map((i) => (
                <li key={i.line_no}>
                  <strong>{i.item_ref}</strong>
                  <RecordStatus value={i.kind} />
                  <MoneyValue value={i.amount} />
                </li>
              ))}
            </ul>
          </details>
          {s.description_untrusted && (
            <>
              <span className="record-source-label">
                Source description · untrusted text
              </span>
              <p className="record-text">{s.description_untrusted}</p>
            </>
          )}
          <details className="record-technical">
            <summary>Evidence identities and hashes</summary>
            <ReferenceFacts id={s.evidence_ref.id} kind={s.evidence_ref.kind} />
            <ReferenceFacts
              id={s.source_record_evidence_ref.id}
              kind={s.source_record_evidence_ref.kind}
              hash={s.source_record_evidence_ref.sha256}
            />
            <p>Statement financial fingerprint</p>
            <code>{s.financial_fingerprint}</code>
          </details>
        </article>
      ))}
      {!page.loading && !page.items.length && (
        <RecordEmpty>
          No staged statements returned for this import. Rejected and duplicate
          batches may have none.
        </RecordEmpty>
      )}
      <PageActions page={page} label="statements" />
    </section>
  );
}
function SourceRecords({ id, ledgerId }: { id: string; ledgerId: string }) {
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await api.GET("/api/v1/imports/{import_id}/records", {
        signal,
        params: {
          path: { import_id: id },
          query: { limit: "10", ...(cursor ? { cursor } : {}) },
        },
      });
      if (!r.data) throw sourceError(r.response.status);
      if (
        r.data.import_batch_id !== id ||
        r.data.items.some(
          (i) => i.ledger_id !== ledgerId || i.import_batch_id !== id,
        )
      )
        throw new Error(
          "Source scope could not be verified. No source text is shown.",
        );
      return r.data;
    },
    [id, ledgerId],
  );
  const page = useSourcePage(loader);
  if (page.error)
    return <ReadFailure message={page.error} retry={page.reload} />;
  return (
    <section className="record-detail-section">
      <p className="record-source-label">
        VALID / INVALID is derived from the batch outcome. It does not establish
        per-record reconciliation or accounting correctness.
      </p>
      {page.items.map((r) => (
        <article className="record-statement" key={r.id}>
          <div className="record-detail-heading">
            <div>
              <h4>Record {r.record_index}</h4>
              <p>
                {r.source_system} · {r.source_key}
              </p>
            </div>
            <RecordStatus value={r.validation_status} />
          </div>
          <ReferenceFacts
            id={r.evidence_ref.id}
            kind={r.evidence_ref.kind}
            hash={r.evidence_ref.sha256}
          />
          <SourceTextIntegrity
            text={r.content}
            sha256={r.evidence_ref.sha256}
            byteLength={r.byte_length}
          />
          {r.messages.length > 0 && (
            <div className="record-text">
              <h4>Stored messages</h4>
              <pre>{JSON.stringify(r.messages, null, 2)}</pre>
            </div>
          )}
          <details className="record-technical">
            <summary>
              Read source text · {r.byte_length.toLocaleString("en-GB")} bytes ·
              untrusted
            </summary>
            <pre
              className="record-raw-text"
              tabIndex={0}
              aria-label={`Untrusted source text for record ${r.record_index}`}
            >
              {r.content}
            </pre>
          </details>
        </article>
      ))}
      {!page.loading && !page.items.length && (
        <RecordEmpty>
          No source records returned for this import. Duplicate batches may
          store no records.
        </RecordEmpty>
      )}
      <PageActions page={page} label="source records" />
    </section>
  );
}

function SourceTextIntegrity({
  text,
  sha256,
  byteLength,
}: {
  text: string;
  sha256: string | null | undefined;
  byteLength: number;
}) {
  const [check, setCheck] = useState<{
    text: string;
    sha256: string | null | undefined;
    byteLength: number;
    result: TextIntegrity;
  } | null>(null);
  const state =
    check?.text === text &&
    check.sha256 === sha256 &&
    check.byteLength === byteLength
      ? check.result
      : "CHECKING";
  useEffect(() => {
    let active = true;
    void checkSourceText(text, sha256, byteLength).then((result) => {
      if (active) setCheck({ text, sha256, byteLength, result });
    });
    return () => {
      active = false;
    };
  }, [text, sha256, byteLength]);
  return (
    <p
      className={`record-integrity ${state === "MISMATCH" ? "is-mismatch" : ""}`}
      role={state === "MISMATCH" ? "alert" : "status"}
    >
      {state === "MATCH"
        ? "Returned text matches its record SHA-256 and UTF-8 byte length. This checks consistency, not authenticity or accounting correctness."
        : state === "MISMATCH"
          ? "Source text does not match its returned hash or byte length. Treat this response as inconsistent; refresh the source read."
          : state === "UNAVAILABLE"
            ? "Text integrity could not be checked in this browser. No matching-hash claim is made."
            : "Checking returned source-text integrity…"}
    </p>
  );
}

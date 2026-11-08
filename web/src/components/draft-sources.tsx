"use client";
import { useCallback, type ReactNode } from "react";
import type { components } from "@/lib/api.generated";
import { api } from "@/lib/api";
import { useSourcePage, sourceError } from "@/lib/use-source-page";
import { formatAmount } from "@/lib/money-format";
import type { Ledger, Statement, Payment } from "@/lib/draft-create";
import { Button } from "./button";
type Batch = components["schemas"]["ImportBatch"];
function Paging({
  page,
  label,
}: {
  page: {
    loading: boolean;
    error: string;
    cursor: string | null;
    reload: () => void;
    more: () => unknown;
  };
  label: string;
}) {
  return (
    <div className="source-page-status">
      {page.loading && <p role="status">Loading {label}…</p>}
      {page.error && (
        <div role="alert">
          <p>{page.error}</p>
          <Button
            type="button"
            variant="secondary"
            disabled={page.loading}
            onClick={page.reload}
          >
            Reload {label}
          </Button>
        </div>
      )}
      {page.cursor && !page.error && (
        <Button
          type="button"
          variant="secondary"
          disabled={page.loading}
          onClick={() => page.more()}
        >
          Load more {label}
        </Button>
      )}
    </div>
  );
}
function Empty({ children }: { children: ReactNode }) {
  return <p className="source-empty">{children}</p>;
}
export function LedgerPicker({
  value,
  onChange,
}: {
  value: string;
  onChange: (ledger: Ledger) => void;
}) {
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await api.GET("/api/v1/ledgers", {
        signal,
        params: { query: { limit: "50", ...(cursor ? { cursor } : {}) } },
      });
      if (!r.data) throw sourceError(r.response.status);
      return r.data;
    },
    [],
  );
  const page = useSourcePage(loader),
    eligible = (page.error ? [] : page.items).filter(
      (l) => l.purpose === "SANDBOX" && l.source === "NATIVE",
    );
  return (
    <div className="source-ledger">
      <label htmlFor="create-ledger">Sandbox ledger</label>
      <select
        id="create-ledger"
        value={value}
        onChange={(e) => {
          const l = eligible.find((l) => l.id === e.target.value);
          if (l) onChange(l);
        }}
      >
        <option value="" disabled>
          Choose a ledger
        </option>
        {eligible.map((l) => (
          <option value={l.id} key={l.id}>
            {l.name} · {l.functional_currency}
          </option>
        ))}
      </select>
      <p>Native SANDBOX only · Accounting UNREVIEWED</p>
      {!page.loading && !page.error && !eligible.length && (
        <Empty>No sandbox ledgers on this page. Load more if available.</Empty>
      )}
      <Paging page={page} label="ledgers" />
    </div>
  );
}
export function StatementPicker({
  ledgerId,
  batch,
  onBatch,
  statement,
  onSelect,
  description = "Stored import outcomes. Choosing a statement copies its stated values into the draft.",
}: {
  description?: string;
  ledgerId: string;
  batch: Batch | null;
  onBatch: (b: Batch) => void;
  statement: Statement | null;
  onSelect: (s: Statement) => void;
}) {
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await api.GET("/api/v1/imports", {
        signal,
        params: {
          query: {
            ledger_id: ledgerId,
            limit: "20",
            ...(cursor ? { cursor } : {}),
          },
        },
      });
      if (!r.data) throw sourceError(r.response.status);
      if (r.data.items.some((item) => item.ledger_id !== ledgerId))
        throw new Error(
          "Sources did not match this ledger. Reload before selecting.",
        );
      return r.data;
    },
    [ledgerId],
  );
  const page = useSourcePage(loader);
  return (
    <section className="source-section">
      <div className="source-section-title">
        <span>01</span>
        <div>
          <h3>Choose a source statement</h3>
          <p>{description}</p>
        </div>
      </div>
      <label className="source-select-label" htmlFor="create-import">
        Import batch
      </label>
      <select
        id="create-import"
        value={batch?.id ?? ""}
        onChange={(e) => {
          const b = page.items.find((b) => b.id === e.target.value);
          if (b) onBatch(b);
        }}
      >
        <option value="" disabled>
          Choose an import batch
        </option>
        {(page.error ? [] : page.items).map((b) => (
          <option value={b.id} key={b.id} disabled={b.status !== "STAGED"}>
            {b.created_at.slice(0, 10)} · {b.status} ·{" "}
            {b.source_file.sha256.slice(0, 12)}
          </option>
        ))}
      </select>
      {!page.loading && !page.error && !page.items.length && (
        <Empty>
          No import batches on this page. Uploads are not available.
        </Empty>
      )}
      <Paging page={page} label="imports" />
      {batch && (
        <>
          <div className="source-provenance">
            <span className="outline-tag">{batch.status}</span>
            <span>Ledger effect {batch.ledger_effect}</span>
            <p>
              Source file SHA-256 <code>{batch.source_file.sha256}</code>
            </p>
          </div>
          <Statements
            key={batch.id}
            batchId={batch.id}
            ledgerId={ledgerId}
            selected={statement?.id ?? ""}
            onSelect={onSelect}
          />
        </>
      )}
    </section>
  );
}
function Statements({
  batchId,
  ledgerId,
  selected,
  onSelect,
}: {
  batchId: string;
  ledgerId: string;
  selected: string;
  onSelect: (s: Statement) => void;
}) {
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await api.GET("/api/v1/imports/{import_id}/staged-documents", {
        signal,
        params: {
          path: { import_id: batchId },
          query: { limit: "20", ...(cursor ? { cursor } : {}) },
        },
      });
      if (!r.data) throw sourceError(r.response.status);
      if (
        r.data.import_batch_id !== batchId ||
        r.data.items.some(
          (item) =>
            item.ledger_id !== ledgerId || item.import_batch_id !== batchId,
        )
      )
        throw new Error(
          "Statements did not match this import and ledger. Reload before selecting.",
        );
      return r.data;
    },
    [batchId, ledgerId],
  );
  const page = useSourcePage(loader);
  return (
    <div className="source-statements">
      {(page.error ? [] : page.items).map((s) => (
        <label
          className={`source-option ${selected === s.id ? "is-selected" : ""}`}
          key={s.id}
        >
          <input
            type="radio"
            name="source-statement"
            checked={selected === s.id}
            disabled={s.status === "POSTED"}
            onChange={() => onSelect(s)}
          />
          <span className="source-option-content">
            <strong>{s.payout_id}</strong>
            <span>
              {s.processor_ref} · {s.payout_date} · {s.provenance}
            </span>
            <span className="source-money">
              Gross {s.gross.currency}{" "}
              {formatAmount(s.gross.amount, s.gross.currency)} · Fee{" "}
              {formatAmount(s.fee.amount, s.fee.currency)} · Net{" "}
              {formatAmount(s.net.amount, s.net.currency)}
            </span>
            <span>
              {s.item_count} source items · Validation {s.validation_status} ·{" "}
              {s.duplicate_batch_count} duplicate batches ·{" "}
              {s.conflicting_batch_count} conflicts
            </span>
            <span className="source-status">
              Workflow {s.status} · version {s.version}
            </span>
            {s.reserved_by_proposal_id && (
              <span className="source-warning">
                {s.status === "POSTED"
                  ? "Posted by proposal "
                  : "Reserved by proposal "}
                {s.reserved_by_proposal_id}.
                {s.status === "POSTED"
                  ? " This statement cannot be reserved again."
                  : " You may cite it in a draft; submission must acquire the reservation."}
              </span>
            )}
            <span className="source-untrusted">{s.description_untrusted}</span>
            <span className="source-items">
              {s.items.map((i) => (
                <span key={i.line_no}>
                  {i.item_ref} · {i.kind} · {i.amount.currency}{" "}
                  {formatAmount(i.amount.amount, i.amount.currency)}
                </span>
              ))}
            </span>
          </span>
        </label>
      ))}
      {!page.loading && !page.error && !page.items.length && (
        <Empty>No staged statements in this batch.</Empty>
      )}
      <Paging page={page} label="statements" />
    </div>
  );
}
export function PaymentPicker({
  ledgerId,
  selected,
  onToggle,
}: {
  ledgerId: string;
  selected: readonly Pick<Payment, "id">[];
  onToggle: (p: Payment) => void;
}) {
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await api.GET("/api/v1/ledgers/{ledger_id}/documents", {
        signal,
        params: {
          path: { ledger_id: ledgerId },
          query: {
            type: "PAYMENT",
            limit: "20",
            ...(cursor ? { cursor } : {}),
          },
        },
      });
      if (!r.data) throw sourceError(r.response.status);
      if (
        r.data.ledger_id !== ledgerId ||
        r.data.items.some(
          (item) => item.ledger_id !== ledgerId || item.type !== "PAYMENT",
        )
      )
        throw new Error(
          "Sources did not match this ledger. Reload before selecting.",
        );
      return r.data;
    },
    [ledgerId],
  );
  const page = useSourcePage(loader);
  return (
    <section className="source-section">
      <div className="source-section-title">
        <span>02</span>
        <div>
          <h3>Select payment records</h3>
          <p>
            Compare processor item references yourself. Nothing is paired or
            approved automatically.
          </p>
        </div>
      </div>
      <div className="source-payment-list">
        {(page.error ? [] : page.items).map((p) => (
          <label
            className={`source-option ${selected.some((s) => s.id === p.id) ? "is-selected" : ""}`}
            key={p.id}
          >
            <input
              type="checkbox"
              checked={selected.some((s) => s.id === p.id)}
              disabled={!p.amount}
              onChange={() => onToggle(p)}
            />
            <span className="source-option-content">
              <strong>
                {p.processor_item_id ?? p.source_ref}
                <span
                  className={`source-status ${p.status === "SETTLED" ? "source-warning" : ""}`}
                >
                  {p.status}
                </span>
              </strong>
              <span>
                {p.party?.name ?? "No party"} · {p.processor_ref} · {p.date} ·
                Version {p.version}
              </span>
              <span className="source-money">
                {p.amount
                  ? `${p.amount.currency} ${formatAmount(p.amount.amount, p.amount.currency)}`
                  : "Amount unavailable"}
              </span>
              <span>
                {p.provenance} · {p.source_ref}
              </span>
              {p.status === "SETTLED" && (
                <span className="source-warning">
                  Already settled. Draft acceptance does not establish
                  settlement eligibility.
                </span>
              )}
            </span>
          </label>
        ))}
      </div>
      {!page.loading && !page.error && !page.items.length && (
        <Empty>
          No payment records on this page. You cannot create a settlement draft
          without a payment.
        </Empty>
      )}
      <Paging page={page} label="payments" />
    </section>
  );
}

"use client";
import type { ReactNode } from "react";
import { AlertCircle, FileSearch, RefreshCw } from "lucide-react";
import type { components } from "@/lib/api.generated";
import { formatAmount } from "@/lib/money-format";
import { Button } from "./button";
export function MoneyValue({
  value,
}: {
  value: components["schemas"]["Money"] | null;
}) {
  return value ? (
    <span className="record-money">
      {value.currency} {formatAmount(value.amount, value.currency)}
    </span>
  ) : (
    <span className="muted">Not applicable</span>
  );
}
export function RecordStatus({ value }: { value: string | null }) {
  return value ? (
    <span
      className={`record-status ${["REJECTED", "INVALID"].includes(value) ? "is-refused" : value === "SETTLED" || value === "DUPLICATE" ? "is-caution" : ""}`}
    >
      {value.replaceAll("_", " ")}
    </span>
  ) : (
    <span className="muted small">No status model</span>
  );
}
export function ReadFailure({
  message,
  retry,
  refresh,
}: {
  message: string;
  retry: () => void;
  refresh?: () => void;
}) {
  return (
    <div className="record-empty record-error" role="alert">
      <AlertCircle size={24} />
      <h3>Records unavailable</h3>
      <p>{message}</p>
      <div className="financial-actions">
        <Button type="button" variant="secondary" onClick={retry}>
          Retry read
        </Button>
        {refresh && (
          <Button type="button" variant="ghost" onClick={refresh}>
            Refresh session
          </Button>
        )}
      </div>
    </div>
  );
}
export function RecordEmpty({ children }: { children: ReactNode }) {
  return (
    <div className="record-empty">
      <FileSearch size={28} />
      <h3>No records to display</h3>
      <p>{children}</p>
    </div>
  );
}
export function PageActions({
  page,
  label,
}: {
  page: {
    loading: boolean;
    cursor: string | null;
    more: () => unknown;
    reload: () => void;
  };
  label: string;
}) {
  return (
    <div className="record-page-actions">
      <Button
        type="button"
        variant="ghost"
        onClick={page.reload}
        disabled={page.loading}
      >
        <RefreshCw size={14} />
        Refresh {label}
      </Button>
      {page.cursor && (
        <Button
          type="button"
          variant="secondary"
          disabled={page.loading}
          onClick={() => page.more()}
        >
          Load more {label}
        </Button>
      )}
      {page.loading && <span role="status">Loading {label}…</span>}
    </div>
  );
}
export function ReferenceFacts({
  id,
  hash,
  kind,
}: {
  id: string;
  hash?: string | null;
  kind?: string;
}) {
  return (
    <div className="record-reference">
      {kind && <span>{kind.replaceAll("_", " ")}</span>}
      <code>{id}</code>
      {hash && (
        <>
          <span>SHA-256</span>
          <code>{hash}</code>
        </>
      )}
    </div>
  );
}
export function Fact({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

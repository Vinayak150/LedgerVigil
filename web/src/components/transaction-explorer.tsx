"use client";
import { useCallback, useRef, useState } from "react";
import { ArrowUpRight, FileInput, Files, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import type { components } from "@/lib/api.generated";
import { useSourcePage, sourceError } from "@/lib/use-source-page";
import { LedgerPicker } from "./draft-sources";
import { Button } from "./button";
import { DocumentInspector } from "./document-inspector";
import { ImportInspector } from "./import-inspector";
import { PublicPlanBrowser } from "./public-plan-browser";
import {
  MoneyValue,
  RecordStatus,
  ReadFailure,
  RecordEmpty,
  PageActions,
} from "./record-ui";
type Ledger = components["schemas"]["Ledger"];
type Document = components["schemas"]["DocumentSummary"];
type Batch = components["schemas"]["ImportBatch"];
export function TransactionExplorer({
  onSessionRefresh,
  csrfToken,
  canImport,
}: {
  onSessionRefresh: () => void;
  csrfToken: string;
  canImport: boolean;
}) {
  const [ledger, setLedger] = useState<Ledger | null>(null),
    [view, setView] = useState<"documents" | "imports" | "public">("documents");
  return (
    <section
      className="record-explorer"
      aria-label="Transaction and source explorer"
    >
      <div className="notice record-notice">
        <ShieldCheck size={18} />
        <span>
          SANDBOX — not accounting records. Accounting UNREVIEWED. Stored
          statuses describe records; they do not establish matching or
          eligibility.
        </span>
      </div>
      <div className="card record-ledger-card">
        <LedgerPicker value={ledger?.id ?? ""} onChange={setLedger} />
        {ledger && (
          <span className="record-ledger-sequence">
            {ledger.functional_currency} · {ledger.profile_version} ·{" "}
            {ledger.state}
          </span>
        )}
      </div>
      {ledger ? (
        <div className="card record-browser" key={ledger.id}>
          <div className="record-browser-top">
            <div className="record-view-switch" aria-label="Record collections">
              <Button
                type="button"
                variant={view === "documents" ? "primary" : "ghost"}
                aria-pressed={view === "documents"}
                onClick={() => setView("documents")}
              >
                <Files size={15} />
                Documents
              </Button>
              <Button
                type="button"
                variant={view === "imports" ? "primary" : "ghost"}
                aria-pressed={view === "imports"}
                onClick={() => setView("imports")}
              >
                <FileInput size={15} />
                Source imports
              </Button>
              <Button
                type="button"
                variant={view === "public" ? "primary" : "ghost"}
                aria-pressed={view === "public"}
                onClick={() => setView("public")}
              >
                Public dataset
              </Button>
            </div>
            <span className="muted small">{ledger.name}</span>
          </div>
          {view === "documents" ? (
            <DocumentCollection
              ledger={ledger}
              onSessionRefresh={onSessionRefresh}
            />
          ) : view === "public" ? (
            <PublicPlanBrowser
              key={ledger.id}
              ledger={ledger}
              csrfToken={csrfToken}
              canImport={canImport}
              onSessionRefresh={onSessionRefresh}
            />
          ) : (
            <ImportCollection
              ledger={ledger}
              onSessionRefresh={onSessionRefresh}
            />
          )}
        </div>
      ) : (
        <div className="card record-empty">
          <Files size={32} />
          <h3>Start with a ledger</h3>
          <p>
            Choose a sandbox ledger to see its current documents, import
            outcomes and evidence. No records are loaded across ledgers.
          </p>
        </div>
      )}
      <p className="record-availability">
        Available: current records and source provenance. Date/search filters,
        historical versions, full journal lines and reconciliation actions are
        not available.
      </p>
    </section>
  );
}
function DocumentCollection({
  ledger,
  onSessionRefresh,
}: {
  ledger: Ledger;
  onSessionRefresh: () => void;
}) {
  const [kind, setKind] = useState("ALL"),
    [status, setStatus] = useState("ALL");
  return (
    <>
      <div className="record-filters">
        <div>
          <label htmlFor="record-type">Document type</label>
          <select
            id="record-type"
            value={kind}
            onChange={(e) => {
              setKind(e.target.value);
              setStatus("ALL");
            }}
          >
            <option value="ALL">All available types</option>
            <option value="PAYMENT">Payment</option>
            <option value="SETTLEMENT">Settlement</option>
            <option value="MANUAL_JOURNAL">Manual journal</option>
          </select>
        </div>
        <div>
          <label htmlFor="record-status">Stored status</label>
          <select
            id="record-status"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            disabled={kind === "MANUAL_JOURNAL"}
          >
            <option value="ALL">All statuses</option>
            {kind !== "SETTLEMENT" && (
              <>
                <option value="RECEIVED">RECEIVED</option>
                <option value="SETTLED">SETTLED</option>
              </>
            )}
            {kind !== "PAYMENT" && <option value="RECORDED">RECORDED</option>}
          </select>
        </div>
      </div>
      <DocumentRows
        key={`${kind}:${status}`}
        ledger={ledger}
        kind={kind}
        status={status}
        onSessionRefresh={onSessionRefresh}
      />
    </>
  );
}
function DocumentRows({
  ledger,
  kind,
  status,
  onSessionRefresh,
}: {
  ledger: Ledger;
  kind: string;
  status: string;
  onSessionRefresh: () => void;
}) {
  const [sequence, setSequence] = useState<number | null>(null),
    [selected, setSelected] = useState<Document | null>(null);
  const back = useRef<HTMLElement | null>(null);
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await api.GET("/api/v1/ledgers/{ledger_id}/documents", {
        signal,
        params: {
          path: { ledger_id: ledger.id },
          query: {
            limit: "25",
            ...(kind !== "ALL" ? { type: kind } : {}),
            ...(status !== "ALL" ? { status } : {}),
            ...(cursor ? { cursor } : {}),
          },
        },
      });
      if (!r.data) throw sourceError(r.response.status);
      if (
        r.data.ledger_id !== ledger.id ||
        r.data.items.some((i) => i.ledger_id !== ledger.id)
      )
        throw new Error(
          "The document response did not match this ledger. No records are shown.",
        );
      if (!signal.aborted) setSequence(r.data.as_of_seq);
      return r.data;
    },
    [ledger.id, kind, status],
  );
  const page = useSourcePage(loader);
  if (page.error)
    return (
      <ReadFailure
        message={page.error}
        retry={page.reload}
        refresh={onSessionRefresh}
      />
    );
  return (
    <>
      <div className="record-list-caption">
        <span>{page.items.length} documents loaded · newest first</span>
        <span>
          {sequence === null
            ? "Current records"
            : `Latest page read at sequence ${sequence}`}
        </span>
      </div>
      {page.items.length ? (
        <div className="record-table-wrap">
          <table className="record-table">
            <caption className="sr-only">
              Current documents in {ledger.name}
            </caption>
            <thead>
              <tr>
                <th scope="col">Document / source reference</th>
                <th scope="col">Stored status</th>
                <th scope="col">Date</th>
                <th scope="col">Current amount</th>
                <th scope="col">
                  <span className="sr-only">Inspect</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((d) => (
                <tr key={d.id}>
                  <td>
                    <strong>
                      {d.processor_item_id ?? d.payout_id ?? d.source_ref}
                    </strong>
                    <span>
                      {d.type.replaceAll("_", " ")} · Version {d.version}
                    </span>
                    <small>
                      {d.party?.name ?? d.processor_ref ?? d.provenance}
                    </small>
                  </td>
                  <td>
                    <RecordStatus value={d.status} />
                  </td>
                  <td>{d.date ?? "Not available"}</td>
                  <td>
                    <MoneyValue value={d.amount} />
                  </td>
                  <td>
                    <Button
                      type="button"
                      variant="ghost"
                      aria-label={`Inspect record ${d.processor_item_id ?? d.payout_id ?? d.source_ref}`}
                      onClick={(e) => {
                        back.current = e.currentTarget;
                        setSelected(d);
                      }}
                    >
                      Inspect
                      <ArrowUpRight size={14} />
                    </Button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        !page.loading && (
          <RecordEmpty>
            No documents match these available filters. No figures have been
            substituted.
          </RecordEmpty>
        )
      )}
      <PageActions page={page} label="documents" />
      <p className="record-page-note">
        Pages are separate current reads, not a historical snapshot. Manual
        journals have no amount or status model here.
      </p>
      {selected && (
        <DocumentInspector
          id={selected.id}
          ledgerId={ledger.id}
          onClose={() => {
            setSelected(null);
            requestAnimationFrame(() => {
              if (back.current?.isConnected) back.current.focus();
            });
          }}
        />
      )}
    </>
  );
}
function ImportCollection({
  ledger,
  onSessionRefresh,
}: {
  ledger: Ledger;
  onSessionRefresh: () => void;
}) {
  const [selected, setSelected] = useState<Batch | null>(null);
  const back = useRef<HTMLElement | null>(null);
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await api.GET("/api/v1/imports", {
        signal,
        params: {
          query: {
            ledger_id: ledger.id,
            limit: "25",
            ...(cursor ? { cursor } : {}),
          },
        },
      });
      if (!r.data) throw sourceError(r.response.status);
      if (r.data.items.some((i) => i.ledger_id !== ledger.id))
        throw new Error(
          "The import response did not match this ledger. No imports are shown.",
        );
      return r.data;
    },
    [ledger.id],
  );
  const page = useSourcePage(loader);
  if (page.error)
    return (
      <ReadFailure
        message={page.error}
        retry={page.reload}
        refresh={onSessionRefresh}
      />
    );
  return (
    <>
      <div className="record-list-caption">
        <span>{page.items.length} imports loaded · newest first</span>
        <span>Stored outcomes · ledger effect NONE</span>
      </div>
      {page.items.length ? (
        <ul className="record-import-list">
          {page.items.map((b) => (
            <li key={b.id}>
              <div className="record-import-icon">
                <FileInput size={21} />
              </div>
              <div className="record-import-main">
                <strong>
                  {b.source_file.declared_format.replaceAll("_", " ")}
                </strong>
                <span>
                  {b.created_at.slice(0, 10)} · {b.parser} ·{" "}
                  {b.source_file.byte_size.toLocaleString("en-GB")} bytes
                </span>
                <code>SHA-256 {b.source_file.sha256}</code>
                {b.failure_code && (
                  <span className="record-import-refusal">
                    {b.failure_code}
                  </span>
                )}
              </div>
              <RecordStatus value={b.status} />
              <Button
                type="button"
                variant="ghost"
                aria-label={`Inspect import ${b.source_file.sha256.slice(0, 12)}`}
                onClick={(e) => {
                  back.current = e.currentTarget;
                  setSelected(b);
                }}
              >
                Inspect
                <ArrowUpRight size={14} />
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        !page.loading && (
          <RecordEmpty>
            No imports returned for this ledger. Uploads are not available in
            this release.
          </RecordEmpty>
        )
      )}
      <PageActions page={page} label="imports" />
      <p className="record-page-note">
        STAGED, REJECTED and DUPLICATE describe stored ingestion outcomes. No
        reconcile, discard or post action is available.
      </p>
      {selected && (
        <ImportInspector
          id={selected.id}
          ledgerId={ledger.id}
          onClose={() => {
            setSelected(null);
            requestAnimationFrame(() => {
              if (back.current?.isConnected) back.current.focus();
            });
          }}
        />
      )}
    </>
  );
}

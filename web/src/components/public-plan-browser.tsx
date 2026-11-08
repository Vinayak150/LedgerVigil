"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowUpRight, Database, FileSearch, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import type { components } from "@/lib/api.generated";
import {
  PUBLIC_PLAN_MANIFEST,
  assertGroup,
  groupLabel,
  planError,
} from "@/lib/public-plan";
import { useSourcePage } from "@/lib/use-source-page";
import { Button } from "./button";
import {
  PageActions,
  ReadFailure,
  RecordEmpty,
  ReferenceFacts,
} from "./record-ui";
import { PublicPlanInspector } from "./public-plan-inspector";
type Ledger = components["schemas"]["Ledger"];
type Registration = components["schemas"]["Registration"];
export function PublicPlanBrowser({
  ledger,
  csrfToken,
  canImport,
  onSessionRefresh,
}: {
  ledger: Ledger;
  csrfToken: string;
  canImport: boolean;
  onSessionRefresh: () => void;
}) {
  const [registration, setRegistration] = useState<Registration | null>(null);
  const [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  const active = useRef<AbortController | null>(null),
    busy = useRef(false),
    key = useRef<string | null>(null);
  useEffect(
    () => () => {
      active.current?.abort();
    },
    [],
  );
  async function open() {
    if (busy.current || !canImport) return;
    busy.current = true;
    setLoading(true);
    setError("");
    const controller = new AbortController();
    active.current = controller;
    key.current ??= crypto.randomUUID();
    try {
      const r = await api.POST("/api/v1/source-plan-registrations", {
        signal: controller.signal,
        headers: { "X-CSRF-Token": csrfToken, "Idempotency-Key": key.current },
        body: { ledger_id: ledger.id, manifest_sha256: PUBLIC_PLAN_MANIFEST },
      });
      if (controller.signal.aborted) return;
      if (!r.data) throw planError(r.response.status, true);
      if (
        r.data.ledger_id !== ledger.id ||
        r.data.manifest_sha256 !== PUBLIC_PLAN_MANIFEST ||
        r.data.classification !== "PUBLIC_OBSERVATIONAL" ||
        r.data.posting_eligibility !== "UNAVAILABLE" ||
        r.data.accounting_status !== "UNREVIEWED"
      )
        throw new Error(
          "Source scope could not be verified. No records are shown.",
        );
      setRegistration(r.data);
    } catch (e) {
      if (!controller.signal.aborted)
        setError(
          e instanceof Error
            ? e.message
            : "Source registration is unavailable.",
        );
    } finally {
      if (!controller.signal.aborted) {
        busy.current = false;
        setLoading(false);
      }
    }
  }
  if (
    ledger.functional_currency !== "GBP" ||
    ledger.purpose !== "SANDBOX" ||
    ledger.source !== "NATIVE"
  )
    return (
      <RecordEmpty>
        This public snapshot is available only with a native GBP sandbox ledger.
        Choose a GBP ledger above; no currency conversion is performed.
      </RecordEmpty>
    );
  if (!registration)
    return (
      <section
        className="public-plan-intro"
        aria-labelledby="public-plan-title"
      >
        <div className="public-plan-intro-heading">
          <span className="public-plan-symbol">
            <Database size={25} />
          </span>
          <div>
            <span className="eyebrow">PUBLIC OBSERVATIONAL DATA</span>
            <h2 id="public-plan-title">
              Inspect the evidence before the numbers.
            </h2>
            <p>
              UCI Online Retail II · invoice groups and original source cells
            </p>
          </div>
        </div>
        <p>
          Open the published observation plan in this ledger’s workspace. This
          records a source registration and its audit entry; it does not import
          transactions or change balances.
        </p>
        <div className="notice record-notice">
          <ShieldCheck size={18} />
          <span>
            Accounting UNREVIEWED · Posting UNAVAILABLE. Public retail
            observations do not establish settlement, reconciliation or
            accounting correctness.
          </span>
        </div>
        {error && (
          <div role="alert" className="public-plan-open-error">
            <p>{error}</p>
            <Button variant="ghost" onClick={onSessionRefresh}>
              Refresh session
            </Button>
          </div>
        )}
        {canImport ? (
          <Button onClick={() => void open()} disabled={loading}>
            <FileSearch size={16} />
            {loading
              ? "Opening published dataset…"
              : error
                ? "Retry opening dataset"
                : "Open published dataset"}
          </Button>
        ) : (
          <p className="muted">
            Opening a registration requires source import permission. Contact
            your administrator for access.
          </p>
        )}
        {loading && (
          <p role="status" className="small muted">
            Checking the registration. A retry will reuse this request’s
            idempotency key.
          </p>
        )}
        <p className="public-plan-attribution">
          Source:{" "}
          <a
            href="https://archive.ics.uci.edu/dataset/502/online+retail+ii"
            target="_blank"
            rel="noreferrer"
          >
            UCI Machine Learning Repository <ArrowUpRight size={12} />
          </a>{" "}
          · Daqing Chen · CC BY 4.0. Read-only observation plan derived from the
          original workbook.
        </p>
      </section>
    );
  return (
    <RegisteredPlan
      key={registration.id}
      registration={registration}
      onSessionRefresh={onSessionRefresh}
    />
  );
}
function RegisteredPlan({
  registration,
  onSessionRefresh,
}: {
  registration: Registration;
  onSessionRefresh: () => void;
}) {
  const [status, setStatus] = useState<"HOLD" | "CANDIDATE_REVIEW" | "ALL">(
    "HOLD",
  );
  return (
    <section aria-label="Public invoice observation plan">
      <div className="public-plan-summary">
        <div>
          <span className="eyebrow">UCI ONLINE RETAIL II</span>
          <h2>Every invoice. Original evidence.</h2>
          <p>
            Public observations · Accounting UNREVIEWED · Posting UNAVAILABLE
          </p>
        </div>
        <dl className="public-plan-counts">
          <div>
            <dt>Source rows</dt>
            <dd>{registration.source_rows.toLocaleString("en-GB")}</dd>
          </div>
          <div>
            <dt>Held groups</dt>
            <dd>{registration.held.toLocaleString("en-GB")}</dd>
          </div>
          <div>
            <dt>Candidate review</dt>
            <dd>{registration.candidate_review.toLocaleString("en-GB")}</dd>
          </div>
        </dl>
        <details className="record-technical">
          <summary>
            Snapshot provenance ·{" "}
            {registration.invoice_groups.toLocaleString("en-GB")} groups
          </summary>
          <ReferenceFacts
            id={registration.id}
            hash={registration.manifest_sha256}
            kind="PUBLIC OBSERVATIONAL"
          />
          <p>
            Registration {registration.created_at}. Source counts describe this
            pinned snapshot, not the rows currently loaded.
          </p>
        </details>
      </div>
      <div className="record-filters">
        <div>
          <label htmlFor="public-plan-status">Observation status</label>
          <select
            id="public-plan-status"
            value={status}
            onChange={(e) => setStatus(e.target.value as typeof status)}
          >
            <option value="HOLD">Held for investigation</option>
            <option value="CANDIDATE_REVIEW">Candidate review</option>
            <option value="ALL">All invoice groups</option>
          </select>
        </div>
        <p className="small muted">
          Candidates still require review. No group is eligible for posting.
        </p>
      </div>
      <GroupRows
        key={`${registration.id}:${status}`}
        registrationId={registration.id}
        status={status}
        onSessionRefresh={onSessionRefresh}
      />
    </section>
  );
}
function GroupRows({
  registrationId,
  status,
  onSessionRefresh,
}: {
  registrationId: string;
  status: "HOLD" | "CANDIDATE_REVIEW" | "ALL";
  onSessionRefresh: () => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await api.GET(
        "/api/v1/source-plan-registrations/{registration_id}/groups",
        {
          signal,
          params: {
            path: { registration_id: registrationId },
            query: {
              limit: "25",
              ...(status === "ALL" ? {} : { status }),
              ...(cursor ? { cursor } : {}),
            },
          },
        },
      );
      if (!r.data) throw planError(r.response.status);
      if (r.data.registration_id !== registrationId)
        throw new Error(
          "Source scope could not be verified. No invoices are shown.",
        );
      r.data.items.forEach(assertGroup);
      if (status !== "ALL" && r.data.items.some((g) => g.status !== status))
        throw new Error(
          "The response did not match this status filter. Refresh the list.",
        );
      return {
        ...r.data,
        items: r.data.items.map((g) => ({ ...g, id: g.group_id })),
      };
    },
    [registrationId, status],
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
        <span aria-live="polite">
          {page.items.length} invoice groups loaded
        </span>
        <span>Source order · repeated rows retained</span>
      </div>
      <div className="record-table-wrap">
        <table className="public-plan-table">
          <caption className="sr-only">
            Invoice observation groups and investigation holds
          </caption>
          <thead>
            <tr>
              <th scope="col">Invoice group</th>
              <th scope="col">Observation status</th>
              <th scope="col">Source rows</th>
              <th scope="col">Investigation holds</th>
              <th scope="col">Inspect</th>
            </tr>
          </thead>
          <tbody>
            {page.items.map((g) => (
              <tr key={g.group_id}>
                <th scope="row">
                  {groupLabel(g)}
                  <small>Accounting UNREVIEWED</small>
                </th>
                <td>
                  <span
                    className={`public-plan-status ${g.status === "HOLD" ? "is-held" : ""}`}
                  >
                    {g.status === "HOLD" ? "HOLD" : "CANDIDATE REVIEW"}
                  </span>
                </td>
                <td>{g.source_line_count.toLocaleString("en-GB")}</td>
                <td>
                  {g.hold_reasons.length ? (
                    <>
                      <span className="public-plan-reason">
                        {g.hold_reasons[0].replaceAll("_", " ").toLowerCase()}
                      </span>
                      {g.hold_reasons.length > 1 && (
                        <small>
                          +{g.hold_reasons.length - 1} more in inspector
                        </small>
                      )}
                    </>
                  ) : (
                    <span className="muted">
                      No recorded holds · review required
                    </span>
                  )}
                </td>
                <td>
                  <Button
                    variant="ghost"
                    onClick={() => setSelected(g.group_id)}
                    aria-label={`Inspect ${groupLabel(g)}`}
                  >
                    <FileSearch size={15} />
                    Inspect
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!page.loading && !page.items.length && (
        <RecordEmpty>
          No groups returned for this observation status.
        </RecordEmpty>
      )}
      <PageActions page={page} label="invoice groups" />
      {selected && (
        <PublicPlanInspector
          key={selected}
          registrationId={registrationId}
          groupId={selected}
          onClose={() => setSelected(null)}
          onSessionRefresh={onSessionRefresh}
        />
      )}
    </>
  );
}

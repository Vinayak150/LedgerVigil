"use client";
// Detection review (PROPOSED DET-1..DET-11). Deterministic rules only: no model, score, probability or confidence is shown or
// implied. Facts (statement observation, ledger records), the rule's computed difference and the reviewer's conclusion are
// kept in separate panels. Reviewer decisions change no financial, approval or execution record.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Clock3,
  FileSearch,
  History,
  Play,
  Scale,
  ShieldCheck,
} from "lucide-react";
import type { components } from "@/lib/api.generated";
import {
  optionalApi,
  type DetectionConfiguration,
  type Finding,
} from "@/lib/optional-api";
import { useSourcePage, sourceError } from "@/lib/use-source-page";
import {
  CLASS_LABELS,
  DISMISS_REASONS,
  EXCLUDED_REASONS,
  availableActions,
  dayDifferenceText,
  kindLabel,
  readPendingReview,
  reviewBody,
  reviewProblem,
  reviewStorageKey,
  validNote,
  type PendingReview,
  type ReviewAction,
} from "@/lib/detection-review";
import { LedgerPicker } from "./draft-sources";
import { Button } from "./button";
import { Fact, PageActions, ReadFailure, RecordEmpty } from "./record-ui";
type Ledger = components["schemas"]["Ledger"];
type Json = unknown;

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function DetectionReview({
  tenantId,
  userId,
  csrfToken,
  permissions,
  onSessionRefresh,
}: {
  tenantId: string;
  userId: string;
  csrfToken: string;
  permissions: string[];
  onSessionRefresh: () => void;
}) {
  const [config, setConfig] = useState<DetectionConfiguration | null>(null),
    [state, setState] = useState<
      "loading" | "ready" | "disabled" | "denied" | "failed"
    >("loading"),
    [ledger, setLedger] = useState<Ledger | null>(null),
    [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setState("loading");
    void (async () => {
      try {
        const r = await optionalApi.GET("/api/v1/detection-configuration", {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        if (r.data) {
          setConfig(r.data);
          setState("ready");
        } else
          setState(
            r.response.status === 404
              ? "disabled"
              : r.response.status === 403
                ? "denied"
                : "failed",
          );
      } catch {
        if (!controller.signal.aborted) setState("failed");
      }
    })();
    return () => controller.abort();
  }, [attempt]);
  return (
    <section
      className="record-explorer detection-review"
      aria-label="Detection review"
    >
      <div className="notice record-notice">
        <ShieldCheck size={18} />
        <span>
          Findings come from <strong>deterministic rules</strong> that compare a
          synthetic statement with the native SANDBOX ledger. No model is used,
          and no score, probability or confidence exists. A finding is a prompt
          to check the cited evidence: it is not a fraud determination and not
          an accounting conclusion. Accounting UNREVIEWED.
        </span>
      </div>
      {state === "loading" && (
        <div className="card record-empty" role="status">
          <Clock3 size={26} />
          <h3>Checking whether detection is available</h3>
        </div>
      )}
      {state === "disabled" && (
        <div className="card record-empty">
          <FileSearch size={28} />
          <h3>Detection is not enabled on this server</h3>
          <p>
            The detection feature is off by default (LV_DETECTION_ENABLED is
            unset). No findings are read or shown. An operator can enable it for
            a SANDBOX review session.
          </p>
        </div>
      )}
      {state === "denied" && (
        <div className="card record-empty" role="alert">
          <AlertTriangle size={26} />
          <h3>Your role cannot read detection findings</h3>
          <p>Ask an administrator of this organization for read access.</p>
        </div>
      )}
      {state === "failed" && (
        <ReadFailure
          message="Detection could not be reached. Nothing was changed."
          retry={() => setAttempt((n) => n + 1)}
          refresh={onSessionRefresh}
        />
      )}
      {state === "ready" && config && (
        <>
          <details className="record-technical detection-rules">
            <summary>
              How findings are produced · {config.detector}{" "}
              {config.detector_version}
            </summary>
            <ul>
              {config.rules.map((r) => (
                <li key={r.kind}>
                  <strong>{r.label}</strong>{" "}
                  <span className="record-status">
                    {CLASS_LABELS[r.class] ?? r.class}
                  </span>
                  <p>{r.condition}</p>
                </li>
              ))}
            </ul>
            <p className="small muted">
              When a statement has a conflicting second file, refund lines, or
              source bytes that no longer verify, detection abstains for it
              instead of guessing ({config.abstentions.join(", ")}).
            </p>
            <ul className="small muted">
              {config.limits.map((l) => (
                <li key={l}>{l}</li>
              ))}
            </ul>
          </details>
          <div className="card record-ledger-card">
            <LedgerPicker value={ledger?.id ?? ""} onChange={setLedger} />
          </div>
          {ledger ? (
            <Workbench
              key={ledger.id}
              ledger={ledger}
              config={config}
              tenantId={tenantId}
              userId={userId}
              csrfToken={csrfToken}
              canRun={permissions.includes("propose")}
              canReview={
                permissions.includes("propose") ||
                permissions.includes("review")
              }
              onSessionRefresh={onSessionRefresh}
            />
          ) : (
            <div className="card record-empty">
              <Scale size={30} />
              <h3>Choose a sandbox ledger</h3>
              <p>
                Findings are read for one ledger at a time. Nothing is loaded
                across ledgers or organizations.
              </p>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function Workbench({
  ledger,
  config,
  tenantId,
  userId,
  csrfToken,
  canRun,
  canReview,
  onSessionRefresh,
}: {
  ledger: Ledger;
  config: DetectionConfiguration;
  tenantId: string;
  userId: string;
  csrfToken: string;
  canRun: boolean;
  canReview: boolean;
  onSessionRefresh: () => void;
}) {
  const [kind, setKind] = useState(""),
    [review, setReview] = useState(""),
    [version, setVersion] = useState(""),
    [statement, setStatement] = useState(""),
    [selected, setSelected] = useState<string | null>(null),
    [generation, setGeneration] = useState(0);
  const detail = useRef<HTMLDivElement>(null);
  const showStatement = (id: string) => {
    setStatement(id);
    setSelected(null);
  };
  return (
    <div className="card record-browser">
      <div className="record-browser-top">
        <RunDetection
          ledger={ledger}
          tenantId={tenantId}
          userId={userId}
          csrfToken={csrfToken}
          canRun={canRun}
          onDone={() => setGeneration((n) => n + 1)}
        />
        <span className="muted small">{ledger.name}</span>
      </div>
      <div
        className="record-filters"
        role="group"
        aria-label="Server-side finding filters"
      >
        <div>
          <label htmlFor="finding-kind">Finding kind</label>
          <select
            id="finding-kind"
            value={kind}
            onChange={(e) => setKind(e.target.value)}
          >
            <option value="">All kinds</option>
            {config.rules.map((r) => (
              <option key={r.kind} value={r.kind}>
                {r.label}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label htmlFor="finding-review">Review status</label>
          <select
            id="finding-review"
            value={review}
            onChange={(e) => setReview(e.target.value)}
          >
            <option value="">Any status</option>
            <option value="OPEN">Open</option>
            <option value="CONFIRMED">Confirmed</option>
            <option value="DISMISSED">Dismissed</option>
          </select>
        </div>
        <div>
          <label htmlFor="finding-version">Detector version</label>
          <select
            id="finding-version"
            value={version}
            onChange={(e) => setVersion(e.target.value)}
          >
            <option value="">Any version</option>
            <option value="1.1.0">1.1.0</option>
            <option value="1.0.0">1.0.0</option>
          </select>
        </div>
      </div>
      <p className="record-list-caption">
        <span>
          Filters are applied by the server and match exactly. Freshness
          (current or stale) is computed for each finding when it is read; it is
          shown on every row but cannot be filtered.
        </span>
        {statement && (
          <span>
            Statement <code>{statement}</code>{" "}
            <Button
              type="button"
              variant="ghost"
              onClick={() => setStatement("")}
            >
              Clear statement filter
            </Button>
          </span>
        )}
      </p>
      <FindingList
        key={`${kind}|${review}|${version}|${statement}|${generation}`}
        ledgerId={ledger.id}
        filters={{ kind, review, version, statement }}
        selected={selected}
        onSelect={(id) => {
          setSelected(id);
          requestAnimationFrame(() => detail.current?.focus());
        }}
        onSessionRefresh={onSessionRefresh}
      />
      <div ref={detail} tabIndex={-1} className="detection-detail-anchor">
        {selected && (
          <FindingDetail
            key={selected}
            id={selected}
            ledgerId={ledger.id}
            config={config}
            tenantId={tenantId}
            userId={userId}
            csrfToken={csrfToken}
            canReview={canReview}
            onStatement={showStatement}
            onChanged={() => setGeneration((n) => n + 1)}
            onClose={() => setSelected(null)}
            onSessionRefresh={onSessionRefresh}
          />
        )}
      </div>
    </div>
  );
}

function RunDetection({
  ledger,
  tenantId,
  userId,
  csrfToken,
  canRun,
  onDone,
}: {
  ledger: Ledger;
  tenantId: string;
  userId: string;
  csrfToken: string;
  canRun: boolean;
  onDone: () => void;
}) {
  const key = `lv.detection-run.1:${tenantId}:${userId}:${ledger.id}`;
  const [pending, setPending] = useState<{
      key: string;
      startedAt: number;
    } | null>(null),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  useEffect(() => {
    try {
      const v = JSON.parse(storage()?.getItem(key) ?? "null");
      if (
        v &&
        typeof v.key === "string" &&
        Number.isSafeInteger(v.startedAt) &&
        Date.now() - v.startedAt < 23 * 3600e3
      )
        setPending(v);
    } catch {
      /* ignore unreadable recovery */
    }
  }, [key]);
  async function send(attempt: { key: string; startedAt: number }) {
    setBusy(true);
    setMessage("");
    storage()?.setItem(key, JSON.stringify(attempt));
    setPending(attempt);
    try {
      const r = await optionalApi.POST(
        "/api/v1/ledgers/{ledger_id}/detection-runs",
        {
          params: { path: { ledger_id: ledger.id } },
          headers: {
            "X-CSRF-Token": csrfToken,
            "Idempotency-Key": attempt.key,
          },
          body: {},
        },
      );
      const status = r.response.status,
        code = (r.error as { code?: string } | undefined)?.code;
      if (r.data) {
        storage()?.removeItem(key);
        setPending(null);
        setMessage(
          `Detection run completed: ${r.data.finding_count} finding(s), ${r.data.new_finding_count} new, ${r.data.abstentions.length} statement(s) abstained.`,
        );
        onDone();
      } else if (status === 409 && code === "REQUEST_IN_PROGRESS") {
        setMessage("The same run is still in progress. Resend it in a moment.");
      } else {
        if (status !== 401 && status < 500) {
          storage()?.removeItem(key);
          setPending(null);
        }
        setMessage(
          status === 502
            ? "Detection could not be verified, so nothing was recorded (for example a statement arrived during the run). Start a new run."
            : status === 403
              ? "Your role cannot run detection."
              : status === 422
                ? "Detection cannot run for this ledger (for example, more than 200 statements)."
                : status === 401
                  ? "Your session has ended. Refresh it; the pending run is kept and can be resent."
                  : "The run could not be confirmed. Resend the same run to find out whether it completed.",
        );
      }
    } catch {
      setMessage(
        "The run could not be confirmed. Resend the same run to find out whether it completed.",
      );
    } finally {
      setBusy(false);
    }
  }
  if (!canRun)
    return (
      <span className="small muted">
        Your role can read findings. Running detection needs the propose
        permission.
      </span>
    );
  return (
    <div className="detection-run">
      {pending ? (
        <Button
          type="button"
          onClick={() => void send(pending)}
          disabled={busy}
        >
          <Play size={15} />
          Resend the same detection run
        </Button>
      ) : (
        <Button
          type="button"
          onClick={() =>
            void send({ key: crypto.randomUUID(), startedAt: Date.now() })
          }
          disabled={busy}
        >
          <Play size={15} />
          Run detection on this ledger
        </Button>
      )}
      {busy && <span role="status">Running detection…</span>}
      {message && (
        <p className="small" role="status">
          {message}
        </p>
      )}
    </div>
  );
}

function FindingList({
  ledgerId,
  filters,
  selected,
  onSelect,
  onSessionRefresh,
}: {
  ledgerId: string;
  filters: { kind: string; review: string; version: string; statement: string };
  selected: string | null;
  onSelect: (id: string) => void;
  onSessionRefresh: () => void;
}) {
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await optionalApi.GET(
        "/api/v1/ledgers/{ledger_id}/detection-findings",
        {
          signal,
          params: {
            path: { ledger_id: ledgerId },
            query: {
              limit: "25",
              ...(filters.kind ? { kind: filters.kind } : {}),
              ...(filters.review ? { review_status: filters.review } : {}),
              ...(filters.version ? { detector_version: filters.version } : {}),
              ...(filters.statement
                ? { staged_document_id: filters.statement }
                : {}),
              ...(cursor ? { cursor } : {}),
            },
          },
        },
      );
      if (!r.data) throw sourceError(r.response.status);
      if (
        r.data.ledger_id !== ledgerId ||
        r.data.items.some((f) => f.ledger_id !== ledgerId)
      )
        throw new Error(
          "The findings did not match this ledger. Nothing is shown.",
        );
      return r.data;
    },
    [
      ledgerId,
      filters.kind,
      filters.review,
      filters.version,
      filters.statement,
    ],
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
      {page.items.length ? (
        <div className="record-table-wrap">
          <table className="record-table">
            <caption className="sr-only">
              Detection findings for the selected ledger
            </caption>
            <thead>
              <tr>
                <th scope="col">Finding</th>
                <th scope="col">Inputs</th>
                <th scope="col">Review</th>
                <th scope="col">Detected</th>
                <th scope="col">
                  <span className="sr-only">Open</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {page.items.map((f) => (
                <tr key={f.id} aria-selected={selected === f.id}>
                  <td>
                    <strong>{kindLabel(f.kind)}</strong>
                    <span>{CLASS_LABELS[f.class] ?? f.class}</span>
                    <small>
                      Statement <code>{f.staged_document_id.slice(0, 8)}</code>{" "}
                      · detector {f.detector_version}
                    </small>
                  </td>
                  <td>
                    <Freshness value={f.freshness.status} />
                  </td>
                  <td>
                    <ReviewTag value={f.review.status as string} />
                  </td>
                  <td>
                    <time dateTime={f.created_at}>
                      {f.created_at.slice(0, 10)}
                    </time>
                  </td>
                  <td>
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={() => onSelect(f.id)}
                      aria-label={`Review evidence: ${kindLabel(f.kind)}`}
                    >
                      Review evidence
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
            No findings match these filters. A ledger with no findings may
            simply agree with its statements, or detection has not run yet.
          </RecordEmpty>
        )
      )}
      <PageActions page={page} label="findings" />
    </>
  );
}

function Freshness({ value }: { value: string }) {
  return value === "CURRENT" ? (
    <span className="record-status">Current inputs</span>
  ) : (
    <span className="record-status is-caution">Stale: inputs changed</span>
  );
}
function ReviewTag({ value }: { value: string }) {
  return (
    <span className={`record-status ${value === "OPEN" ? "is-caution" : ""}`}>
      {value === "OPEN"
        ? "Open"
        : value === "CONFIRMED"
          ? "Confirmed by reviewer"
          : value === "DISMISSED"
            ? "Dismissed by reviewer"
            : value}
    </span>
  );
}

function FindingDetail({
  id,
  ledgerId,
  config,
  tenantId,
  userId,
  csrfToken,
  canReview,
  onStatement,
  onChanged,
  onClose,
  onSessionRefresh,
}: {
  id: string;
  ledgerId: string;
  config: DetectionConfiguration;
  tenantId: string;
  userId: string;
  csrfToken: string;
  canReview: boolean;
  onStatement: (id: string) => void;
  onChanged: () => void;
  onClose: () => void;
  onSessionRefresh: () => void;
}) {
  const [finding, setFinding] = useState<Finding | null>(null),
    [error, setError] = useState(""),
    [reload, setReload] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError("");
    void (async () => {
      try {
        const r = await optionalApi.GET(
          "/api/v1/detection-findings/{finding_id}",
          {
            signal: controller.signal,
            params: { path: { finding_id: id } },
          },
        );
        if (controller.signal.aborted) return;
        if (!r.data) return setError(sourceError(r.response.status).message);
        if (r.data.id !== id || r.data.ledger_id !== ledgerId)
          return setError(
            "The finding did not match this ledger. Nothing is shown.",
          );
        setFinding(r.data);
      } catch {
        if (!controller.signal.aborted)
          setError("The finding could not be loaded.");
      }
    })();
    return () => controller.abort();
  }, [id, ledgerId, reload]);
  if (error)
    return (
      <ReadFailure
        message={error}
        retry={() => setReload((n) => n + 1)}
        refresh={onSessionRefresh}
      />
    );
  if (!finding)
    return (
      <div className="record-loading" role="status">
        Loading the finding and its evidence…
      </div>
    );
  const rule = config.rules.find((r) => r.kind === finding.kind);
  const stale = finding.freshness.status !== "CURRENT";
  return (
    <article
      className="record-detail-section detection-detail"
      aria-labelledby={`finding-${finding.id}`}
    >
      <div className="record-detail-heading">
        <FileSearch size={22} />
        <div>
          <h3 id={`finding-${finding.id}`}>{kindLabel(finding.kind)}</h3>
          <p>
            {CLASS_LABELS[finding.class] ?? finding.class} · detector{" "}
            {finding.detector} {finding.detector_version} · statement{" "}
            <code>{finding.staged_document_id}</code>
          </p>
        </div>
        <Button type="button" variant="ghost" onClick={onClose}>
          Close
        </Button>
      </div>
      {stale && (
        <div className="notice" role="note">
          <AlertTriangle size={18} />
          <span>
            <strong>Stale.</strong> The ledger records, statement or other
            claims this finding compared have changed since it was detected. The
            facts below are those compared at detection time. Run detection
            again to compare current records; a confirmation is not possible on
            stale inputs.
          </span>
        </div>
      )}
      <section aria-label="Rule" className="detection-panel">
        <h4>What the rule checks</h4>
        <p>{rule?.condition ?? "Rule description unavailable."}</p>
        <p className="small muted">
          Deterministic comparison · no model · not an accounting conclusion ·
          accounting UNREVIEWED
        </p>
      </section>
      <div className="detection-facts">
        <section aria-label="Statement observation" className="detection-panel">
          <h4>Statement says · observation</h4>
          <Value value={finding.observed} />
        </section>
        <section aria-label="Ledger records" className="detection-panel">
          <h4>Ledger records · at the run’s snapshot</h4>
          <Value value={finding.recorded} />
        </section>
      </div>
      <section aria-label="Computed difference" className="detection-panel">
        <h4>Computed by the rule</h4>
        {finding.kind === "PAYMENT_CLAIMED_BY_OTHER_PAYOUT" ? (
          <OverlapClaims
            computed={finding.computed}
            onStatement={onStatement}
          />
        ) : finding.kind === "PAYOUT_DATE_DIFFERS" ? (
          <DateDifference computed={finding.computed} />
        ) : (
          <Value value={finding.computed} />
        )}
      </section>
      <section aria-label="Evidence" className="detection-panel">
        <h4>Cited evidence</h4>
        <ul className="detection-evidence">
          {finding.evidence.map((e, i) => (
            <li key={i}>
              <span className="record-status">
                {String(
                  (e as Record<string, Json>).type ?? "EVIDENCE",
                ).replaceAll("_", " ")}
              </span>
              <Value
                value={Object.fromEntries(
                  Object.entries(e).filter(([k]) => k !== "type"),
                )}
              />
              {(e as Record<string, Json>).staged_document_id &&
              (e as Record<string, Json>).staged_document_id !==
                finding.staged_document_id ? (
                <Button
                  type="button"
                  variant="ghost"
                  onClick={() =>
                    onStatement(
                      String((e as Record<string, Json>).staged_document_id),
                    )
                  }
                >
                  Show findings for that statement
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
        <p className="small muted">
          Input fingerprint at detection{" "}
          <code>{finding.input_fingerprint}</code>
          <br />
          Current input fingerprint{" "}
          <code>{finding.freshness.current_input_fingerprint}</code>
        </p>
      </section>
      <section aria-label="Interpretation" className="detection-panel">
        <h4>Interpretation</h4>
        <p className="muted">
          Not available — no model is configured for this detector. Nothing here
          is a model output.
        </p>
      </section>
      <ReviewPanel
        finding={finding}
        tenantId={tenantId}
        userId={userId}
        csrfToken={csrfToken}
        canReview={canReview}
        onUpdated={(f) => {
          if (f) setFinding(f);
          else setReload((n) => n + 1);
          onChanged();
        }}
      />
      <section aria-label="Decision history" className="detection-panel">
        <h4>
          <History size={15} /> Decision history · immutable
        </h4>
        {finding.dispositions.length ? (
          <ol className="detection-history">
            {finding.dispositions.map((d) => (
              <li key={String((d as Record<string, Json>).id)}>
                <strong>{String((d as Record<string, Json>).action)}</strong> ·{" "}
                {String((d as Record<string, Json>).reason_code)
                  .replaceAll("_", " ")
                  .toLowerCase()}{" "}
                · inputs{" "}
                {String((d as Record<string, Json>).freshness).toLowerCase()} at
                the decision ·{" "}
                <time dateTime={String((d as Record<string, Json>).created_at)}>
                  {String((d as Record<string, Json>).created_at)
                    .replace("T", " ")
                    .slice(0, 19)}{" "}
                  UTC
                </time>
                {(d as Record<string, Json>).note ? (
                  <p className="record-text">
                    Reviewer note (untrusted text):{" "}
                    {String((d as Record<string, Json>).note)}
                  </p>
                ) : null}
              </li>
            ))}
          </ol>
        ) : (
          <p className="muted">
            No decision recorded yet. A new finding always starts open; earlier
            decisions are never carried over.
          </p>
        )}
      </section>
    </article>
  );
}

function OverlapClaims({
  computed,
  onStatement,
}: {
  computed: Record<string, Json>;
  onStatement: (id: string) => void;
}) {
  const claims = (computed.claims as Record<string, Json>[] | undefined) ?? [];
  const payout = computed.statement_payout as string[] | undefined;
  return (
    <>
      <p>
        <strong>Potential overlapping claim.</strong>{" "}
        {String(computed.basis ?? "")}
      </p>
      {claims.map((c) => (
        <div className="detection-claims" key={String(c.line_no)}>
          <div>
            <h5>This statement{payout ? ` · payout ${payout[1]}` : ""}</h5>
            <dl className="record-facts">
              <Fact label="Line">{String(c.line_no)}</Fact>
              <Fact label="Item reference">
                <code>{String(c.item_ref)}</code>
              </Fact>
              <Fact label="Statement amount">{String(c.statement_amount)}</Fact>
              <Fact label="Ledger payment">
                <code>{String(c.payment_document_id)}</code> v
                {String(c.payment_version)}
              </Fact>
              <Fact label="Ledger payment amount">
                {String(c.payment_amount)}
              </Fact>
            </dl>
          </div>
          <div>
            <h5>Other payouts listing the same payment</h5>
            {((c.other_claims as Record<string, Json>[]) ?? []).map((o) => (
              <dl className="record-facts" key={String(o.staged_document_id)}>
                <Fact label="Payout">{(o.payout as string[])?.[1]}</Fact>
                <Fact label="Line">{String(o.line_no)}</Fact>
                <Fact label="Statement amount">
                  {String(o.statement_amount)}
                </Fact>
                <Fact label="Source record SHA-256">
                  <code>{String(o.source_record_hash)}</code>
                </Fact>
                <Fact label="Statement">
                  <Button
                    type="button"
                    variant="ghost"
                    onClick={() => onStatement(String(o.staged_document_id))}
                  >
                    Show findings for statement{" "}
                    {String(o.staged_document_id).slice(0, 8)}
                  </Button>
                </Fact>
              </dl>
            ))}
            {((c.excluded_claims as Record<string, Json>[]) ?? []).length >
              0 && (
              <div className="detection-excluded">
                <h5>Not counted</h5>
                <ul>
                  {((c.excluded_claims as Record<string, Json>[]) ?? []).map(
                    (x) => (
                      <li key={String(x.staged_document_id)}>
                        Payout {(x.payout as string[])?.[1]}, line{" "}
                        {String(x.line_no)}: not counted because{" "}
                        {EXCLUDED_REASONS[String(x.reason)] ?? String(x.reason)}
                        .
                      </li>
                    ),
                  )}
                </ul>
              </div>
            )}
          </div>
        </div>
      ))}
    </>
  );
}

function DateDifference({ computed }: { computed: Record<string, Json> }) {
  const days = Number(computed.calendar_days_statement_minus_ledger);
  return (
    <>
      <dl className="record-facts">
        <Fact label="Statement payout date">
          <time dateTime={String(computed.statement_payout_date)}>
            {String(computed.statement_payout_date)}
          </time>
        </Fact>
        <Fact label="Recorded payout date">
          <time dateTime={String(computed.ledger_payout_date)}>
            {String(computed.ledger_payout_date)}
          </time>
        </Fact>
        <Fact label="Difference">
          <strong>{dayDifferenceText(days)}</strong> ({days > 0 ? "+" : ""}
          {days} calendar days, statement minus recorded)
        </Fact>
        <Fact label="Recorded settlement">
          <code>{String(computed.settlement_document_id)}</code> v
          {String(computed.settlement_version)}
        </Fact>
      </dl>
      <p className="small muted">
        {String(computed.date_semantics ?? "")}. A date difference can be
        legitimate (for example a processor value date); it is shown, not
        judged.
      </p>
    </>
  );
}

function Value({ value, depth = 0 }: { value: Json; depth?: number }) {
  if (value === null || value === undefined)
    return <span className="muted">none</span>;
  if (typeof value !== "object")
    return typeof value === "string" && /^[0-9a-f-]{32,}$/i.test(value) ? (
      <code>{value}</code>
    ) : (
      <span>{String(value)}</span>
    );
  if (depth > 2)
    return (
      <pre className="detection-json">{JSON.stringify(value, null, 2)}</pre>
    );
  if (Array.isArray(value))
    return value.length ? (
      <ol className="detection-list">
        {value.map((v, i) => (
          <li key={i}>
            <Value value={v} depth={depth + 1} />
          </li>
        ))}
      </ol>
    ) : (
      <span className="muted">none</span>
    );
  return (
    <dl className="record-facts">
      {Object.entries(value as Record<string, Json>).map(([k, v]) => (
        <Fact key={k} label={k.replaceAll("_", " ")}>
          <Value value={v} depth={depth + 1} />
        </Fact>
      ))}
    </dl>
  );
}

function ReviewPanel({
  finding,
  tenantId,
  userId,
  csrfToken,
  canReview,
  onUpdated,
}: {
  finding: Finding;
  tenantId: string;
  userId: string;
  csrfToken: string;
  canReview: boolean;
  onUpdated: (f: Finding | null) => void;
}) {
  const scope = { tenantId, userId, findingId: finding.id };
  const store = reviewStorageKey(scope);
  const [pending, setPending] = useState<PendingReview | null>(null),
    [reason, setReason] = useState<string>(DISMISS_REASONS[0][0]),
    [note, setNote] = useState(""),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState("");
  useEffect(() => {
    setPending(readPendingReview(storage()?.getItem(store) ?? null, scope));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [store]);
  const status = finding.review.status as string,
    etag = finding.review.etag as string;
  const actions = availableActions(status, finding.freshness.status, canReview);
  async function send(p: PendingReview) {
    setBusy(true);
    setMessage("");
    storage()?.setItem(store, JSON.stringify(p));
    setPending(p);
    try {
      const r = await optionalApi.POST(
        "/api/v1/detection-findings/{finding_id}/dispositions",
        {
          params: { path: { finding_id: finding.id } },
          headers: {
            "X-CSRF-Token": csrfToken,
            "Idempotency-Key": p.key,
            "If-Match": `"${p.etag}"`,
          },
          body: reviewBody(p) as never,
        },
      );
      const code = (r.error as { code?: string } | undefined)?.code;
      if (r.data) {
        storage()?.removeItem(store);
        setPending(null);
        setNote("");
        setMessage(
          r.response.headers.get("Idempotent-Replayed") === "true"
            ? "This decision had already been recorded."
            : "Decision recorded.",
        );
        onUpdated(r.data);
        return;
      }
      setMessage(reviewProblem(r.response.status, code));
      if (
        r.response.status !== 401 &&
        r.response.status !== 409 &&
        r.response.status < 500
      ) {
        storage()?.removeItem(store);
        setPending(null);
      }
      if (
        [409, 412].includes(r.response.status) &&
        code !== "REQUEST_IN_PROGRESS"
      ) {
        storage()?.removeItem(store);
        setPending(null);
        onUpdated(null);
      }
    } catch {
      setMessage(reviewProblem(0, undefined));
    } finally {
      setBusy(false);
    }
  }
  function start(action: ReviewAction, fixed: string | null) {
    if (!validNote(note))
      return setMessage("A note must be 1 to 2000 characters of visible text.");
    void send({
      schema: 1,
      ...scope,
      key: crypto.randomUUID(),
      etag,
      action,
      reason: fixed ?? reason,
      note,
      startedAt: Date.now(),
    });
  }
  return (
    <section
      aria-label="Reviewer conclusion"
      className="detection-panel detection-review-panel"
    >
      <h4>Reviewer conclusion</h4>
      <p>
        Current status: <ReviewTag value={status} />{" "}
        <span className="small muted">
          (review version {String(finding.review.version)})
        </span>
      </p>
      <p className="small muted">
        A decision records what a reviewer concluded after checking the cited
        evidence. It changes no ledger, payment, settlement, proposal or
        execution record.
      </p>
      {!canReview && (
        <p className="muted">
          Your role can read this finding but cannot record a decision.
        </p>
      )}
      {pending ? (
        <div className="notice" role="status">
          <Clock3 size={18} />
          <span>
            A {pending.action.toLowerCase()} decision may not have reached the
            server.{" "}
            <Button
              type="button"
              onClick={() => void send(pending)}
              disabled={busy}
            >
              Resend the same decision
            </Button>{" "}
            <Button
              type="button"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                storage()?.removeItem(store);
                setPending(null);
                onUpdated(null);
              }}
            >
              Discard and reload
            </Button>
          </span>
        </div>
      ) : (
        actions.length > 0 && (
          <form
            className="detection-review-form"
            onSubmit={(e) => {
              e.preventDefault();
            }}
          >
            {status === "OPEN" && (
              <div>
                <label htmlFor={`dismiss-reason-${finding.id}`}>
                  Reason if dismissed
                </label>
                <select
                  id={`dismiss-reason-${finding.id}`}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  disabled={busy}
                >
                  {DISMISS_REASONS.map(([v, label]) => (
                    <option key={v} value={v}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
            )}
            <div>
              <label htmlFor={`review-note-${finding.id}`}>
                Note (optional, stored as untrusted text)
              </label>
              <textarea
                id={`review-note-${finding.id}`}
                rows={3}
                maxLength={2000}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                disabled={busy}
              />
            </div>
            <div className="financial-actions">
              {actions.map((a) => (
                <Button
                  key={a.action}
                  type="button"
                  variant={a.action === "DISMISSED" ? "secondary" : "primary"}
                  disabled={busy || !a.enabled}
                  aria-describedby={a.why ? `why-${finding.id}` : undefined}
                  onClick={() => start(a.action, a.reason)}
                >
                  {a.action === "CONFIRMED"
                    ? "Confirm: verified against the evidence"
                    : a.action === "DISMISSED"
                      ? "Dismiss with the selected reason"
                      : "Reopen for correction"}
                </Button>
              ))}
            </div>
            {actions.some((a) => a.why) && (
              <p id={`why-${finding.id}`} className="small muted">
                {actions.find((a) => a.why)?.why}
              </p>
            )}
          </form>
        )
      )}
      {busy && <span role="status">Sending decision…</span>}
      {message && (
        <p className="small" role="alert">
          {message}
        </p>
      )}
    </section>
  );
}

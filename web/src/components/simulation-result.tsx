"use client";
import { useEffect, useRef, useState } from "react";
import {
  ArrowDownUp,
  FileCheck2,
  Fingerprint,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { api } from "@/lib/api";
import type { components } from "@/lib/api.generated";
import {
  record,
  records,
  resultLabel,
  text,
  problemCode,
  type Simulation,
} from "@/lib/proposal-workflow";
import { formatAmount } from "@/lib/money-format";
import { Button } from "./button";
function Json({ value }: { value: unknown }) {
  return (
    <pre className="simulation-json">{JSON.stringify(value, null, 2)}</pre>
  );
}
function Money({ value }: { value: unknown }) {
  const m = record(value);
  return (
    <span className="simulation-money">
      {typeof m.currency === "string" && typeof m.amount === "string"
        ? `${m.currency} ${formatAmount(m.amount, m.currency)}`
        : "Unavailable"}
    </span>
  );
}
const policyNames: Record<string, string> = {
  "P-ACC-01": "Account configuration",
  "P-APP-01": "Applications",
  "P-BAL-01": "Balanced entry",
  "P-CLR-01": "Included items clear",
  "P-CLR-02": "Clearing balance",
  "P-CUR-01": "Currency and scale",
  "P-DUP-01": "Duplicate protection",
  "P-EVD-01": "Required evidence",
  "P-FEE-01": "Statement fee",
  "P-PER-01": "Open accounting period",
  "P-REV-01": "Revenue boundary",
  "P-SNP-01": "Provider snapshot",
  "P-STL-01": "Statement agreement",
  "P-SUB-01": "Receivables control",
};
export function SimulationResult({
  id,
  proposalId,
  ledgerId,
  revision,
  csrfToken,
  focusOnLoad = false,
}: {
  id: string;
  proposalId: string;
  ledgerId: string;
  revision: number;
  csrfToken: string;
  focusOnLoad?: boolean;
}) {
  const [run, setRun] = useState<Simulation | null>(null),
    [error, setError] = useState(""),
    [reload, setReload] = useState(0),
    [replaying, setReplaying] = useState(false);
  const [replay, setReplay] = useState<
      components["schemas"]["ReplayResult"] | null
    >(null),
    [replayError, setReplayError] = useState("");
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (run && focusOnLoad) heading.current?.focus();
  }, [run, focusOnLoad]);
  const request = useRef<AbortController | null>(null),
    replayBusy = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    setRun(null);
    setError("");
    setReplay(null);
    setReplayError("");
    void (async () => {
      try {
        const r = await api.GET("/api/v1/simulations/{simulation_id}", {
          signal: controller.signal,
          params: { path: { simulation_id: id } },
        });
        if (controller.signal.aborted) return;
        if (
          !r.data ||
          r.data.id !== id ||
          r.data.proposal_id !== proposalId ||
          r.data.ledger_id !== ledgerId ||
          r.data.revision !== revision
        ) {
          setError(
            "This recorded result could not be verified for the selected proposal revision. No previous result is shown.",
          );
          return;
        }
        setRun(r.data);
      } catch {
        if (!controller.signal.aborted)
          setError(
            "The recorded simulation could not be reached. Retry or refresh your session.",
          );
      }
    })();
    return () => {
      controller.abort();
      request.current?.abort();
    };
  }, [id, proposalId, ledgerId, revision, reload]);
  async function doReplay() {
    if (!run || replayBusy.current) return;
    replayBusy.current = true;
    setReplaying(true);
    setReplay(null);
    setReplayError("");
    const controller = new AbortController();
    request.current = controller;
    try {
      const r = await api.POST("/api/v1/simulations/{simulation_id}/replay", {
        headers: { "X-CSRF-Token": csrfToken },
        signal: controller.signal,
        params: { path: { simulation_id: id } },
      });
      if (controller.signal.aborted) return;
      if (
        r.data &&
        r.data.simulation_id === id &&
        r.data.input_hash === run.input_hash &&
        r.data.original_hash === run.result_hash &&
        r.data.reloaded_current_state === false &&
        r.data.identical === (r.data.original_hash === r.data.replay_hash)
      )
        setReplay(r.data);
      else
        setReplayError(
          problemCode(r.error) === "SIMULATION_VERSION_UNAVAILABLE"
            ? "Replay unavailable: an exact pinned engine or policy version is not installed. The original result is preserved."
            : "Replay could not be verified. No identical result is claimed. Refresh your session or retry.",
        );
    } catch {
      if (!controller.signal.aborted)
        setReplayError(
          "Replay could not be reached. No new outcome was stored.",
        );
    } finally {
      replayBusy.current = false;
      if (!controller.signal.aborted) setReplaying(false);
    }
  }
  if (error)
    return (
      <section className="card simulation-error" role="alert">
        <ShieldAlert size={22} />
        <h3>Recorded result unavailable</h3>
        <p>{error}</p>
        <Button variant="secondary" onClick={() => setReload((n) => n + 1)}>
          Retry result
        </Button>
      </section>
    );
  if (!run)
    return (
      <section className="card financial-status" role="status">
        Loading pinned simulation result…
      </section>
    );
  const label = resultLabel(run),
    verdicts = records(run.outcome.verdicts),
    evaluated = record(run.outcome.verification).status === "EVALUATED";
  const counts = [
    "FAIL",
    "INSUFFICIENT_EVIDENCE",
    "PASS",
    "NOT_APPLICABLE",
  ].map((k) => ({
    key: k,
    count: verdicts.filter((v) => v.outcome === k).length,
  }));
  return (
    <section
      className="simulation-section"
      aria-label="Pinned simulation result"
    >
      <div className={`simulation-banner ${label.tone}`}>
        <ShieldAlert size={25} />
        <div>
          <span className="eyebrow">
            PINNED SIMULATION · REVISION {run.revision}
          </span>
          <h3 ref={heading} tabIndex={-1}>
            {label.title}
          </h3>
          <p>{label.description}</p>
        </div>
      </div>
      <p className="simulation-boundary">
        Prediction only · {run.data_label ?? "Data label unavailable"} ·
        Accounting {run.accounting_status}. The simulation itself made no
        financial changes. Stored results do not establish current eligibility.
      </p>
      <div className="simulation-pins">
        <div>
          <span>Ledger sequence</span>
          <strong>{run.snapshot.seq}</strong>
        </div>
        <div>
          <span>Simulator</span>
          <strong>{run.simulator_version}</strong>
        </div>
        <div>
          <span>Policy set</span>
          <strong>{run.snapshot.policy_set_version ?? "Not evaluated"}</strong>
        </div>
        <div>
          <span>Dependencies pinned</span>
          <strong>{run.read_set.length}</strong>
        </div>
      </div>
      <section className="card">
        <div className="card-heading">
          <FileCheck2 size={18} />
          <h3>Policy verdicts</h3>
        </div>
        <div className="card-content">
          {evaluated ? (
            <>
              <div className="verdict-counts">
                {counts.map((c) => (
                  <span key={c.key}>
                    <strong>{c.count}</strong>
                    {c.key.replaceAll("_", " ").toLowerCase()}
                  </span>
                ))}
              </div>
              <p className="muted small">
                Not applicable is separate from pass. Deferred checks and kernel
                diagnostics are not evaluated policy passes.
              </p>
            </>
          ) : (
            <p>
              No evaluated policy verdicts are available for this historical
              profile.
            </p>
          )}
          <div className="verdict-list">
            {verdicts.map((v, i) => (
              <details
                className={`verdict ${text(v.outcome).toLowerCase()}`}
                key={`${text(v.policy_id)}-${i}`}
              >
                <summary>
                  <span>
                    <strong>
                      {policyNames[text(v.policy_id)] ?? text(v.policy_id)}
                    </strong>
                    <small>
                      {text(v.policy_id)} · v{text(v.policy_version)}
                    </small>
                  </span>
                  <span className="verdict-outcome">
                    {text(v.outcome).replaceAll("_", " ")}
                  </span>
                </summary>
                <div className="verdict-body">
                  <p className="workflow-code">{text(v.explanation_code)}</p>
                  {v.explanation_code === "REQUIRED_EVIDENCE_NOT_SOURCE" && (
                    <p>
                      The required statement is synthetic. Matching values do
                      not turn it into source evidence.
                    </p>
                  )}
                  <h4>Recorded facts</h4>
                  <Json value={v.facts} />
                  <h4>Evidence considered</h4>
                  {records(v.evidence).length ? (
                    <ul className="simulation-evidence">
                      {records(v.evidence).map((e, j) => (
                        <li key={j}>
                          <strong>{text(e.kind)}</strong>
                          <code>{text(e.id)}</code>
                          {typeof e.provenance === "string" && (
                            <span>Provenance: {text(e.provenance)}</span>
                          )}
                          {typeof e.sha256 === "string" && (
                            <code>SHA-256 {text(e.sha256)}</code>
                          )}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="muted">
                      No evidence references recorded for this check.
                    </p>
                  )}
                </div>
              </details>
            ))}
          </div>
          <details className="record-technical">
            <summary>Evaluation scope and deferred checks</summary>
            <Json value={run.outcome.verification} />
          </details>
        </div>
      </section>
      <section className="card">
        <div className="card-heading">
          <ArrowDownUp size={18} />
          <h3>Predicted accounting impact</h3>
        </div>
        <div className="card-content">
          <p className="muted">
            Before and after are pinned simulation values. Consult the current
            proposal and execution receipt for committed effects.
          </p>
        </div>
        <div
          className="simulation-table-wrap"
          role="region"
          aria-label="Predicted account balances"
          tabIndex={0}
        >
          <table className="simulation-table">
            <caption>
              Predicted account balances · exact decimal amounts
            </caption>
            <thead>
              <tr>
                <th scope="col">Account / role</th>
                <th scope="col">Before</th>
                <th scope="col">Change</th>
                <th scope="col">After</th>
              </tr>
            </thead>
            <tbody>
              {records(run.outcome.account_deltas).map((a, i) => (
                <tr key={i}>
                  <th scope="row">
                    {text(a.account_code)}
                    <small>{text(a.role).replaceAll("_", " ")}</small>
                    {a.party_id ? <code>{text(a.party_id)}</code> : null}
                  </th>
                  <td>
                    <Money value={a.before} />
                  </td>
                  <td>
                    <Money value={a.delta} />
                  </td>
                  <td>
                    <Money value={a.after} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="card-content">
          <details className="record-technical">
            <summary>Predicted journal lines</summary>
            <ul className="predicted-lines">
              {records(run.outcome.predicted_postings).map((p, i) => (
                <li key={i}>
                  <span>
                    {text(p.account_code)} · {text(p.role).replaceAll("_", " ")}
                  </span>
                  <strong>
                    {p.side === "D"
                      ? "Debit"
                      : p.side === "C"
                        ? "Credit"
                        : "Unknown side"}{" "}
                    <Money value={p.amount} />
                  </strong>
                </li>
              ))}
            </ul>
          </details>
          <details className="record-technical">
            <summary>Predicted document changes</summary>
            <Json value={run.outcome.document_effects} />
          </details>
          <details className="record-technical">
            <summary>
              Kernel diagnostics · separate from policy verdicts
            </summary>
            <Json value={run.outcome.calculation} />
          </details>
          {run.status === "REJECTED" && <Json value={run.outcome.rejection} />}
        </div>
      </section>
      <section className="card">
        <div className="card-heading">
          <Fingerprint size={18} />
          <h3>Reproducibility & provenance</h3>
        </div>
        <div className="card-content">
          <p>
            Replay recomputes from stored inputs and pinned versions. It does
            not reload current ledger state, release reservations or authorize a
            write.
          </p>
          <div className="simulation-replay">
            <Button
              variant="secondary"
              disabled={replaying}
              onClick={() => void doReplay()}
            >
              <RefreshCw size={15} className={replaying ? "spin" : ""} />
              {replaying ? "Replaying…" : "Replay pinned result"}
            </Button>
            {replay && (
              <span
                className={replay.identical ? "replay-match" : "field-error"}
                role="status"
              >
                {replay.identical
                  ? "Identical result · hashes match"
                  : "Result differs · hashes do not match"}
              </span>
            )}
          </div>
          {replayError && (
            <p role="alert" className="notice error">
              {replayError}
            </p>
          )}
          {replay && (
            <details className="record-technical">
              <summary>Replay hashes</summary>
              <Json value={replay} />
            </details>
          )}
          <details className="record-technical">
            <summary>Snapshot, dependencies and version pins</summary>
            <Json
              value={{
                snapshot: run.snapshot,
                read_set: run.read_set,
                versions: run.versions,
              }}
            />
          </details>
          <details className="record-technical">
            <summary>Pinned source provenance</summary>
            <Json value={run.pinned_sources} />
          </details>
          <dl className="simulation-hashes">
            <div>
              <dt>Run</dt>
              <dd>
                <code>{run.id}</code>
              </dd>
            </div>
            <div>
              <dt>Input SHA-256</dt>
              <dd>
                <code>{run.input_hash}</code>
              </dd>
            </div>
            <div>
              <dt>Result SHA-256</dt>
              <dd>
                <code>{run.result_hash}</code>
              </dd>
            </div>
          </dl>
        </div>
      </section>
    </section>
  );
}

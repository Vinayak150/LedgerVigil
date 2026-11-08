"use client";
import { useCallback } from "react";
import { ShieldAlert } from "lucide-react";
import { api } from "@/lib/api";
import { sourceError, useSourcePage } from "@/lib/use-source-page";
import { PageActions, ReadFailure } from "./record-ui";

export function PolicyExceptions({
  proposalId,
  ledgerId,
  revision,
  simulationId,
}: {
  proposalId: string;
  ledgerId: string;
  revision: number;
  simulationId: string;
}) {
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await api.GET("/api/v1/proposals/{proposal_id}/reviews", {
        signal,
        params: {
          path: { proposal_id: proposalId },
          query: { limit: "10", ...(cursor ? { cursor } : {}) },
        },
      });
      if (!r.data) throw sourceError(r.response.status);
      if (
        r.data.proposal_id !== proposalId ||
        r.data.items.some(
          (item) =>
            item.proposal_id !== proposalId || item.ledger_id !== ledgerId,
        )
      )
        throw new Error(
          "Exception history did not match this proposal. Refresh to request a new view.",
        );
      return r.data;
    },
    [proposalId, ledgerId],
  );
  const page = useSourcePage(loader);
  if (!page.loading && !page.error && !page.items.length) return null;
  return (
    <section
      className="card policy-exceptions"
      aria-labelledby="exceptions-title"
    >
      <div className="card-heading">
        <ShieldAlert size={18} />
        <h3 id="exceptions-title" tabIndex={-1}>
          Recorded reviews and exceptions
        </h3>
        <span className="outline-tag">Immutable history</span>
      </div>
      <div className="card-content">
        <p className="muted small">
          Reviews and exceptions bind to a saved revision and simulation.
          Original verdicts stay unchanged. Approval and rejection are recorded
          workflow decisions; neither posts a settlement or establishes
          accounting accuracy.
        </p>
        {page.error ? (
          <ReadFailure message={page.error} retry={page.reload} />
        ) : (
          <>
            {page.items.map((item) => (
              <article className="policy-exception" key={item.id}>
                <h4>
                  {item.kind === "APPROVAL"
                    ? "Approval"
                    : item.kind === "REJECTION"
                      ? "Rejection"
                      : item.policy_id}{" "}
                  · revision {item.revision}
                </h4>
                <p className="small muted">
                  {item.revision === revision &&
                  item.simulation_id === simulationId
                    ? "Recorded for the displayed revision"
                    : "Historical revision record"}{" "}
                  ·{" "}
                  {item.kind === "APPROVAL"
                    ? "Recorded approval · no execution"
                    : item.kind === "REJECTION"
                      ? "Recorded rejection · terminal decision"
                      : "Original verdict: INSUFFICIENT EVIDENCE"}
                </p>
                <p className="policy-exception-reason">
                  {item.kind !== "INSUFFICIENT_EVIDENCE_OVERRIDE"
                    ? (item.comment ?? "No approval comment recorded.")
                    : item.reason}
                </p>
                <details className="record-technical">
                  <summary>
                    {item.kind !== "INSUFFICIENT_EVIDENCE_OVERRIDE"
                      ? "Review attribution and freshness"
                      : "Exception attribution and dependencies"}
                  </summary>
                  <p>
                    {item.kind !== "INSUFFICIENT_EVIDENCE_OVERRIDE"
                      ? "Reviewer"
                      : "Controller"}{" "}
                    <code>
                      {item.kind === "APPROVAL"
                        ? item.approved_by
                        : item.kind === "REJECTION"
                          ? item.rejected_by
                          : item.overridden_by}
                    </code>
                  </p>
                  <p>
                    Recorded at{" "}
                    <time dateTime={item.created_at}>{item.created_at}</time>
                  </p>
                  <p>
                    Simulation <code>{item.simulation_id}</code>
                  </p>
                  <p>
                    {item.kind === "INSUFFICIENT_EVIDENCE_OVERRIDE" && (
                      <>Policy version {item.policy_version} · </>
                    )}
                    Proposal version {item.observed_version} →{" "}
                    {item.resulting_version}
                  </p>
                  <p>
                    {item.kind !== "INSUFFICIENT_EVIDENCE_OVERRIDE" ? (
                      <>
                        Independent review and freshness recorded under check{" "}
                        <code>{item.check_id}</code>. It is not a current
                        eligibility guarantee.
                      </>
                    ) : (
                      <>
                        Dependency check: {String(item.freshness.status)} at
                        recording time.
                      </>
                    )}
                  </p>
                  <code>{item.id}</code>
                </details>
              </article>
            ))}
            <PageActions page={page} label="reviews" />
          </>
        )}
      </div>
    </section>
  );
}

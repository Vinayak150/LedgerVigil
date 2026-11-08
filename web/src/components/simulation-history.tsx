"use client";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { Simulation } from "@/lib/proposal-workflow";
import { Button } from "./button";
import { SimulationResult } from "./simulation-result";

export function SimulationHistory(props: {
  id: string;
  proposalId: string;
  ledgerId: string;
  revision: number;
  csrfToken: string;
  focusOnLoad?: boolean;
}) {
  const [open, setOpen] = useState(false),
    [cursor, setCursor] = useState<string | null>(null);
  const [page, setPage] = useState<{
    items: Simulation[];
    next: string | null;
  } | null>(null);
  const [error, setError] = useState(""),
    [retry, setRetry] = useState(0);
  const [selected, setSelected] = useState({
    id: props.id,
    revision: props.revision,
  });
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setPage(null);
    setError("");
    void (async () => {
      try {
        const r = await api.GET("/api/v1/proposals/{proposal_id}/simulations", {
          signal: controller.signal,
          params: {
            path: { proposal_id: props.proposalId },
            query: { limit: "5", ...(cursor ? { cursor } : {}) },
          },
        });
        if (controller.signal.aborted) return;
        if (
          !r.data ||
          r.data.proposal_id !== props.proposalId ||
          r.data.items.some(
            (run) =>
              run.proposal_id !== props.proposalId ||
              run.ledger_id !== props.ledgerId,
          )
        ) {
          setError(
            "Recorded history could not be verified for this proposal. No previous page is shown.",
          );
          return;
        }
        setPage({ items: r.data.items, next: r.data.next_cursor });
      } catch {
        if (!controller.signal.aborted)
          setError("Simulation history could not be reached. Retry this page.");
      }
    })();
    return () => controller.abort();
  }, [open, cursor, retry, props.proposalId, props.ledgerId]);
  return (
    <>
      <section className="card" aria-label="Recorded simulation history">
        <div className="card-heading">
          <h3 ref={heading} tabIndex={-1}>
            Recorded simulations
          </h3>
          <Button
            variant="secondary"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
          >
            {open ? "Hide history" : "Browse history"}
          </Button>
        </div>
        {open && (
          <div className="editor-body">
            <p className="muted">
              Immutable runs on pinned inputs. Historical results do not
              establish current freshness or authorize posting.
            </p>
            {error ? (
              <div role="alert">
                {error}{" "}
                <Button
                  variant="secondary"
                  onClick={() => setRetry((value) => value + 1)}
                >
                  Retry history
                </Button>
              </div>
            ) : !page ? (
              <p role="status">Loading recorded runs…</p>
            ) : (
              <>
                {page.items.length === 0 && <p>No recorded runs.</p>}
                <ul>
                  {page.items.map((run) => (
                    <li key={run.id}>
                      <Button
                        variant="secondary"
                        aria-pressed={selected.id === run.id}
                        onClick={() =>
                          setSelected({ id: run.id, revision: run.revision })
                        }
                      >
                        Inspect revision {run.revision} ·{" "}
                        {run.id === props.id
                          ? "latest recorded run"
                          : "historical run"}
                      </Button>
                    </li>
                  ))}
                </ul>
                {cursor && (
                  <Button
                    variant="secondary"
                    onClick={() => {
                      setCursor(null);
                      heading.current?.focus();
                    }}
                  >
                    Newest runs
                  </Button>
                )}
                {page.next && (
                  <Button
                    variant="secondary"
                    onClick={() => {
                      setCursor(page.next);
                      heading.current?.focus();
                    }}
                  >
                    Older runs
                  </Button>
                )}
              </>
            )}
          </div>
        )}
      </section>
      {selected.id !== props.id && (
        <div className="notice" role="status">
          Showing historical simulation for revision {selected.revision}.{" "}
          <Button
            variant="secondary"
            onClick={() =>
              setSelected({ id: props.id, revision: props.revision })
            }
          >
            Return to latest run
          </Button>
        </div>
      )}
      <SimulationResult
        {...props}
        key={selected.id}
        id={selected.id}
        focusOnLoad={props.focusOnLoad && selected.id === props.id}
        revision={selected.revision}
      />
    </>
  );
}

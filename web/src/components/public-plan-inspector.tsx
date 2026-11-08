"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { X, FileSearch } from "lucide-react";
import { api } from "@/lib/api";
import {
  assertGroup,
  groupLabel,
  planError,
  SOURCE_COLUMNS,
  type PublicGroup,
} from "@/lib/public-plan";
import { useSourcePage } from "@/lib/use-source-page";
import { Button } from "./button";
import { Fact, ReadFailure, PageActions, ReferenceFacts } from "./record-ui";
export function PublicPlanInspector({
  registrationId,
  groupId,
  onClose,
  onSessionRefresh,
}: {
  registrationId: string;
  groupId: string;
  onClose: () => void;
  onSessionRefresh: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null),
    title = useRef<HTMLHeadingElement>(null);
  const [version, setVersion] = useState(0);
  useEffect(() => {
    const previous =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    const element = dialog.current;
    element?.showModal();
    title.current?.focus();
    return () => {
      element?.close();
      requestAnimationFrame(() => {
        if (previous?.isConnected) previous.focus();
      });
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className="draft-editor-dialog record-inspector public-plan-inspector"
      aria-labelledby="public-invoice-title"
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        const controls = Array.from(
          event.currentTarget.querySelectorAll<HTMLElement>(
            'button:not([disabled]), summary, a[href], [tabindex="0"]',
          ),
        ).filter(
          (element) =>
            element.getClientRects().length &&
            getComputedStyle(element).visibility !== "hidden",
        );
        const first = controls[0],
          last = controls.at(-1);
        if (
          event.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === title.current)
        ) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }}
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
    >
      <header className="editor-header">
        <div>
          <div className="eyebrow">ORIGINAL SOURCE EVIDENCE</div>
          <h2 id="public-invoice-title" ref={title} tabIndex={-1}>
            Invoice observation
          </h2>
          <p>Read-only · no financial actions</p>
        </div>
        <Button
          variant="ghost"
          aria-label="Close invoice inspection"
          onClick={onClose}
        >
          <X size={20} />
        </Button>
      </header>
      <div className="record-inspector-body">
        <GroupRead
          key={version}
          registrationId={registrationId}
          groupId={groupId}
          retry={() => setVersion((n) => n + 1)}
          onSessionRefresh={onSessionRefresh}
        />
      </div>
      <footer className="editor-footer">
        <span>Public observations · Posting UNAVAILABLE</span>
        <Button variant="secondary" onClick={onClose}>
          Close inspection
        </Button>
      </footer>
    </dialog>
  );
}
function GroupRead({
  registrationId,
  groupId,
  retry,
  onSessionRefresh,
}: {
  registrationId: string;
  groupId: string;
  retry: () => void;
  onSessionRefresh: () => void;
}) {
  const [group, setGroup] = useState<PublicGroup | null>(null),
    [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const r = await api.GET(
          "/api/v1/source-plan-registrations/{registration_id}/groups/{group_id}",
          {
            signal: controller.signal,
            params: {
              path: { registration_id: registrationId, group_id: groupId },
            },
          },
        );
        if (controller.signal.aborted) return;
        if (!r.data) throw planError(r.response.status);
        assertGroup(r.data);
        if (r.data.group_id !== groupId)
          throw new Error(
            "The source group did not match the selection. No rows are shown.",
          );
        setGroup(r.data);
      } catch (e) {
        if (!controller.signal.aborted)
          setError(e instanceof Error ? e.message : "Invoice unavailable.");
      }
    })();
    return () => controller.abort();
  }, [registrationId, groupId]);
  if (error)
    return (
      <ReadFailure message={error} retry={retry} refresh={onSessionRefresh} />
    );
  if (!group)
    return (
      <p className="record-loading" role="status">
        Loading invoice observation…
      </p>
    );
  return (
    <>
      <div className="record-detail-heading">
        <FileSearch size={25} />
        <div>
          <h3>{groupLabel(group)}</h3>
          <p>
            {group.source_line_count.toLocaleString("en-GB")} original source
            rows · Accounting UNREVIEWED
          </p>
        </div>
        <span
          className={`public-plan-status ${group.status === "HOLD" ? "is-held" : ""}`}
        >
          {group.status === "HOLD" ? "HOLD" : "CANDIDATE REVIEW"}
        </span>
      </div>
      <section
        className="public-plan-holds"
        aria-labelledby="hold-reasons-title"
      >
        <h4 id="hold-reasons-title">
          {group.hold_reasons.length
            ? "Why this invoice is held"
            : "No recorded holds — review still required"}
        </h4>
        {group.hold_reasons.length ? (
          <ul>
            {group.hold_reasons.map((reason) => (
              <li key={reason}>
                <code>{reason}</code>
              </li>
            ))}
          </ul>
        ) : (
          <p>
            Passing the observation rules does not establish accounting
            correctness or permission to post.
          </p>
        )}
      </section>
      <dl className="record-facts">
        <Fact label="Observation amount">
          {group.amount ? (
            <>
              <code>
                {group.amount.currency} {group.amount.amount}
              </code>
              <p className="small muted">
                Exact source-derived candidate amount · unreviewed
              </p>
            </>
          ) : (
            "Unavailable — held source values have not been rounded or repaired"
          )}
        </Fact>
        <Fact label="Posting eligibility">UNAVAILABLE</Fact>
      </dl>
      <details className="record-technical">
        <summary>Group identity and recorded payload hash</summary>
        <ReferenceFacts id={group.group_id} hash={group.payload_sha256} />
        <p>
          Hashes identify the recorded source payload; they do not certify the
          invoice or its accounting treatment.
        </p>
      </details>
      <OriginalRows
        registrationId={registrationId}
        groupId={groupId}
        expectedCount={group.source_line_count}
        onSessionRefresh={onSessionRefresh}
      />
    </>
  );
}
function OriginalRows({
  registrationId,
  groupId,
  expectedCount,
  onSessionRefresh,
}: {
  registrationId: string;
  groupId: string;
  expectedCount: number;
  onSessionRefresh: () => void;
}) {
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await api.GET(
        "/api/v1/source-plan-registrations/{registration_id}/groups/{group_id}/rows",
        {
          signal,
          params: {
            path: { registration_id: registrationId, group_id: groupId },
            query: { limit: "10", ...(cursor ? { cursor } : {}) },
          },
        },
      );
      if (!r.data) throw planError(r.response.status);
      if (
        r.data.registration_id !== registrationId ||
        r.data.group_id !== groupId ||
        r.data.posting_eligibility !== "UNAVAILABLE" ||
        r.data.items.some(
          (row) => row.raw.values.length !== SOURCE_COLUMNS.length,
        )
      )
        throw new Error(
          "Source row scope could not be verified. No rows are shown.",
        );
      return {
        ...r.data,
        items: r.data.items.map((row) => ({
          ...row,
          id: `${row.sheet}:${row.row}`,
        })),
      };
    },
    [registrationId, groupId],
  );
  const page = useSourcePage(loader);
  return (
    <section
      className="record-detail-section"
      aria-labelledby="original-rows-title"
    >
      <h4 id="original-rows-title">Original source rows</h4>
      <p className="record-source-label">
        Source text, exactly as recorded. Quantities, dates and prices are not
        normalized or converted. Repeated content stays in its original row.
      </p>
      {page.error ? (
        <ReadFailure
          message={page.error}
          retry={page.reload}
          refresh={onSessionRefresh}
        />
      ) : (
        <>
          <p className="small muted" aria-live="polite">
            {page.items.length} of {expectedCount.toLocaleString("en-GB")}{" "}
            source rows loaded
          </p>
          {page.items.map((row) => (
            <article className="public-plan-row" key={row.id}>
              <h5>
                {row.sheet} · row {row.row}
              </h5>
              <dl>
                {SOURCE_COLUMNS.map((label, i) => (
                  <Fact key={label} label={label}>
                    {row.raw.values[i] === null ? (
                      <span className="muted">Blank source cell</span>
                    ) : row.raw.values[i] === "" ? (
                      <span className="muted">Empty source text</span>
                    ) : (
                      <code>{row.raw.values[i]}</code>
                    )}
                  </Fact>
                ))}
              </dl>
              {row.raw.cell_issues.length > 0 && (
                <p role="note">
                  Recorded cell issues: {row.raw.cell_issues.join(", ")}
                </p>
              )}
              <details className="record-technical">
                <summary>Recorded row SHA-256</summary>
                <code>{row.sha256}</code>
              </details>
            </article>
          ))}
          {!page.loading &&
            !page.cursor &&
            page.items.length !== expectedCount && (
              <p role="alert">
                The returned row count differs from this group’s source count.
                Refresh the inspection before relying on this view.
              </p>
            )}
          <PageActions page={page} label="source rows" />
        </>
      )}
    </section>
  );
}

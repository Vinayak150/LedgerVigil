"use client";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { AlertCircle, LoaderCircle, LockKeyhole, Save, X } from "lucide-react";
import { api } from "@/lib/api";
import {
  editableContent,
  editChanged,
  editError,
  fieldIssues,
  validAmount,
  issueText,
  issueLabel,
  type Proposal,
  type EditableContent,
  type FieldIssue,
} from "@/lib/draft-edit";
import { formatAmount } from "@/lib/money-format";
import { Button } from "./button";
import { DraftReferenceEditor } from "./draft-reference-editor";
import { patchContent } from "@/lib/draft-references";

type Latest = { proposal: Proposal; etag: string };
export function DraftEditor({
  proposal,
  etag,
  csrfToken,
  onClose,
  onSaved,
}: {
  proposal: Proposal;
  etag: string;
  csrfToken: string;
  onClose: () => void;
  onSaved: (p: Proposal) => void;
}) {
  const [base, setBase] = useState<Latest>({ proposal, etag }),
    [content, setContent] = useState<EditableContent>(() =>
      editableContent(proposal),
    );
  const [referenceEdit, setReferenceEdit] = useState<number | null>(null),
    [referenceDirty, setReferenceDirty] = useState(false),
    [referenceCancel, setReferenceCancel] = useState(0),
    [referenceNotice, setReferenceNotice] = useState("");
  const [pending, setPending] = useState(false),
    [checking, setChecking] = useState(false),
    [error, setError] = useState(""),
    [issues, setIssues] = useState<FieldIssue[]>([]),
    [discard, setDiscard] = useState(false);
  const [conflict, setConflict] = useState<Latest | null>(null),
    [uncertain, setUncertain] = useState(false),
    [checked, setChecked] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null),
    errorFocus = useRef<HTMLDivElement>(null),
    discardFocus = useRef<HTMLDivElement>(null),
    returnFocus = useRef<HTMLElement | null>(null),
    request = useRef<AbortController | null>(null),
    saving = useRef(false);
  const mayEdit =
    base.proposal.state === "DRAFT" &&
    base.proposal.allowed_actions.includes("EDIT");
  const dirty = editChanged(editableContent(base.proposal), content),
    busy = pending || checking;
  useEffect(() => {
    dialog.current?.showModal();
    return () => {
      request.current?.abort();
      dialog.current?.close();
    };
  }, []);
  useEffect(() => {
    if (!dirty && !pending && !referenceDirty) return;
    const before = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", before);
    return () => window.removeEventListener("beforeunload", before);
  }, [dirty, pending, referenceDirty]);
  function close() {
    if (busy) return;
    if (referenceEdit !== null) {
      setReferenceCancel((n) => n + 1);
      return;
    }
    if (dirty || uncertain) {
      returnFocus.current =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
      setDiscard(true);
      requestAnimationFrame(() => discardFocus.current?.focus());
    } else onClose();
  }
  function announce(message: string) {
    setError(message);
    requestAnimationFrame(() => errorFocus.current?.focus());
  }
  function updateOperation(
    index: number,
    field:
      "processor_ref" | "payout_id" | "payout_date" | "gross" | "fee" | "net",
    value: string,
  ) {
    if (uncertain) return;
    setContent((old) => ({
      ...old,
      operations: old.operations.map((op, i) =>
        i !== index
          ? op
          : {
              ...op,
              [field]: ["gross", "fee", "net"].includes(field)
                ? { ...op[field as "gross" | "fee" | "net"], amount: value }
                : value,
            },
      ),
    }));
    setChecked(false);
  }
  function issueFor(pointer: string) {
    return issues.find((i) => i.pointer === pointer);
  }
  async function checkLatest() {
    if (busy) return;
    setChecking(true);
    setChecked(false);
    setError("");
    const controller = new AbortController();
    request.current = controller;
    try {
      const r = await api.GET("/api/v1/proposals/{proposal_id}", {
        signal: controller.signal,
        params: { path: { proposal_id: proposal.id } },
      });
      if (controller.signal.aborted) return;
      const next = r.response.headers.get("ETag");
      if (!r.data || r.data.id !== proposal.id || !next) {
        announce(
          "The latest draft could not be loaded. Your changes are still kept here.",
        );
        return;
      }
      if (r.data.version === base.proposal.version && next === base.etag) {
        setChecked(true);
        setConflict(null);
        announce(
          "The latest read still shows your original version. A timed-out save may still be running; retrying the same changes uses the original version check.",
        );
      } else {
        setConflict({ proposal: r.data, etag: next });
        announce(
          `Latest draft is revision ${r.data.revision}. Your unsaved changes are kept below. Load the latest version to start from its content.`,
        );
      }
    } catch {
      if (!controller.signal.aborted)
        announce(
          "We couldn’t check the latest draft. Reconnect and retry the check.",
        );
    } finally {
      if (!controller.signal.aborted) setChecking(false);
    }
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (
      saving.current ||
      busy ||
      !mayEdit ||
      referenceEdit !== null ||
      discard ||
      conflict ||
      !dirty ||
      (!checked && uncertain)
    )
      return;
    const bad: FieldIssue[] = [];
    content.operations.forEach((op, i) => {
      for (const field of ["gross", "fee", "net"] as const)
        if (!validAmount(op[field].amount))
          bad.push({
            pointer: `/operations/${i}/${field}/amount`,
            code: "Use an unsigned decimal amount with at most 16 whole-number digits and 4 decimal places.",
          });
    });
    if (bad.length) {
      setIssues(bad);
      announce("Review the highlighted amount fields.");
      return;
    }
    let patch;
    try {
      patch = patchContent(content);
    } catch (e) {
      announce(e instanceof Error ? e.message : "Review the draft references.");
      return;
    }
    saving.current = true;
    setPending(true);
    setError("");
    setIssues([]);
    setChecked(false);
    const controller = new AbortController();
    request.current = controller;
    try {
      const r = await api.PATCH("/api/v1/proposals/{proposal_id}", {
        signal: controller.signal,
        params: { path: { proposal_id: proposal.id } },
        headers: { "If-Match": base.etag, "X-CSRF-Token": csrfToken },
        body: patch,
      });
      if (controller.signal.aborted) return;
      if (
        r.data &&
        r.data.id === proposal.id &&
        r.data.version > base.proposal.version
      ) {
        onSaved(r.data);
        return;
      }
      setIssues(fieldIssues(r.error));
      if (r.response.status === 412) {
        setUncertain(true);
        announce(editError(412));
      } else if (r.response.status >= 500 || r.response.ok) {
        setUncertain(true);
        announce(editError(500));
      } else announce(editError(r.response.status));
    } catch {
      if (!controller.signal.aborted) {
        setUncertain(true);
        announce(
          "The connection ended before the save was confirmed. Your changes are kept here. Check the latest draft before retrying.",
        );
      }
    } finally {
      saving.current = false;
      if (!controller.signal.aborted) setPending(false);
    }
  }
  function loadLatest() {
    if (!conflict) return;
    setReferenceNotice("");
    setBase(conflict);
    setContent(editableContent(conflict.proposal));
    setConflict(null);
    setUncertain(false);
    setChecked(false);
    setError("");
    setIssues([]);
  }
  return (
    <dialog
      ref={dialog}
      className="draft-editor-dialog"
      aria-labelledby="draft-editor-title"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
    >
      <form onSubmit={save}>
        <header className="editor-header">
          <div>
            <span className="eyebrow">DRAFT REVISION</span>
            <h2 id="draft-editor-title">
              {referenceEdit === null
                ? "Edit settlement draft"
                : "Choose draft references"}
            </h2>
            <p>
              Based on revision {base.proposal.revision} · version{" "}
              {base.proposal.version}
            </p>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label={
              referenceEdit === null
                ? "Close draft editor"
                : "Close reference selection"
            }
            disabled={busy}
            onClick={close}
          >
            <X size={20} />
          </button>
        </header>
        {referenceEdit !== null ? (
          <DraftReferenceEditor
            key={referenceEdit}
            content={content}
            index={referenceEdit}
            ledgerId={base.proposal.ledger_id}
            metadata={base.proposal.evidence_refs}
            cancelSignal={referenceCancel}
            onDirtyChange={setReferenceDirty}
            onCancel={() => {
              const index = referenceEdit;
              setReferenceEdit(null);
              setReferenceDirty(false);
              requestAnimationFrame(() =>
                document.getElementById(`change-refs-${index}`)?.focus(),
              );
            }}
            onApply={(next) => {
              const index = referenceEdit;
              setContent(next);
              setReferenceEdit(null);
              setReferenceDirty(false);
              setChecked(false);
              setIssues([]);
              setError("");
              setReferenceNotice(
                "Reference changes are local. Save a draft revision to record them.",
              );
              requestAnimationFrame(() =>
                document.getElementById(`change-refs-${index}`)?.focus(),
              );
            }}
          />
        ) : (
          <>
            <div className="editor-body">
              <div className="notice">
                <LockKeyhole size={18} />
                <span>
                  Saving creates a new draft revision. It does not submit,
                  approve or post a settlement.
                </span>
              </div>
              {referenceNotice && (
                <div className="notice" role="status">
                  {referenceNotice}
                </div>
              )}
              {error && (
                <div
                  className="editor-error"
                  role="alert"
                  ref={errorFocus}
                  tabIndex={-1}
                >
                  <AlertCircle size={18} />
                  <div>
                    <p>{error}</p>
                    {uncertain && (
                      <Button
                        type="button"
                        variant="secondary"
                        disabled={busy}
                        onClick={() => void checkLatest()}
                      >
                        {checking ? "Checking…" : "Check latest draft"}
                      </Button>
                    )}
                  </div>
                </div>
              )}
              {conflict && (
                <section className="editor-conflict">
                  <h3>
                    Latest saved version · revision {conflict.proposal.revision}
                  </h3>
                  <p>Latest rationale</p>
                  <blockquote>
                    {conflict.proposal.rationale || "No rationale provided."}
                  </blockquote>
                  <p>
                    Your edits remain in the form below. Loading the latest
                    replaces those edits; nothing is merged or saved
                    automatically.
                  </p>
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={loadLatest}
                    disabled={busy}
                  >
                    Replace my edits with latest
                  </Button>
                </section>
              )}
              {!mayEdit && (
                <div className="notice error" role="alert">
                  Your latest draft permissions do not allow editing.
                </div>
              )}
              <fieldset
                disabled={busy || uncertain || !mayEdit}
                className="editor-fields"
              >
                <label className="editor-label" htmlFor="draft-rationale">
                  Rationale<span>Explain what is proposed and why.</span>
                </label>
                <textarea
                  id="draft-rationale"
                  value={content.rationale ?? ""}
                  maxLength={4000}
                  rows={4}
                  onChange={(event) => {
                    setContent((old) => ({
                      ...old,
                      rationale: event.target.value || null,
                    }));
                    setChecked(false);
                  }}
                />
                <div className="editor-count">
                  {(content.rationale ?? "").length} / 4,000
                </div>
                {content.operations.map((op, index) => (
                  <section className="editor-operation" key={index}>
                    <h3>Operation {index + 1} · Record settlement</h3>
                    <div className="editor-field-grid">
                      {(
                        [
                          ["processor_ref", "Processor reference"],
                          ["payout_id", "Payout reference"],
                          ["payout_date", "Payout date"],
                        ] as const
                      ).map(([field, label]) => (
                        <div key={field}>
                          <label htmlFor={`op-${index}-${field}`}>
                            {label}
                          </label>
                          <input
                            id={`op-${index}-${field}`}
                            type={field === "payout_date" ? "date" : "text"}
                            maxLength={field === "payout_date" ? undefined : 64}
                            required
                            value={op[field]}
                            pattern={
                              field === "payout_date"
                                ? undefined
                                : "[A-Za-z0-9_.:\\-]{1,64}"
                            }
                            onChange={(event) =>
                              updateOperation(index, field, event.target.value)
                            }
                            aria-invalid={
                              !!issueFor(`/operations/${index}/${field}`)
                            }
                          />
                          {issueFor(`/operations/${index}/${field}`) && (
                            <p className="field-error">
                              {issueText(
                                issueFor(`/operations/${index}/${field}`)
                                  ?.code ?? "",
                              )}
                            </p>
                          )}
                        </div>
                      ))}
                    </div>
                    <div className="editor-money-grid">
                      {(
                        [
                          ["gross", "Proposed gross"],
                          ["fee", "Proposed fee"],
                          ["net", "Proposed net"],
                        ] as const
                      ).map(([field, label]) => {
                        const pointer = `/operations/${index}/${field}/amount`,
                          issue = issueFor(pointer);
                        return (
                          <div key={field}>
                            <label htmlFor={`op-${index}-${field}`}>
                              {label} · {op[field].currency}
                            </label>
                            <input
                              id={`op-${index}-${field}`}
                              type="text"
                              inputMode="decimal"
                              required
                              value={op[field].amount}
                              onChange={(event) =>
                                updateOperation(
                                  index,
                                  field,
                                  event.target.value,
                                )
                              }
                              aria-invalid={!!issue}
                              aria-describedby={
                                issue ? `issue-${index}-${field}` : undefined
                              }
                            />
                            {issue && (
                              <p
                                className="field-error"
                                id={`issue-${index}-${field}`}
                              >
                                {issueText(issue.code)}
                              </p>
                            )}
                          </div>
                        );
                      })}
                    </div>
                    <div className="editor-fixed-refs">
                      <h4>Draft references</h4>
                      <p>
                        Change this operation’s statement and payment
                        references, and choose evidence for the draft. Changes
                        are recorded only when you save this revision.
                      </p>
                      <Button
                        id={`change-refs-${index}`}
                        type="button"
                        variant="secondary"
                        onClick={() => {
                          setReferenceEdit(index);
                          setReferenceDirty(false);
                        }}
                      >
                        Change source references
                      </Button>
                      <p className="small">
                        Statement <code>{op.statement_document_id}</code> ·{" "}
                        {content.evidence_refs.length} evidence references
                        across the draft
                      </p>
                      <ul>
                        {op.items.map((item) => (
                          <li key={item.document_id}>
                            <code>{item.document_id}</code>
                            <span>
                              {item.amount.currency}{" "}
                              {formatAmount(
                                item.amount.amount,
                                item.amount.currency,
                              )}
                            </span>
                          </li>
                        ))}
                      </ul>
                      <p className="small">
                        Amounts are proposed inputs. Totals, settlement
                        eligibility and period status are checked by future
                        submission/simulation workflows.
                      </p>
                    </div>
                  </section>
                ))}
              </fieldset>
              {issues.some(
                (i) =>
                  !i.pointer.match(
                    /^\/operations\/\d+\/(gross|fee|net)\/amount$/,
                  ),
              ) && (
                <ul className="editor-issues">
                  {issues.map((issue, index) => (
                    <li key={index}>
                      <strong>{issueLabel(issue.pointer)}</strong>{" "}
                      {issueText(issue.code)}
                    </li>
                  ))}
                </ul>
              )}
              {discard && (
                <div
                  className="editor-discard"
                  role="alert"
                  ref={discardFocus}
                  tabIndex={-1}
                >
                  <strong>Discard changes and close?</strong>
                  <p>
                    {uncertain
                      ? "A save may have reached the server. Closing discards only these local edits; reopen the draft to check its latest saved version."
                      : "These unsaved changes will be lost."}
                  </p>
                  <div>
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={() => {
                        setDiscard(false);
                        requestAnimationFrame(() =>
                          returnFocus.current?.focus(),
                        );
                      }}
                    >
                      Keep editing
                    </Button>
                    <Button type="button" onClick={onClose}>
                      Discard and close
                    </Button>
                  </div>
                </div>
              )}
            </div>
            <footer className="editor-footer">
              <span>
                {pending ? (
                  <>
                    <LoaderCircle size={15} className="spin" />
                    Saving revision…
                  </>
                ) : dirty ? (
                  "Unsaved changes"
                ) : (
                  "No changes"
                )}
              </span>
              <div>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy}
                  onClick={close}
                >
                  Cancel
                </Button>
                <Button
                  type="submit"
                  disabled={
                    busy ||
                    !mayEdit ||
                    !dirty ||
                    !!conflict ||
                    discard ||
                    (uncertain && !checked)
                  }
                >
                  <Save size={16} />
                  {uncertain ? "Retry same save" : "Save draft revision"}
                </Button>
              </div>
            </footer>
          </>
        )}
      </form>
    </dialog>
  );
}

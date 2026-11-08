"use client";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { MessageSquare, Send } from "lucide-react";
import { api } from "@/lib/api";
import {
  commentStorageKey,
  readCommentRecovery,
  type CommentScope,
  type CommentRecovery,
} from "@/lib/comment-recovery";
import { useSourcePage, sourceError } from "@/lib/use-source-page";
import { Button } from "./button";
import { PageActions, ReadFailure } from "./record-ui";

export function ProposalDiscussion({
  proposalId,
  ledgerId,
  revision,
  version,
  userId,
  tenantId,
  csrfToken,
  canDiscuss,
  onReload,
  onSessionRefresh,
}: {
  proposalId: string;
  ledgerId: string;
  revision: number;
  version: number;
  userId: string;
  tenantId: string;
  csrfToken: string;
  canDiscuss: boolean;
  onReload: () => void;
  onSessionRefresh: () => void;
}) {
  const [refresh, setRefresh] = useState(0);
  return (
    <section
      className="card proposal-discussion"
      aria-labelledby="discussion-title"
    >
      <div className="card-heading">
        <MessageSquare size={18} />
        <h3 id="discussion-title" tabIndex={-1}>
          Discussion
        </h3>
        <span className="outline-tag">Not a decision</span>
      </div>
      <div className="card-content">
        <p className="muted small">
          Comments record their author and the revision current when posted.
          They do not approve, reject, release reservations or change financial
          records.
        </p>
        <DiscussionRows
          key={refresh}
          proposalId={proposalId}
          ledgerId={ledgerId}
          userId={userId}
          version={version}
          onSessionRefresh={onSessionRefresh}
        />
        {canDiscuss ? (
          <CommentComposer
            scope={{ tenantId, userId, proposalId }}
            revision={revision}
            version={version}
            csrfToken={csrfToken}
            onReload={onReload}
            onSessionRefresh={onSessionRefresh}
            onSaved={() => setRefresh((n) => n + 1)}
          />
        ) : (
          <p className="muted small">
            Your account can read this thread. Posting requires propose or
            review permission.
          </p>
        )}
      </div>
    </section>
  );
}
function DiscussionRows({
  proposalId,
  ledgerId,
  userId,
  version,
  onSessionRefresh,
}: {
  proposalId: string;
  ledgerId: string;
  userId: string;
  version: number;
  onSessionRefresh: () => void;
}) {
  const [newer, setNewer] = useState(false);
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await api.GET("/api/v1/proposals/{proposal_id}/comments", {
        signal,
        params: {
          path: { proposal_id: proposalId },
          query: { limit: "20", ...(cursor ? { cursor } : {}) },
        },
      });
      if (!r.data) throw sourceError(r.response.status);
      if (
        r.data.proposal_id !== proposalId ||
        r.data.items.some(
          (c) => c.proposal_id !== proposalId || c.ledger_id !== ledgerId,
        )
      )
        throw new Error(
          "Discussion scope could not be verified. No comments are shown.",
        );
      if (!signal.aborted) setNewer(r.data.current_version !== version);
      return r.data;
    },
    [proposalId, ledgerId, version],
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
    <div className="discussion-thread">
      {newer && (
        <p className="notice" role="status">
          The proposal has changed since this view was opened. Refresh the
          proposal before posting to its current revision.
        </p>
      )}
      {!page.loading && !page.items.length && (
        <p className="discussion-empty">
          No comments yet. Start with a question about the evidence.
        </p>
      )}
      <ol aria-label="Proposal comments">
        {page.items.map((comment) => (
          <li key={comment.id}>
            <div className="discussion-comment-meta">
              <strong>
                {comment.authored_by === userId
                  ? "You"
                  : `Member ${comment.authored_by.slice(0, 8)}`}
              </strong>
              <span>
                Comment {comment.position} · Revision {comment.revision} ·{" "}
                <time dateTime={comment.created_at}>
                  {new Intl.DateTimeFormat("en-GB", {
                    dateStyle: "medium",
                    timeStyle: "short",
                  }).format(new Date(comment.created_at))}
                </time>
              </span>
            </div>
            <p className="discussion-comment-body">{comment.body}</p>
            <details className="record-technical">
              <summary>Comment attribution</summary>
              <p>
                Author <code>{comment.authored_by}</code>
              </p>
              <p>
                Recorded proposal version {comment.proposal_version} · Comment{" "}
                <code>{comment.id}</code>
              </p>
            </details>
          </li>
        ))}
      </ol>
      <PageActions page={page} label="comments" />
    </div>
  );
}
function CommentComposer({
  scope,
  revision,
  version,
  csrfToken,
  onSaved,
  onReload,
  onSessionRefresh,
}: {
  scope: CommentScope;
  revision: number;
  version: number;
  csrfToken: string;
  onSaved: () => void;
  onReload: () => void;
  onSessionRefresh: () => void;
}) {
  const storageKey = commentStorageKey(scope);
  const [recovery, setRecovery] = useState<CommentRecovery>({
    schema: 1,
    ...scope,
    body: "",
    request: null,
    blockedVersion: null,
  });
  const [ready, setReady] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [stored, setStored] = useState(true);
  const active = useRef<AbortController | null>(null),
    sending = useRef(false);
  useEffect(() => {
    try {
      const saved = readCommentRecovery(
        sessionStorage.getItem(storageKey),
        scope,
      );
      if (saved) setRecovery(saved);
    } catch {
      setStored(false);
    }
    setReady(true);
    return () => active.current?.abort();
    // The parent is keyed by the verified tenant/proposal. Session changes unmount it.
  }, [storageKey, scope.tenantId, scope.userId, scope.proposalId]);
  function remember(value: CommentRecovery) {
    setRecovery(value);
    try {
      if (value.body || value.request)
        sessionStorage.setItem(storageKey, JSON.stringify(value));
      else sessionStorage.removeItem(storageKey);
      setStored(true);
    } catch {
      setStored(false);
    }
  }
  async function send(event: FormEvent) {
    event.preventDefault();
    if (
      !ready ||
      sending.current ||
      !recovery.body.trim() ||
      (recovery.blockedVersion === version && !recovery.request)
    )
      return;
    const attempted = {
      ...recovery,
      request: recovery.request ?? { key: crypto.randomUUID(), version },
    };
    remember(attempted);
    sending.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    const controller = new AbortController();
    active.current = controller;
    try {
      const r = await api.POST("/api/v1/proposals/{proposal_id}/comments", {
        signal: controller.signal,
        params: { path: { proposal_id: scope.proposalId } },
        body: { body: attempted.body },
        headers: {
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": attempted.request.key,
          "If-Match": `"v${attempted.request.version}"`,
        },
      });
      if (controller.signal.aborted) return;
      if (r.data) {
        if (
          r.data.proposal_id !== scope.proposalId ||
          r.data.authored_by !== scope.userId ||
          r.data.body !== attempted.body
        )
          throw new Error(
            "The returned comment could not be confirmed. Retry with the same key.",
          );
        remember({
          ...attempted,
          body: "",
          request: null,
          blockedVersion: null,
        });
        setNotice(
          `Comment ${r.data.position} saved on revision ${r.data.revision}. No decision or financial change was made.`,
        );
        onSaved();
      } else if (r.response.status === 409 || r.response.status >= 500) {
        setError(
          "The comment’s result is not confirmed. Retry the same comment; its text and request key are preserved.",
        );
      } else {
        remember({
          ...attempted,
          request: null,
          blockedVersion:
            r.response.status === 412 ? attempted.request.version : null,
        });
        setError(
          r.response.status === 412
            ? "The proposal changed. Refresh and review its current content before posting. Your draft stays in this tab."
            : r.response.status === 401
              ? "Your session has ended. Refresh your session before continuing."
              : r.response.status === 403
                ? "Your account cannot post to this thread. Refresh your session."
                : r.response.status === 404
                  ? "This proposal is unavailable to your organization."
                  : "The comment was refused. Check its text and retry; no comment was saved.",
        );
      }
    } catch (e) {
      if (!controller.signal.aborted)
        setError(
          e instanceof Error
            ? e.message
            : "The response was interrupted. Retry the same comment.",
        );
    } finally {
      if (!controller.signal.aborted) {
        sending.current = false;
        setBusy(false);
      }
    }
  }
  const pending = recovery.request !== null,
    blocked = recovery.blockedVersion === version;
  return (
    <form className="discussion-composer" onSubmit={(e) => void send(e)}>
      <label htmlFor="proposal-comment-body">
        Add a comment · current revision {revision}
      </label>
      <textarea
        id="proposal-comment-body"
        rows={4}
        value={recovery.body}
        readOnly={pending}
        disabled={!ready || busy}
        aria-describedby="comment-help"
        onChange={(e) => {
          if (Array.from(e.target.value).length <= 4000)
            remember({ ...recovery, body: e.target.value });
        }}
      />
      <p id="comment-help" className="muted small">
        Plain text · {Array.from(recovery.body).length}/4,000 characters. Draft
        text and retry details stay in this browser tab for recovery; no session
        credentials are stored with them.
      </p>
      {!stored && (
        <p role="status" className="notice">
          Tab storage is unavailable. Keep this view open until the comment is
          confirmed; copy your text before refreshing.
        </p>
      )}
      {error && (
        <div role="alert">
          <p>{error}</p>
          <div className="financial-actions">
            {blocked && (
              <Button type="button" variant="secondary" onClick={onReload}>
                Refresh proposal
              </Button>
            )}
            <Button type="button" variant="ghost" onClick={onSessionRefresh}>
              Refresh session
            </Button>
          </div>
        </div>
      )}
      {notice && (
        <p role="status" className="notice">
          {notice}
        </p>
      )}
      <div className="discussion-compose-actions">
        <span className="muted small">
          Discussion only · no approval authority
        </span>
        <Button
          type="submit"
          disabled={
            !ready || busy || !recovery.body.trim() || (blocked && !pending)
          }
        >
          <Send size={15} />
          {busy
            ? "Saving comment…"
            : pending
              ? "Retry same comment"
              : "Post comment"}
        </Button>
      </div>
    </form>
  );
}

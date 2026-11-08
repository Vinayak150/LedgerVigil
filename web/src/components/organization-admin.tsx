"use client";
// Organization administration (Stage 2B Family 1, migration 0036): members and roles, account security, and the audit log.
// The server enforces every rule (administer, fresh password confirmation, no self-grant of review/execute/ledger.bootstrap,
// 24-hour cooling-off, last administrator, If-Match). This UI only shows what the server returns; nothing changes before the
// server commits, and refusals are shown with the server's reason.
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  History,
  KeyRound,
  LogOut,
  ShieldCheck,
  UserPlus,
  Users,
} from "lucide-react";
import { api, offlineMessage } from "@/lib/api";
import type { components } from "@/lib/api.generated";
import { Button } from "./button";
import { ReadFailure, RecordEmpty } from "./record-ui";

type Member = components["schemas"]["Member"];
type AuditEvent = components["schemas"]["AuditEvent"];
const ROLES = [
  "ANALYST",
  "CONTROLLER",
  "AUTOMATION_DEVELOPER",
  "ADMINISTRATOR",
] as const;
const ROLE_TEXT: Record<string, string> = {
  ANALYST: "Analyst · read, propose, import sources",
  CONTROLLER: "Controller · review, execute, export evidence, read audit",
  AUTOMATION_DEVELOPER:
    "Automation developer · propose, import, regression, export evidence",
  ADMINISTRATOR:
    "Administrator · members, ledgers, audit (no financial approval)",
};

function refusal(status: number, code?: string, detail?: string) {
  if (code === "REAUTHENTICATION_REQUIRED")
    return "Confirm your password first (below), then try again.";
  if (code === "SEGREGATION_OF_DUTIES")
    return "You cannot grant yourself review, execute or ledger.bootstrap. Another administrator must do it.";
  if (code === "LAST_ADMINISTRATOR")
    return "An organization must keep at least one administrator.";
  if (code === "MEMBER_EXISTS")
    return "This person is already a member. Change their roles instead.";
  if (status === 412 || status === 428)
    return "This membership changed since you loaded it. It has been reloaded; review it and try again.";
  if (status === 403) return "This needs the administer permission.";
  if (status === 401) return "Your session has ended. Sign in again.";
  if (status === 422)
    return "Check the email address and choose at least one role.";
  return (
    detail ??
    "The change was not confirmed. Reload to see the current state before trying again."
  );
}

function ReauthCard({
  csrfToken,
  onDone,
}: {
  csrfToken: string;
  onDone: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(
    null,
  );
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget;
    const password = String(new FormData(form).get("password") ?? "");
    setPending(true);
    setMessage(null);
    try {
      const r = await api.POST("/api/v1/auth/reauthenticate", {
        headers: { "X-CSRF-Token": csrfToken },
        body: { password },
      });
      form.reset();
      setMessage(
        r.data
          ? {
              ok: true,
              text: `Confirmed. Member changes are allowed until ${new Date(r.data.fresh_until).toLocaleTimeString()}.`,
            }
          : {
              ok: false,
              text:
                r.response.status === 429
                  ? "Too many attempts. Wait before trying again."
                  : "The password was not accepted.",
            },
      );
      if (r.data) onDone();
    } catch {
      setMessage({ ok: false, text: offlineMessage });
    } finally {
      setPending(false);
    }
  }
  return (
    <form
      className="admin-reauth"
      onSubmit={submit}
      aria-label="Confirm your password"
    >
      <label htmlFor="reauth-password">
        Confirm your password to change memberships (valid 15 minutes)
      </label>
      <div className="admin-inline">
        <input
          id="reauth-password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          maxLength={1024}
          disabled={pending}
        />
        <Button type="submit" variant="secondary" disabled={pending}>
          <KeyRound size={15} /> {pending ? "Checking…" : "Confirm"}
        </Button>
      </div>
      {message && (
        <p
          className={message.ok ? "small" : "notice error"}
          role={message.ok ? "status" : "alert"}
        >
          {message.text}
        </p>
      )}
    </form>
  );
}

export function MembersPanel({
  csrfToken,
  userId,
  canAdminister,
  onSessionRefresh,
}: {
  csrfToken: string;
  userId: string;
  canAdminister: boolean;
  onSessionRefresh: () => void;
}) {
  const [items, setItems] = useState<Member[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(
    null,
  );
  const [editing, setEditing] = useState<string | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const live = useRef<HTMLParagraphElement>(null);
  const load = useCallback(async () => {
    setError("");
    try {
      const r = await api.GET("/api/v1/members");
      if (r.data) setItems(r.data.items);
      else
        setError(
          r.response.status === 403
            ? "Reading members needs the read permission."
            : "Members could not be loaded.",
        );
    } catch {
      setError(offlineMessage);
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  useEffect(() => {
    if (message) live.current?.focus();
  }, [message]);
  async function run(
    name: string,
    call: () => Promise<{ response: Response; error?: unknown }>,
    ok: string,
  ) {
    if (busy) return;
    setBusy(name);
    setMessage(null);
    try {
      const r = await call();
      if (r.response.status >= 200 && r.response.status < 300) {
        setMessage({ ok: true, text: ok });
        setEditing(null);
      } else {
        const e = r.error as { code?: string; detail?: string } | undefined;
        setMessage({
          ok: false,
          text: refusal(r.response.status, e?.code, e?.detail),
        });
        if (r.response.status === 401) onSessionRefresh();
      }
    } catch {
      setMessage({
        ok: false,
        text: "The change was not confirmed because the service could not be reached. Reload before trying again.",
      });
    } finally {
      setBusy(null);
      await load();
    }
  }
  const headers = (etag?: string) => ({
    "X-CSRF-Token": csrfToken,
    "Idempotency-Key": crypto.randomUUID(),
    ...(etag ? { "If-Match": `"${etag}"` } : {}),
  });
  function invite(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = e.currentTarget,
      data = new FormData(form);
    const roles = ROLES.filter((r) => data.get(`role-${r}`) === "on");
    void run(
      "invite",
      () =>
        api.POST("/api/v1/members", {
          headers: headers(),
          body: {
            email: String(data.get("email") ?? ""),
            display_name: String(data.get("display_name") ?? "") || null,
            roles,
          },
        }),
      "Invitation recorded. If delivery is configured, the person receives a single-use link valid for 7 days.",
    ).then(() => form.reset());
  }
  function saveRoles(m: Member, e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const data = new FormData(e.currentTarget);
    const roles = ROLES.filter(
      (r) => data.get(`edit-${m.user_id}-${r}`) === "on",
    );
    void run(
      `roles-${m.user_id}`,
      () =>
        api.PATCH("/api/v1/members/{user_id}", {
          params: { path: { user_id: m.user_id } },
          headers: headers(m.etag),
          body: { roles },
        }),
      m.user_id === userId
        ? "Roles changed. Your own sessions ended; sign in again."
        : "Roles changed. The member's sessions in this organization ended; new review or execute grants wait 24 hours.",
    );
  }
  return (
    <section className="card" aria-labelledby="members-heading">
      <div className="card-heading">
        <Users size={19} />
        <h2 id="members-heading">Members and roles</h2>
      </div>
      <div className="card-content">
        {canAdminister && (
          <ReauthCard csrfToken={csrfToken} onDone={() => setMessage(null)} />
        )}
        {message && (
          <p
            ref={live}
            tabIndex={-1}
            className={message.ok ? "notice" : "notice error"}
            role={message.ok ? "status" : "alert"}
          >
            {message.text}
          </p>
        )}
        {error ? (
          <ReadFailure
            message={error}
            retry={() => void load()}
            refresh={onSessionRefresh}
          />
        ) : items === null ? (
          <p role="status">Loading members…</p>
        ) : items.length === 0 ? (
          <RecordEmpty>No members are visible.</RecordEmpty>
        ) : (
          <ul className="admin-members">
            {items.map((m) => (
              <li
                key={m.user_id}
                className={m.status !== "ACTIVE" ? "is-removed" : ""}
              >
                <div>
                  <strong>{m.display_name}</strong>{" "}
                  <span className="muted">{m.email}</span>
                  {m.user_id === userId && <span className="role">you</span>}
                  {m.status !== "ACTIVE" && (
                    <span className="record-status is-refused">removed</span>
                  )}
                  {!m.email_verified && (
                    <span className="record-status is-caution">
                      {m.invitation_pending
                        ? "invitation pending"
                        : "email not verified"}
                    </span>
                  )}
                </div>
                <div className="role-list">
                  {m.roles.map((r) => (
                    <span
                      className="role"
                      key={r.role}
                      title={
                        r.cooling_off_until
                          ? `Review/execute usable after ${r.cooling_off_until}`
                          : undefined
                      }
                    >
                      {r.role.toLowerCase().replaceAll("_", " ")}
                      {r.cooling_off_until &&
                      Date.parse(r.cooling_off_until) > Date.now()
                        ? " · cooling off"
                        : ""}
                    </span>
                  ))}
                </div>
                {canAdminister &&
                  m.status === "ACTIVE" &&
                  (editing === m.user_id ? (
                    <form
                      onSubmit={(e) => saveRoles(m, e)}
                      className="admin-edit"
                    >
                      <fieldset>
                        <legend>Roles for {m.display_name}</legend>
                        {ROLES.map((r) => (
                          <label key={r} className="admin-check">
                            <input
                              type="checkbox"
                              name={`edit-${m.user_id}-${r}`}
                              defaultChecked={m.roles.some((x) => x.role === r)}
                            />
                            {ROLE_TEXT[r]}
                          </label>
                        ))}
                      </fieldset>
                      <div className="financial-actions">
                        <Button type="submit" disabled={!!busy}>
                          Save roles
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          onClick={() => setEditing(null)}
                          disabled={!!busy}
                        >
                          Cancel
                        </Button>
                      </div>
                    </form>
                  ) : (
                    <div className="financial-actions">
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => setEditing(m.user_id)}
                        disabled={!!busy}
                      >
                        Change roles
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        disabled={!!busy}
                        onClick={() =>
                          void run(
                            `sessions-${m.user_id}`,
                            () =>
                              api.POST(
                                "/api/v1/members/{user_id}/sessions/revoke",
                                {
                                  params: { path: { user_id: m.user_id } },
                                  headers: headers(),
                                },
                              ),
                            "That member's sessions in this organization were ended.",
                          )
                        }
                      >
                        End sessions
                      </Button>
                      {removing === m.user_id ? (
                        <span className="provider-confirm">
                          <span>
                            Remove {m.display_name}? Their roles and sessions
                            here end; history is kept.
                          </span>
                          <Button
                            type="button"
                            variant="secondary"
                            disabled={!!busy}
                            onClick={() => {
                              setRemoving(null);
                              void run(
                                `remove-${m.user_id}`,
                                () =>
                                  api.DELETE("/api/v1/members/{user_id}", {
                                    params: { path: { user_id: m.user_id } },
                                    headers: headers(m.etag),
                                  }),
                                "Member removed. Their history is kept.",
                              );
                            }}
                          >
                            Yes, remove
                          </Button>
                          <Button
                            type="button"
                            variant="ghost"
                            onClick={() => setRemoving(null)}
                          >
                            Keep
                          </Button>
                        </span>
                      ) : (
                        <Button
                          type="button"
                          variant="ghost"
                          disabled={!!busy}
                          onClick={() => setRemoving(m.user_id)}
                        >
                          Remove
                        </Button>
                      )}
                    </div>
                  ))}
              </li>
            ))}
          </ul>
        )}
        {canAdminister ? (
          <form
            className="admin-invite"
            onSubmit={invite}
            aria-labelledby="invite-heading"
          >
            <h3 id="invite-heading">
              <UserPlus size={16} /> Invite a member
            </h3>
            <label htmlFor="invite-email">Email address</label>
            <input
              id="invite-email"
              name="email"
              type="email"
              required
              maxLength={320}
              disabled={!!busy}
            />
            <label htmlFor="invite-name">Display name (optional)</label>
            <input
              id="invite-name"
              name="display_name"
              maxLength={120}
              disabled={!!busy}
            />
            <fieldset>
              <legend>Roles</legend>
              {ROLES.map((r) => (
                <label key={r} className="admin-check">
                  <input type="checkbox" name={`role-${r}`} disabled={!!busy} />{" "}
                  {ROLE_TEXT[r]}
                </label>
              ))}
            </fieldset>
            <Button type="submit" disabled={!!busy}>
              {busy === "invite" ? "Sending…" : "Send invitation"}
            </Button>
          </form>
        ) : (
          <p className="small muted">
            Only an administrator can invite members or change roles.
          </p>
        )}
      </div>
    </section>
  );
}

export function AccountSecurity({
  csrfToken,
  onSignedOut,
}: {
  csrfToken: string;
  onSignedOut: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [confirming, setConfirming] = useState(false);
  async function everywhere() {
    setConfirming(false);
    setPending(true);
    setError("");
    try {
      const r = await api.POST("/api/v1/auth/sessions/revoke-all", {
        headers: { "X-CSRF-Token": csrfToken },
      });
      if (r.response.status === 204) {
        new BroadcastChannel("ledgervigil-session").postMessage("signed-out");
        onSignedOut();
      } else setError("Sign-out could not be confirmed. Reload and try again.");
    } catch {
      setError(offlineMessage);
    } finally {
      setPending(false);
    }
  }
  return (
    <section className="card" aria-labelledby="security-heading">
      <div className="card-heading">
        <ShieldCheck size={19} />
        <h2 id="security-heading">Account security</h2>
      </div>
      <div className="card-content">
        <p className="small">
          Change your password from the sign-in page (“Forgot your password?”);
          a reset signs out every session. This deployment supports sandbox
          ledgers and provider sandboxes only.
        </p>
        <div className="financial-actions">
          {confirming ? (
            <span className="provider-confirm">
              <span>
                Sign out of every browser and device, including this one?
              </span>
              <Button
                type="button"
                variant="secondary"
                onClick={() => void everywhere()}
              >
                Yes, sign out everywhere
              </Button>
              <Button
                type="button"
                variant="ghost"
                onClick={() => setConfirming(false)}
              >
                Cancel
              </Button>
            </span>
          ) : (
            <Button
              type="button"
              variant="secondary"
              onClick={() => setConfirming(true)}
              disabled={pending}
            >
              <LogOut size={15} />{" "}
              {pending ? "Signing out…" : "Sign out everywhere"}
            </Button>
          )}
        </div>
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
      </div>
    </section>
  );
}

export function AuditLogPanel({ canRead }: { canRead: boolean }) {
  const [items, setItems] = useState<AuditEvent[]>([]);
  const [next, setNext] = useState<string | null | undefined>(undefined);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const page = useCallback(async (before?: string) => {
    setLoading(true);
    setError("");
    try {
      const r = await api.GET("/api/v1/audit-events", {
        params: { query: before ? { before, limit: 25 } : { limit: 25 } },
      });
      if (r.data) {
        setItems((old) => (before ? [...old, ...r.data.items] : r.data.items));
        setNext(r.data.next_before);
      } else
        setError(
          r.response.status === 403
            ? "Reading the audit log needs the audit.read permission."
            : "The audit log could not be loaded.",
        );
    } catch {
      setError(offlineMessage);
    } finally {
      setLoading(false);
    }
  }, []);
  useEffect(() => {
    if (canRead) void page();
  }, [canRead, page]);
  if (!canRead) return null;
  return (
    <section className="card" aria-labelledby="audit-heading">
      <div className="card-heading">
        <History size={19} />
        <h2 id="audit-heading">Audit log</h2>
        <span className="muted small">Hash-chained, newest first</span>
      </div>
      <div className="card-content">
        {error ? (
          <ReadFailure message={error} retry={() => void page()} />
        ) : items.length === 0 && loading ? (
          <p role="status">Loading audit events…</p>
        ) : items.length === 0 ? (
          <RecordEmpty>No audit events yet.</RecordEmpty>
        ) : (
          <div className="table-scroll">
            <table className="admin-audit">
              <caption className="sr-only">
                Audit events for this organization
              </caption>
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">When (UTC)</th>
                  <th scope="col">Event</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">User</th>
                  <th scope="col">Hash</th>
                </tr>
              </thead>
              <tbody>
                {items.map((e) => (
                  <tr key={e.seq}>
                    <td>{e.seq}</td>
                    <td>{e.occurred_at.replace("T", " ").slice(0, 19)}</td>
                    <td>{e.event_type}</td>
                    <td>
                      <span
                        className={`record-status ${e.outcome === "DENIED" ? "is-refused" : ""}`}
                      >
                        {e.outcome}
                      </span>
                    </td>
                    <td>
                      <code>{e.user_id ? e.user_id.slice(0, 8) : "—"}</code>
                    </td>
                    <td>
                      <code title={e.hash}>{e.hash.slice(0, 12)}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {next && (
          <Button
            type="button"
            variant="secondary"
            onClick={() => void page(next)}
            disabled={loading}
          >
            {loading ? "Loading…" : "Load older events"}
          </Button>
        )}
      </div>
    </section>
  );
}

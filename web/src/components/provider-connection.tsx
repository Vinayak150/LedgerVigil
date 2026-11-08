"use client";
// QuickBooks Online SANDBOX connection (PROPOSED PROV-1..PROV-8). Read-only: no control here can create, change or delete
// anything at QuickBooks. Tokens never reach the browser; the authorization code passes through once, on the callback page.
import { useCallback, useEffect, useState } from "react";
import {
  Cable,
  Clock3,
  DatabaseZap,
  PlugZap,
  RefreshCw,
  Unplug,
} from "lucide-react";
import {
  optionalApi,
  type ProviderConnection,
  type ProviderConnectionList,
} from "@/lib/optional-api";
import { useSourcePage, sourceError } from "@/lib/use-source-page";
import { Button } from "./button";
import { Fact, PageActions, ReadFailure, RecordEmpty } from "./record-ui";

const STATE_TEXT: Record<string, string> = {
  AUTHORIZING: "Waiting for authorization at Intuit",
  EXCHANGING: "Completing authorization (integration worker)",
  ACTIVE: "Connected · read-only",
  DEGRADED: "Authorization rejected by QuickBooks — reconnect needed",
  REVOKING: "Disconnecting (integration worker)",
  REVOKED: "Disconnected",
  FAILED: "Connection attempt failed",
};
const FAILURE_TEXT: Record<string, string> = {
  AUTHORIZATION_DENIED: "Access was declined at Intuit.",
  AUTHORIZATION_ERROR: "Intuit returned an authorization error.",
  AUTHORIZATION_EXPIRED: "The authorization took longer than 10 minutes.",
  AUTHORIZATION_SUPERSEDED: "A newer connection attempt replaced this one.",
  AUTHORIZATION_CANCELLED: "The attempt was cancelled.",
  AUTHORIZATION_CODE_INVALID:
    "Intuit did not accept the authorization code; start again.",
  CLIENT_CREDENTIALS_INVALID:
    "The server's Intuit app credentials were rejected; an operator must check the private configuration.",
  REALM_ALREADY_CONNECTED:
    "This QuickBooks company is already connected to a LedgerVigil organization.",
  REALM_VERIFICATION_FAILED:
    "The new authorization could not read this company; it was revoked.",
  REALM_VERIFICATION_UNAVAILABLE:
    "QuickBooks was unavailable while verifying the company; the authorization was revoked. Try again.",
  REQUESTER_REVOKED:
    "The administrator who started the attempt no longer has access.",
  AUTH_INVALID:
    "QuickBooks rejected the stored authorization (for example it was disconnected at Intuit).",
  LEASE_EXPIRED: "The integration worker stopped before finishing.",
  REVOCATION_PENDING:
    "Intuit could not be reached to revoke the authorization. The encrypted tokens are kept only so revocation can be retried; they cannot be used for reads.",
};
const ERROR_TEXT: Record<string, string> = {
  PROVIDER_THROTTLED:
    "QuickBooks asked to slow down; the read resumes after 60 seconds from the last saved page.",
  PROVIDER_UNAVAILABLE:
    "QuickBooks was unavailable; the read retries with back-off.",
  PROVIDER_TIMEOUT: "QuickBooks did not answer in time; the read retries.",
  PROVIDER_UNREACHABLE:
    "QuickBooks could not be reached from the integration worker.",
  PROVIDER_FORBIDDEN: "QuickBooks refused access to this data.",
  PROVIDER_REQUEST_REJECTED: "QuickBooks rejected a read request.",
  PROVIDER_RESPONSE_INVALID:
    "QuickBooks returned a response that could not be verified; nothing from it was stored.",
  CONNECTION_NOT_ACTIVE: "The connection stopped being active during the read.",
  AUTH_INVALID: FAILURE_TEXT.AUTH_INVALID,
};

function problemText(status: number, code?: string) {
  if (status === 403)
    return "Connecting, reading and disconnecting QuickBooks needs the administer permission.";
  if (status === 503)
    return "QuickBooks is not configured on this server. An operator must run the private setup step first.";
  if (code === "CONNECTION_EXISTS")
    return "A QuickBooks sandbox connection already exists. Disconnect it first.";
  if (code === "SYNC_IN_PROGRESS")
    return "A read is already queued or running.";
  if (code === "CONNECTION_DEGRADED")
    return "QuickBooks rejected the stored authorization. Disconnect and connect again.";
  if (status === 412)
    return "The connection changed meanwhile. The latest state has been loaded.";
  if (status === 409)
    return "This action is not possible in the connection's current state. The latest state has been loaded.";
  if (status === 401) return "Your session has ended. Refresh your session.";
  return "The request could not be confirmed. Reload to see the current state before trying again.";
}

export function ProviderConnectionPanel({
  csrfToken,
  canAdminister,
  onSessionRefresh,
}: {
  csrfToken: string;
  canAdminister: boolean;
  onSessionRefresh: () => void;
}) {
  const [data, setData] = useState<ProviderConnectionList | null>(null),
    [state, setState] = useState<"loading" | "ready" | "disabled" | "failed">(
      "loading",
    ),
    [reload, setReload] = useState(0),
    [busy, setBusy] = useState(""),
    [message, setMessage] = useState(""),
    [confirming, setConfirming] = useState(false),
    [abandoning, setAbandoning] = useState(false),
    [browse, setBrowse] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const r = await optionalApi.GET("/api/v1/provider-connections", {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        if (r.data) {
          setData(r.data);
          setState("ready");
        } else setState(r.response.status === 404 ? "disabled" : "failed");
      } catch {
        if (!controller.signal.aborted) setState("failed");
      }
    })();
    return () => controller.abort();
  }, [reload]);
  const current =
      data?.items.find((c) => !["REVOKED", "FAILED"].includes(c.state)) ?? null,
    last = data?.items[0] ?? null;
  // Background states move on the worker's schedule; poll gently only while one is pending.
  useEffect(() => {
    const working =
      current &&
      (["EXCHANGING", "REVOKING"].includes(current.state) ||
        current.jobs.some((j) => ["QUEUED", "CLAIMED"].includes(j.state)));
    if (!working) return;
    const t = window.setTimeout(() => setReload((n) => n + 1), 4000);
    return () => window.clearTimeout(t);
  }, [current]);
  async function act(
    name: string,
    run: () => Promise<{ data?: unknown; response: Response; error?: unknown }>,
    after?: (d: unknown) => void,
  ) {
    setBusy(name);
    setMessage("");
    try {
      const r = await run();
      if (r.data) after?.(r.data);
      else
        setMessage(
          problemText(
            r.response.status,
            (r.error as { code?: string } | undefined)?.code,
          ),
        );
    } catch {
      setMessage(
        "The request could not be confirmed. Reload to see the current state before trying again.",
      );
    } finally {
      setBusy("");
      setReload((n) => n + 1);
    }
  }
  const connect = () =>
    act(
      "connect",
      () =>
        optionalApi.POST("/api/v1/provider-connections", {
          headers: {
            "X-CSRF-Token": csrfToken,
            "Idempotency-Key": crypto.randomUUID(),
          },
          body: {},
        }),
      (d) => {
        const url = (d as { authorization_url: string | null })
          .authorization_url;
        if (
          url &&
          url.startsWith("https://appcenter.intuit.com/connect/oauth2?")
        )
          window.location.assign(url);
        else
          setMessage(
            "The authorization link could not be issued again. Start the connection again.",
          );
      },
    );
  const sync = (c: ProviderConnection) =>
    act("sync", () =>
      optionalApi.POST("/api/v1/provider-connections/{connection_id}/syncs", {
        params: { path: { connection_id: c.id } },
        headers: {
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": crypto.randomUUID(),
          "If-Match": `"${c.etag}"`,
        },
        body: {},
      }),
    );
  const disconnect = (c: ProviderConnection, abandon = false) =>
    act(
      abandon ? "abandon" : "disconnect",
      () =>
        optionalApi.POST(
          "/api/v1/provider-connections/{connection_id}/disconnect",
          {
            params: { path: { connection_id: c.id } },
            headers: {
              "X-CSRF-Token": csrfToken,
              "Idempotency-Key": crypto.randomUUID(),
              "If-Match": `"${c.etag}"`,
            },
            body: abandon ? { abandon_revocation: true } : {},
          },
        ),
      () => {
        setConfirming(false);
        setAbandoning(false);
      },
    );
  return (
    <section className="card provider-card" aria-labelledby="provider-heading">
      <div className="card-heading">
        <Cable size={19} />
        <h2 id="provider-heading">QuickBooks Online sandbox</h2>
        <span className="outline-tag">Read-only · sandbox</span>
      </div>
      <div className="card-content">
        <p className="small muted">
          Reads accounts, customers, invoices, payments, deposits and journal
          entries from an Intuit <strong>sandbox</strong> company into a
          separate, append-only replica labelled provider sandbox data. Nothing
          is written to QuickBooks, and nothing from QuickBooks enters native
          ledgers, proposals or detection. Sandbox data is not business
          evidence.
        </p>
        {state === "loading" && <p role="status">Loading connection state…</p>}
        {state === "disabled" && (
          <RecordEmpty>
            The QuickBooks sandbox connection is not enabled on this server
            (LV_PROVIDER_SANDBOX_ENABLED is unset by default).
          </RecordEmpty>
        )}
        {state === "failed" && (
          <ReadFailure
            message="Connection state could not be loaded."
            retry={() => setReload((n) => n + 1)}
            refresh={onSessionRefresh}
          />
        )}
        {state === "ready" && data && (
          <>
            {!data.configuration.configured && (
              <div className="notice" role="note">
                <PlugZap size={18} />
                <span>
                  Not configured on this server. An operator runs the private
                  setup step (client ID and secret entered in Terminal, never in
                  a browser or chat).
                </span>
              </div>
            )}
            <dl className="record-facts">
              <Fact label="State">
                {current ? STATE_TEXT[current.state] : "Not connected"}
              </Fact>
              <Fact label="Company (realm)">
                {current?.realm_id ? <code>{current.realm_id}</code> : "—"}
              </Fact>
              <Fact label="Scope">
                <code>{String(data.capabilities.scope)}</code>
              </Fact>
              <Fact label="Writes to QuickBooks">
                Disabled — no write path exists
              </Fact>
              <Fact label="API minor version">
                {String(data.capabilities.minor_version)}
              </Fact>
              {current?.connected_at && (
                <Fact label="Connected">
                  {current.connected_at.replace("T", " ").slice(0, 19)} UTC
                </Fact>
              )}
            </dl>
            {!current && last?.state === "FAILED" && last.failure_code && (
              <p className="notice error" role="alert">
                Last attempt:{" "}
                {FAILURE_TEXT[last.failure_code] ?? last.failure_code}
              </p>
            )}
            {!current && last?.state === "REVOKED" && (
              <p className="small muted">
                Last connection disconnected{" "}
                {last.revoke_outcome === "ABANDONED"
                  ? "locally after revocation could not reach Intuit; disconnect the app in your Intuit account."
                  : last.revoke_outcome === "PROVIDER_UNCONFIRMED"
                    ? "locally; Intuit answered but did not confirm the revocation, so also disconnect the app in your Intuit account."
                    : "and revoked at Intuit."}{" "}
                Stored tokens were erased.
              </p>
            )}
            {current?.state === "REVOKING" &&
              current.failure_code === "REVOCATION_PENDING" && (
                <p className="notice error" role="alert">
                  {FAILURE_TEXT.REVOCATION_PENDING}{" "}
                  {current.revocation_auto_retry
                    ? `The integration worker retries hourly (${current.revocation_jobs} of 25 attempts used).`
                    : "Automatic retries have ended. Retry now, or end the revocation here and disconnect the app in your Intuit account."}
                </p>
              )}
            {current?.state === "DEGRADED" && (
              <p className="notice error" role="alert">
                {FAILURE_TEXT.AUTH_INVALID} Disconnect, then connect again.
              </p>
            )}
            {canAdminister ? (
              <div className="financial-actions">
                {!current || current.state === "AUTHORIZING" ? (
                  <Button
                    type="button"
                    onClick={() => void connect()}
                    disabled={!!busy || !data.configuration.configured}
                  >
                    <PlugZap size={15} />
                    {current
                      ? "Start the authorization again"
                      : "Connect a QuickBooks sandbox company"}
                  </Button>
                ) : null}
                {current?.state === "ACTIVE" && (
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() => void sync(current)}
                    disabled={
                      !!busy ||
                      current.jobs.some(
                        (j) =>
                          j.kind === "SYNC" &&
                          ["QUEUED", "CLAIMED"].includes(j.state),
                      )
                    }
                  >
                    <RefreshCw size={15} />
                    Read sandbox data now
                  </Button>
                )}
                {current?.state === "REVOKING" &&
                  current.failure_code === "REVOCATION_PENDING" &&
                  !current.jobs.some(
                    (j) =>
                      j.kind === "REVOKE" &&
                      ["QUEUED", "CLAIMED"].includes(j.state),
                  ) && (
                    <Button
                      type="button"
                      variant="secondary"
                      onClick={() => void disconnect(current)}
                      disabled={!!busy}
                    >
                      <Unplug size={15} />
                      Retry revocation now
                    </Button>
                  )}
                {current?.state === "REVOKING" &&
                  current.failure_code === "REVOCATION_PENDING" &&
                  !current.revocation_auto_retry &&
                  (abandoning ? (
                    <span className="provider-confirm">
                      <span>
                        End without Intuit’s confirmation? Local tokens are
                        erased; the authorization may still be active at Intuit
                        until you disconnect it there.
                      </span>
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => void disconnect(current, true)}
                        disabled={!!busy}
                      >
                        Yes, end it here
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() => setAbandoning(false)}
                      >
                        Keep retrying
                      </Button>
                    </span>
                  ) : (
                    <Button
                      type="button"
                      variant="ghost"
                      onClick={() => setAbandoning(true)}
                      disabled={!!busy}
                    >
                      End revocation without confirmation
                    </Button>
                  ))}
                {current &&
                  ["ACTIVE", "DEGRADED", "AUTHORIZING"].includes(
                    current.state,
                  ) &&
                  (confirming ? (
                    <span className="provider-confirm">
                      <span>
                        Disconnect? Tokens are revoked at Intuit and erased
                        here; the replica is kept.
                      </span>
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => void disconnect(current)}
                        disabled={!!busy}
                      >
                        <Unplug size={15} />
                        Yes, disconnect
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        onClick={() => setConfirming(false)}
                        disabled={!!busy}
                      >
                        Keep connected
                      </Button>
                    </span>
                  ) : (
                    <Button
                      type="button"
                      variant="ghost"
                      onClick={() => setConfirming(true)}
                      disabled={!!busy}
                    >
                      <Unplug size={15} />
                      Disconnect
                    </Button>
                  ))}
              </div>
            ) : (
              <p className="small muted">
                Only an administrator can connect, read or disconnect.
              </p>
            )}
            {busy && (
              <p role="status">
                <Clock3 size={14} /> Sending…
              </p>
            )}
            {message && (
              <p className="notice error" role="alert">
                {message}
              </p>
            )}
            {current && current.jobs.length > 0 && (
              <details className="record-technical" open>
                <summary>Integration jobs · most recent first</summary>
                <ul className="provider-jobs">
                  {current.jobs.map((j) => (
                    <li key={j.id}>
                      <strong>
                        {j.kind === "SYNC"
                          ? "Read"
                          : j.kind === "EXCHANGE"
                            ? "Authorization"
                            : "Disconnect"}
                      </strong>{" "}
                      <span
                        className={`record-status ${j.state === "DEAD" || j.state === "DENIED" ? "is-refused" : j.state === "DONE" ? "" : "is-caution"}`}
                      >
                        {j.state}
                      </span>{" "}
                      {j.result && typeof j.result.status === "string"
                        ? `· ${j.result.status.toLowerCase()}`
                        : ""}
                      {j.last_error
                        ? ` · ${ERROR_TEXT[j.last_error] ?? j.last_error}`
                        : ""}
                      {j.kind === "SYNC" &&
                      j.result?.provenance === "RECORDED_MOCK"
                        ? " · recorded mock (not live sandbox)"
                        : ""}
                      {Array.isArray(j.result?.truncated_entities) &&
                      (j.result.truncated_entities as string[]).length > 0
                        ? ` · stopped at the read bound for ${(j.result.truncated_entities as string[]).join(", ")}`
                        : ""}
                    </li>
                  ))}
                </ul>
              </details>
            )}
            {current && Object.keys(current.replica).length > 0 && (
              <div className="provider-replica">
                <h3>
                  <DatabaseZap size={16} /> Provider sandbox replica
                </h3>
                <p className="small muted">{current.data_label}</p>
                <div
                  className="record-view-switch"
                  role="group"
                  aria-label="Replica entities"
                >
                  {Object.entries(current.replica).map(([entity, n]) => (
                    <Button
                      key={entity}
                      type="button"
                      variant={browse === entity ? "primary" : "ghost"}
                      aria-pressed={browse === entity}
                      onClick={() => setBrowse(entity)}
                    >
                      {entity} · {n}
                    </Button>
                  ))}
                </div>
                {browse && (
                  <ReplicaObjects
                    key={`${current.id}:${browse}`}
                    connectionId={current.id}
                    entity={browse}
                  />
                )}
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function ReplicaObjects({
  connectionId,
  entity,
}: {
  connectionId: string;
  entity: string;
}) {
  const [open, setOpen] = useState<{ id: string; text: string } | null>(null);
  const loader = useCallback(
    async (cursor: string | undefined, signal: AbortSignal) => {
      const r = await optionalApi.GET(
        "/api/v1/provider-connections/{connection_id}/objects",
        {
          signal,
          params: {
            path: { connection_id: connectionId },
            query: { entity, limit: "25", ...(cursor ? { cursor } : {}) },
          },
        },
      );
      if (!r.data) throw sourceError(r.response.status);
      if (
        r.data.connection_id !== connectionId ||
        r.data.items.some(
          (o) => o.connection_id !== connectionId || o.entity !== entity,
        )
      )
        throw new Error(
          "The replica response did not match this connection. Nothing is shown.",
        );
      return r.data;
    },
    [connectionId, entity],
  );
  const page = useSourcePage(loader);
  if (page.error)
    return <ReadFailure message={page.error} retry={page.reload} />;
  return (
    <>
      <ul className="provider-objects">
        {page.items.map((o) => (
          <li key={o.id}>
            <span>
              {o.entity} <code>{o.provider_id}</code> · SyncToken {o.sync_token}{" "}
              ·{" "}
              {o.provenance === "RECORDED_MOCK"
                ? "recorded mock"
                : "provider sandbox"}
            </span>
            <Button
              type="button"
              variant="ghost"
              onClick={async () => {
                const r = await optionalApi.GET(
                  "/api/v1/provider-objects/{object_id}",
                  { params: { path: { object_id: o.id } } },
                );
                setOpen(
                  r.data?.object_text
                    ? { id: o.id, text: r.data.object_text }
                    : { id: o.id, text: "Object unavailable." },
                );
              }}
            >
              View provider JSON
            </Button>
            {open?.id === o.id && (
              <div className="record-text">
                <p className="small muted">
                  Provider JSON as stored by PostgreSQL (jsonb form; decimal
                  digits preserved). SHA-256 <code>{o.object_sha256}</code> ·
                  page <code>{String(o.page.body_sha256)}</code> · intuit_tid{" "}
                  <code>{String(o.page.intuit_tid ?? "none")}</code>
                </p>
                <pre>{open.text}</pre>
              </div>
            )}
          </li>
        ))}
      </ul>
      {!page.loading && !page.items.length && (
        <RecordEmpty>No stored objects of this type.</RecordEmpty>
      )}
      <PageActions page={page} label="objects" />
    </>
  );
}

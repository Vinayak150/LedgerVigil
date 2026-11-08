"use client";
// Intuit redirects here with ?code&state&realmId (or ?error&state). The parameters are read once, removed from the address
// bar and history immediately, and sent to the API in one CSRF-protected POST. They are never written to browser storage;
// if the response is lost, the attempt is checked on the settings page instead of being resent.
import { useEffect, useRef, useState } from "react";
import { optionalApi } from "@/lib/optional-api";
import { useSession } from "@/lib/use-session";

let consumed = false; // React development mode mounts twice; the single-use parameters are sent at most once per page load

export default function QuickBooksCallback() {
  const auth = useSession();
  const params = useRef<URLSearchParams | null>(null);
  const [outcome, setOutcome] = useState<{
    tone: "ok" | "error" | "wait";
    text: string;
  }>({
    tone: "wait",
    text: "Completing the QuickBooks sandbox authorization…",
  });
  useEffect(() => {
    if (params.current === null) {
      params.current = new URLSearchParams(window.location.search);
      window.history.replaceState(null, "", window.location.pathname);
    }
  }, []);
  useEffect(() => {
    if (auth.phase !== "ready" && auth.phase !== "anonymous") return;
    if (consumed || !params.current) return;
    consumed = true;
    const p = params.current;
    params.current = new URLSearchParams();
    if (!auth.session?.tenant)
      return setOutcome({
        tone: "error",
        text: "Your session ended before the authorization returned. Sign in and start the connection again.",
      });
    const state = p.get("state") ?? "",
      code = p.get("code"),
      realm = p.get("realmId"),
      error = p.get("error");
    const body = error
      ? { state, error }
      : { state, code: code ?? "", realm_id: realm ?? "" };
    void (async () => {
      try {
        const r = await optionalApi.POST(
          "/api/v1/provider-connections/quickbooks/callback",
          {
            headers: {
              "X-CSRF-Token": auth.session!.csrf_token,
              "Idempotency-Key": crypto.randomUUID(),
            },
            body: body as never,
          },
        );
        const c = (r.error as { code?: string } | undefined)?.code;
        if (r.response.status === 202)
          setOutcome({
            tone: "ok",
            text: "Authorization received. The integration worker is completing the connection; its state is on the settings page.",
          });
        else if (r.response.status === 200)
          setOutcome({
            tone: "error",
            text: "Intuit did not grant access. Nothing was connected.",
          });
        else
          setOutcome({
            tone: "error",
            text:
              c === "OAUTH_STATE_EXPIRED"
                ? "The authorization took longer than 10 minutes. Start the connection again."
                : c === "OAUTH_STATE_USED"
                  ? "This authorization was already processed. Check the connection state on the settings page."
                  : c === "REALM_ALREADY_CONNECTED"
                    ? "This QuickBooks company is already connected to a LedgerVigil organization."
                    : c === "OAUTH_STATE_INVALID"
                      ? "This authorization does not belong to your current session or organization. Start the connection again from this browser."
                      : r.response.status === 403
                        ? "Connecting QuickBooks needs the administer permission."
                        : "The authorization could not be confirmed. Check the connection state on the settings page before trying again.",
          });
      } catch {
        setOutcome({
          tone: "error",
          text: "The authorization could not be confirmed. Check the connection state on the settings page before trying again.",
        });
      }
    })();
  }, [auth.phase, auth.session]);
  return (
    <main className="center-screen">
      <section className="picker" aria-live="polite">
        <h1>QuickBooks sandbox connection</h1>
        <p
          className={outcome.tone === "error" ? "notice error" : "notice"}
          role={outcome.tone === "error" ? "alert" : "status"}
        >
          {outcome.text}
        </p>
        <a className="button button-secondary" href="/#settings">
          Back to organization settings
        </a>
      </section>
    </main>
  );
}

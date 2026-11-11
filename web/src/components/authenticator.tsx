"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { api } from "@/lib/api";
import type { components } from "@/lib/api.generated";
import { Button } from "./button";

type State = components["schemas"]["State"];
type Action = components["schemas"]["Change"]["action"];

export function Authenticator({ csrfToken }: { csrfToken: string }) {
  const [state, setState] = useState<State>();
  const [loading, setLoading] = useState(false);
  const [setup, setSetup] = useState("");
  const [codes, setCodes] = useState<string[]>([]);
  const [action, setAction] = useState<Action>("CHALLENGE");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const proof = useRef<HTMLInputElement>(null);
  const setupHeading = useRef<HTMLHeadingElement>(null);
  const codesHeading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    const controller = new AbortController();
    api
      .GET("/api/v1/auth/mfa", { signal: controller.signal })
      .then(({ data }) => {
        if (!controller.signal.aborted) {
          if (data) setState(data);
          else setError("Could not load authenticator status.");
        }
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setError("Could not load authenticator status.");
      });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (setup) setupHeading.current?.focus();
    if (codes.length) codesHeading.current?.focus();
  }, [setup, codes]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!state?.configured || loading) return;
    const form = event.currentTarget;
    const fields = new FormData(form);
    const chosen = setup ? "CONFIRM" : state.enrolled ? action : "START";
    const headers = { "X-CSRF-Token": csrfToken };
    setLoading(true);
    setError("");
    setMessage("");
    setCodes([]);
    try {
      if (
        chosen === "START" ||
        chosen === "REGENERATE" ||
        chosen.startsWith("RESET_")
      ) {
        const result = await api.POST("/api/v1/auth/reauthenticate", {
          headers,
          body: { password: String(fields.get("password") ?? "") },
        });
        if (!result.data) {
          setError(
            "Password confirmation failed. Check your password or try again later.",
          );
          return;
        }
      }
      const result = await api.POST("/api/v1/auth/mfa", {
        headers,
        body: {
          action: chosen,
          code: String(fields.get("proof") ?? "").trim(),
        },
      });
      if (!result.data) {
        setError(
          result.response.status === 429
            ? "Too many attempts. Wait 15 minutes and try again."
            : "The authenticator action could not be completed. Check your code and try again.",
        );
        return;
      }
      setSetup(result.data.setup_key ?? "");
      setCodes(result.data.recovery_codes ?? []);
      if (chosen !== "START") {
        if (chosen === "RESET_COMPLETE") {
          window.location.reload();
          return;
        }
        setState({ ...state, enrolled: true });
        const refreshed = await api
          .GET("/api/v1/auth/mfa")
          .catch(() => undefined);
        if (refreshed?.data) setState(refreshed.data);
        setMessage("This session has verified an authenticator proof.");
        setAction("CHALLENGE");
      }
    } catch {
      setError(
        "Could not confirm the authenticator action. Reload before retrying.",
      );
    } finally {
      form.reset();
      setLoading(false);
    }
  }
  const needsPassword =
    !setup &&
    state &&
    (!state.enrolled || action === "REGENERATE" || action.startsWith("RESET_"));
  return (
    <section className="card" aria-labelledby="authenticator-heading">
      <div className="card-heading">
        <h2 id="authenticator-heading">Authenticator app</h2>
      </div>
      <div className="card-content">
        {!state && !error && (
          <p role="status">Checking authenticator status…</p>
        )}
        {state && !state.configured && (
          <p>Authenticator setup is unavailable in this environment.</p>
        )}
        {state?.reset_after && (
          <p role="status">
            Authenticator removal cannot finish before{" "}
            {new Date(state.reset_after).toLocaleString()}. Until then,
            protected BOOKS actions are paused.
          </p>
        )}
        {state?.configured && (
          <form onSubmit={submit}>
            <fieldset disabled={loading}>
              {state.enrolled && !setup && (
                <label>
                  Action
                  <select
                    value={action}
                    onChange={(event) => {
                      setAction(event.currentTarget.value as Action);
                      if (proof.current) proof.current.value = "";
                    }}
                  >
                    <option value="CHALLENGE">Verify this session</option>
                    <option value="RECOVERY">Use a recovery code</option>
                    <option value="REGENERATE">Replace recovery codes</option>
                    {!state.reset_after && (
                      <option value="RESET_REQUEST">
                        Request removal (24-hour wait)
                      </option>
                    )}
                    {state.reset_after && (
                      <option value="RESET_CANCEL">Cancel removal</option>
                    )}
                    {state.reset_after && (
                      <option value="RESET_COMPLETE">
                        Complete removal after waiting
                      </option>
                    )}
                  </select>
                </label>
              )}
              {setup && (
                <>
                  <h3 ref={setupHeading} tabIndex={-1}>
                    Add a new entry in your authenticator app
                  </h3>
                  <p>
                    Enter this setup key, then type the six-digit code. The key
                    expires in ten minutes.
                  </p>
                  <code className="id-label">{setup}</code>
                </>
              )}
              {needsPassword && (
                <label>
                  Confirm your password
                  <input
                    type="password"
                    name="password"
                    autoComplete="current-password"
                    required
                  />
                </label>
              )}
              {(setup || state.enrolled) && (
                <label>
                  {action === "RECOVERY"
                    ? "Recovery code"
                    : "Six-digit authenticator code"}
                  <input
                    ref={proof}
                    name="proof"
                    required
                    inputMode={
                      action === "RECOVERY" || action.startsWith("RESET_")
                        ? "text"
                        : "numeric"
                    }
                    autoComplete={
                      action === "RECOVERY" || action.startsWith("RESET_")
                        ? "off"
                        : "one-time-code"
                    }
                    maxLength={
                      action === "RECOVERY" || action.startsWith("RESET_")
                        ? 26
                        : 6
                    }
                  />
                </label>
              )}
              <Button type="submit">
                {loading
                  ? "Working…"
                  : setup
                    ? "Confirm setup"
                    : state.enrolled
                      ? "Continue"
                      : "Start setup"}
              </Button>
            </fieldset>
          </form>
        )}
        {error && <p role="alert">{error}</p>}
        {message && <p role="status">{message}</p>}
        {codes.length > 0 && (
          <>
            <h3 ref={codesHeading} tabIndex={-1}>
              Save these one-time recovery codes
            </h3>
            <p>Store them privately. They will not be shown again.</p>
            <ul>
              {codes.map((code) => (
                <li key={code}>
                  <code>{code}</code>
                </li>
              ))}
            </ul>
            <Button type="button" onClick={() => setCodes([])}>
              Done
            </Button>
          </>
        )}
      </div>
    </section>
  );
}

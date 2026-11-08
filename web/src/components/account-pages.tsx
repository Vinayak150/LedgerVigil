"use client";
// Account lifecycle pages (ADR-027): email verification, password reset and invitation acceptance. Links carry the single-use
// token in the URL fragment, which browsers never send to a server; it is read once, removed from the address bar and history,
// kept only in memory, and sent in one POST. Answers never say whether an address has an account.
import { useEffect, useRef, useState, type FormEvent } from "react";
import { LoaderCircle, MailCheck } from "lucide-react";
import { api, offlineMessage } from "@/lib/api";
import { readFragmentToken } from "@/lib/account-token";
import { Button } from "./button";

type Kind = "verify" | "reset" | "invitation";
type Note = { tone: "ok" | "error"; text: string } | null;

const TITLES: Record<Kind, string> = {
  verify: "Confirm your email address",
  reset: "Reset your password",
  invitation: "Accept your invitation",
};

function problem(status: number, code?: string) {
  if (status === 429)
    return "Too many attempts. Please wait before trying again.";
  if (code === "TOKEN_INVALID")
    return "This link is not valid, was already used or has expired. Request a new one.";
  if (status === 422)
    return "Check what you entered. Passwords need 12 to 1024 characters.";
  return "The service could not complete this request. Please try again.";
}

export function AccountPage({ kind }: { kind: Kind }) {
  const token = useRef<string | null>(null);
  const [hasToken, setHasToken] = useState<boolean | null>(null);
  const [pending, setPending] = useState(false);
  const [note, setNote] = useState<Note>(null);
  const [done, setDone] = useState(false);
  const status = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    token.current = readFragmentToken();
    setHasToken(token.current !== null);
  }, []);
  useEffect(() => {
    if (note) status.current?.focus();
  }, [note]);
  async function requestLink(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const email = String(new FormData(e.currentTarget).get("email") ?? "");
    setPending(true);
    setNote(null);
    try {
      const r =
        kind === "reset"
          ? await api.POST("/api/v1/auth/password-reset", { body: { email } })
          : await api.POST("/api/v1/auth/email-verification", {
              body: { email },
            });
      setNote(
        r.response.status === 202
          ? {
              tone: "ok",
              text: "If this address belongs to an account that can use it, a message with a link is on its way. The link works once.",
            }
          : { tone: "error", text: problem(r.response.status) },
      );
    } catch {
      setNote({ tone: "error", text: offlineMessage });
    } finally {
      setPending(false);
    }
  }
  async function submitToken(e?: FormEvent<HTMLFormElement>) {
    e?.preventDefault();
    const value = token.current;
    if (!value) return;
    const form = e ? new FormData(e.currentTarget) : null;
    const password = String(form?.get("password") ?? ""),
      confirm = String(form?.get("confirm") ?? "");
    if (kind !== "verify" && password !== confirm) {
      setNote({ tone: "error", text: "The two passwords do not match." });
      return;
    }
    setPending(true);
    setNote(null);
    try {
      const r =
        kind === "verify"
          ? await api.POST("/api/v1/auth/email-verification/confirm", {
              body: { token: value },
            })
          : kind === "reset"
            ? await api.POST("/api/v1/auth/password-reset/confirm", {
                body: { token: value, new_password: password },
              })
            : await api.POST("/api/v1/auth/invitation/accept", {
                body: { token: value, password },
              });
      if (r.response.status === 200) {
        token.current = null; // single use: never resent
        setDone(true);
        setNote({
          tone: "ok",
          text:
            kind === "verify"
              ? "Your email address is confirmed. You can sign in now."
              : kind === "reset"
                ? "Your password was changed and every existing session was signed out. Sign in with the new password."
                : "Your account is ready. Sign in with the password you chose.",
        });
      } else {
        const code = (r.error as { code?: string } | undefined)?.code;
        if (code === "TOKEN_INVALID") token.current = null;
        setNote({ tone: "error", text: problem(r.response.status, code) });
      }
    } catch {
      setNote({
        tone: "error",
        text: "The result is unknown because the service could not be reached. Try signing in; if that fails, request a new link.",
      });
      token.current = null; // the outcome is ambiguous; a used token must not be resent automatically
    } finally {
      setPending(false);
    }
  }
  const requestForm =
    (kind === "verify" || kind === "reset") && hasToken === false;
  return (
    <main className="center-screen">
      <section className="picker" aria-labelledby="account-title">
        <h1 id="account-title">{TITLES[kind]}</h1>
        {hasToken === null ? (
          <p role="status">
            <LoaderCircle className="spin" size={16} /> Preparing…
          </p>
        ) : requestForm ? (
          <form className="login-form" onSubmit={requestLink}>
            <p className="muted">
              {kind === "reset"
                ? "Enter your email address and we will send a single-use link to choose a new password."
                : "Enter your email address and we will send a new confirmation link."}
            </p>
            <label htmlFor="account-email">Email address</label>
            <input
              id="account-email"
              name="email"
              type="email"
              autoComplete="username"
              required
              maxLength={320}
              disabled={pending}
            />
            <Button type="submit" disabled={pending}>
              {pending ? "Sending…" : "Send link"}
            </Button>
          </form>
        ) : hasToken === false ? (
          <p className="notice error" role="alert">
            This page needs the link from your invitation email. Ask your
            administrator to send a new invitation.
          </p>
        ) : done ? null : kind === "verify" ? (
          <div className="financial-actions">
            <Button
              type="button"
              onClick={() => void submitToken()}
              disabled={pending}
            >
              <MailCheck size={16} />
              {pending ? "Confirming…" : "Confirm my email address"}
            </Button>
          </div>
        ) : (
          <form className="login-form" onSubmit={(e) => void submitToken(e)}>
            <label htmlFor="new-password">New password</label>
            <input
              id="new-password"
              name="password"
              type="password"
              autoComplete="new-password"
              required
              minLength={12}
              maxLength={1024}
              aria-describedby="password-rule"
              disabled={pending}
            />
            <p id="password-rule" className="field-hint">
              At least 12 characters.
            </p>
            <label htmlFor="confirm-password">Confirm the password</label>
            <input
              id="confirm-password"
              name="confirm"
              type="password"
              autoComplete="new-password"
              required
              minLength={12}
              maxLength={1024}
              disabled={pending}
            />
            <Button type="submit" disabled={pending}>
              {pending
                ? "Saving…"
                : kind === "reset"
                  ? "Change password"
                  : "Create my account"}
            </Button>
          </form>
        )}
        {note && (
          <p
            ref={status}
            tabIndex={-1}
            className={note.tone === "error" ? "notice error" : "notice"}
            role={note.tone === "error" ? "alert" : "status"}
          >
            {note.text}
          </p>
        )}
        <a className="button button-secondary" href="/">
          Back to sign in
        </a>
      </section>
    </main>
  );
}

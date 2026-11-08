"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  offlineMessage,
  problemMessage,
  type Session,
  type Tenant,
} from "./api";
type State = {
  phase: "loading" | "anonymous" | "ready" | "error";
  session: Session | null;
  tenant: Tenant | null;
  error: string;
  retryAt: number;
};
const initial: State = {
  phase: "loading",
  session: null,
  tenant: null,
  error: "",
  retryAt: 0,
};
export function useSession() {
  const [state, setState] = useState<State>(initial);
  const generation = useRef(0),
    sessionRef = useRef<Session | null>(null),
    busy = useRef(false);
  const channel = useRef<BroadcastChannel | null>(null);
  const refreshing = useRef(0);
  const clear = useCallback((phase: State["phase"], error = "") => {
    sessionRef.current = null;
    setState({ ...initial, phase, error });
  }, []);
  const accept = useCallback(
    async (session: Session, version: number) => {
      let tenant: Tenant | null = null;
      if (session.tenant && session.permissions.includes("read")) {
        const result = await api.GET("/api/v1/tenants/current");
        if (version !== generation.current) return;
        if (!result.data) {
          clear(
            result.response.status === 401 ? "anonymous" : "error",
            problemMessage(result.response),
          );
          return;
        }
        if (result.data.id !== session.tenant.id) {
          clear(
            "error",
            "Your organization changed. Refresh your session to continue.",
          );
          return;
        }
        tenant = result.data;
      }
      if (version !== generation.current) return;
      sessionRef.current = session;
      setState({ ...initial, phase: "ready", session, tenant });
    },
    [clear],
  );
  const refresh = useCallback(
    async (preserveCurrent = false) => {
      if (busy.current || refreshing.current) return;
      const prior = sessionRef.current;
      const preserve = preserveCurrent && prior !== null;
      const version = ++generation.current;
      refreshing.current = version;
      if (!preserve) {
        busy.current = true;
        clear("loading");
      }
      try {
        const result = await api.GET("/api/v1/me");
        if (version !== generation.current) return;
        if (result.data) {
          if (
            preserve &&
            (result.data.user.id !== prior?.user.id ||
              result.data.tenant?.id !== prior?.tenant?.id)
          )
            clear("loading");
          await accept(result.data, version);
        } else
          clear(
            result.response.status === 401 ? "anonymous" : "error",
            result.response.status === 401
              ? ""
              : problemMessage(result.response),
          );
      } catch {
        if (version === generation.current) clear("error", offlineMessage);
      } finally {
        if (refreshing.current === version) refreshing.current = 0;
        if (!preserve && version === generation.current) busy.current = false;
      }
    },
    [accept, clear],
  );
  useEffect(() => {
    void refresh();
    channel.current = new BroadcastChannel("ledgervigil-session");
    const changed = () => {
      ++generation.current;
      clear("loading");
      busy.current = false;
      refreshing.current = 0;
      void refresh();
    };
    channel.current.onmessage = changed;
    const focus = () => {
      if (sessionRef.current) void refresh(true);
    };
    const visible = () => {
      if (document.visibilityState === "visible") focus();
    };
    window.addEventListener("focus", focus);
    document.addEventListener("visibilitychange", visible);
    return () => {
      channel.current?.close();
      window.removeEventListener("focus", focus);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [clear, refresh]);
  useEffect(() => {
    if (!state.session) return;
    const expiration = Math.min(
      Date.parse(state.session.idle_expires_at),
      Date.parse(state.session.absolute_expires_at),
    );
    const timer = setTimeout(
      () => {
        ++generation.current;
        clear("anonymous", "Your session has ended. Please sign in again.");
      },
      Math.max(0, expiration - Date.now()),
    );
    return () => clearTimeout(timer);
  }, [state.session, clear]);
  async function login(email: string, password: string) {
    if (busy.current) return;
    busy.current = true;
    const version = ++generation.current;
    setState({ ...initial, phase: "anonymous" });
    try {
      const result = await api.POST("/api/v1/auth/login", {
        body: { email, password },
      });
      if (version !== generation.current) return;
      if (result.data) {
        channel.current?.postMessage("changed");
        await accept(result.data, version);
      } else {
        const wait = Number(result.response.headers.get("Retry-After"));
        setState({
          ...initial,
          phase: "anonymous",
          error: problemMessage(result.response, true),
          retryAt:
            result.response.status === 429
              ? Date.now() +
                (Number.isFinite(wait) && wait > 0 ? wait : 60) * 1000
              : 0,
        });
      }
    } catch {
      if (version === generation.current) clear("anonymous", offlineMessage);
    } finally {
      if (version === generation.current) busy.current = false;
    }
  }
  async function switchTenant(tenantId: string) {
    const session = sessionRef.current;
    if (!session || busy.current) return;
    busy.current = true;
    const version = ++generation.current;
    clear("loading"); // Never retain a previous tenant's data while changing context.
    try {
      const result = await api.POST("/api/v1/auth/switch-tenant", {
        body: { tenant_id: tenantId },
        headers: { "X-CSRF-Token": session.csrf_token },
      });
      if (version !== generation.current) return;
      if (result.data) {
        channel.current?.postMessage("changed");
        await accept(result.data, version);
      } else
        clear(
          result.response.status === 401 ? "anonymous" : "error",
          problemMessage(result.response),
        );
    } catch {
      if (version === generation.current) clear("error", offlineMessage);
    } finally {
      if (version === generation.current) busy.current = false;
    }
  }
  async function logout() {
    const session = sessionRef.current;
    if (!session || busy.current) return;
    busy.current = true;
    const version = ++generation.current;
    clear("loading");
    try {
      const result = await api.POST("/api/v1/auth/logout", {
        headers: { "X-CSRF-Token": session.csrf_token },
      });
      if (version !== generation.current) return;
      if (result.response.status === 204 || result.response.status === 401) {
        clear("anonymous");
        channel.current?.postMessage("changed");
      } else
        clear(
          "error",
          "We couldn’t confirm sign-out. Refresh your session and try again.",
        );
    } catch {
      if (version === generation.current)
        clear(
          "error",
          "We couldn’t confirm sign-out. Reconnect, then refresh your session and try again.",
        );
    } finally {
      if (version === generation.current) busy.current = false;
    }
  }
  return { ...state, login, switchTenant, logout, refresh };
}

"use client";
import { Authenticator } from "./authenticator";
import { canAuthorOverride } from "@/lib/override-recovery";
import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  Check,
  ChevronDown,
  ChevronRight,
  Compass,
  FileCheck2,
  FileSearch,
  Layers3,
  LayoutDashboard,
  LoaderCircle,
  LockKeyhole,
  LogOut,
  Menu,
  Search,
  Settings2,
  ShieldCheck,
  X,
  Building2,
  Activity,
  Eye,
  EyeOff,
} from "lucide-react";
import { Button } from "./button";
import { ProposalBrowser } from "./proposal-browser";
import { FinancialOverview } from "./financial-overview";
import { DetectionReview } from "./detection-review";
import { ProviderConnectionPanel } from "./provider-connection";
import {
  AccountSecurity,
  AuditLogPanel,
  MembersPanel,
} from "./organization-admin";
import { TransactionExplorer } from "./transaction-explorer";
import { WorkspaceSearch } from "./workspace-search";
import { OrganizationPicker } from "./organization-picker";
import { permissionLabels } from "@/lib/permission-labels";
import { useSession } from "@/lib/use-session";
import type { Session } from "@/lib/api";
const spaces = [
  {
    id: "overview",
    name: "Financial Command Center",
    short: "Command center",
    icon: LayoutDashboard,
    eyebrow: "WORKSPACE OVERVIEW",
    title: "A clearer view of your finances.",
    description:
      "Investigate discrepancies, verify the evidence, and keep every financial decision accountable.",
    requires:
      "Financial summaries will appear when ledger and balance services are connected.",
  },
  {
    id: "transactions",
    name: "Transaction Explorer",
    short: "Transactions",
    icon: Search,
    eyebrow: "EXPLORE",
    title: "Follow every transaction.",
    description:
      "Inspect current documents and the source evidence behind them.",
    requires:
      "Transaction search and document history are not available in this release.",
  },
  {
    id: "investigations",
    name: "Investigation Workbench",
    short: "Investigations",
    icon: Compass,
    eyebrow: "INVESTIGATE",
    title: "From a finding to its evidence.",
    description:
      "Review deterministic detection findings against the statements, payments and settlements they cite.",
    requires:
      "Detection findings appear here when detection is enabled for this server.",
  },
  {
    id: "simulator",
    name: "Shadow Ledger Simulator",
    short: "Shadow ledger",
    icon: Layers3,
    eyebrow: "SIMULATE",
    title: "Understand the impact first.",
    description:
      "Review the proposed accounting impact before a change reaches your ledger.",
    requires:
      "Pinned settlement simulations are available with their submitted proposals in Verification & approvals. A standalone what-if simulator is unavailable.",
  },
  {
    id: "approvals",
    name: "Verification and Approvals",
    short: "Verification & approvals",
    icon: FileCheck2,
    eyebrow: "VERIFY",
    title: "Confidence before commitment.",
    description:
      "Review evidence, inspect proposed changes, and maintain separation of duties.",
    requires:
      "Drafts, pinned simulations, independent decisions and deliberate settlement execution are available within the supported native SANDBOX flow.",
  },
  {
    id: "audit",
    name: "Regression Testing and Audit",
    short: "Testing & audit",
    icon: ShieldCheck,
    eyebrow: "TRACE",
    title: "An accountable trail.",
    description:
      "Examine regression results, review provenance, and trace the decisions behind each change.",
    requires:
      "Audit history and evidence exports are not available in this release.",
  },
  {
    id: "settings",
    name: "Organization and Integration Settings",
    short: "Organization & settings",
    icon: Settings2,
    eyebrow: "ORGANIZATION",
    title: "Your workspace, in context.",
    description:
      "Review your active organization, account and assigned access.",
    requires: "",
  },
];
function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark">
        <Layers3 size={23} strokeWidth={1.8} />
      </span>
      <span>
        Ledger<span className="brand-light">Vigil</span>
        <small>FINANCIAL CONTROL WORKSPACE</small>
      </span>
    </div>
  );
}
function Sandbox() {
  return (
    <span className="sandbox">
      <span />
      Sandbox
    </span>
  );
}
function Message({ children }: { children: React.ReactNode }) {
  return (
    <div className="notice error" role="alert">
      {children}
    </div>
  );
}
function Login({ auth }: { auth: ReturnType<typeof useSession> }) {
  const [pending, setPending] = useState(false),
    [now, setNow] = useState(() => Date.now());
  const formRef = useRef<HTMLFormElement>(null);
  const [showPassword, setShowPassword] = useState(false);
  const [capsLock, setCapsLock] = useState(false);
  useEffect(() => {
    if (!auth.retryAt) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [auth.retryAt]);
  const remaining = Math.max(0, Math.ceil((auth.retryAt - now) / 1000));
  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (pending || remaining) return;
    const data = new FormData(e.currentTarget);
    setPending(true);
    setShowPassword(false);
    try {
      await auth.login(
        String(data.get("email")).trim(),
        String(data.get("password")),
      );
    } finally {
      const field = formRef.current?.elements.namedItem("password");
      if (field instanceof HTMLInputElement) field.value = "";
      setPending(false);
    }
  }
  return (
    <main className="login-layout">
      <section className="login-story">
        <Brand />
        <div className="story-content">
          <div className="eyebrow pale">CLARITY. CONTROL. CONFIDENCE.</div>
          <h1>
            Every decision.
            <br />
            Grounded in
            <br />
            <em>evidence.</em>
          </h1>
          <p>
            A dedicated workspace for investigating discrepancies and
            understanding financial change.
          </p>
          <div className="flow-preview" aria-label="Financial review workflow">
            <div>
              <FileSearch />
              <span>Investigate</span>
              <small>Understand the evidence</small>
            </div>
            <ChevronRight />
            <div>
              <Layers3 />
              <span>Simulate</span>
              <small>Inspect the impact</small>
            </div>
            <ChevronRight />
            <div>
              <ShieldCheck />
              <span>Verify</span>
              <small>Review before action</small>
            </div>
          </div>
        </div>
        <div className="story-footer">
          <span className="dot" />
          Designed for accountable financial work
        </div>
      </section>
      <section className="login-panel">
        <div className="login-top">
          <span>LEDGERVIGIL / WORKSPACE</span>
          <Sandbox />
        </div>
        <div className="login-form">
          <div className="section-icon">
            <LockKeyhole size={23} />
          </div>
          <p className="eyebrow">WELCOME BACK</p>
          <h2>Sign in to your workspace</h2>
          <p className="muted">
            Use your assigned account to continue securely.
          </p>
          {auth.error && <Message>{auth.error}</Message>}
          <form onSubmit={submit} ref={formRef}>
            <label htmlFor="email">Email address</label>
            <input
              id="email"
              name="email"
              type="email"
              autoComplete="username"
              placeholder="you@organization.com"
              required
              maxLength={320}
              disabled={pending}
            />
            <label htmlFor="password">Password</label>
            <div className="password-field">
              <input
                id="password"
                name="password"
                type={showPassword ? "text" : "password"}
                autoComplete="current-password"
                placeholder="Enter your password"
                required
                maxLength={1024}
                aria-describedby={capsLock ? "caps-lock-hint" : undefined}
                onKeyUp={(e) => setCapsLock(e.getModifierState("CapsLock"))}
                onKeyDown={(e) => setCapsLock(e.getModifierState("CapsLock"))}
                onBlur={() => setCapsLock(false)}
                disabled={pending}
              />
              <button
                type="button"
                className="icon-button password-toggle"
                disabled={pending}
                aria-label={showPassword ? "Hide password" : "Show password"}
                aria-pressed={showPassword}
                onClick={() => setShowPassword((v) => !v)}
              >
                {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
              </button>
            </div>
            {capsLock && (
              <p id="caps-lock-hint" className="field-hint" role="status">
                Caps Lock is on.
              </p>
            )}
            <Button
              className="login-submit"
              disabled={pending || remaining > 0}
              type="submit"
            >
              {pending ? (
                <>
                  <LoaderCircle className="spin" size={17} />
                  Signing in…
                </>
              ) : remaining > 0 ? (
                `Try again in ${remaining}s`
              ) : (
                <>
                  Sign in
                  <ArrowRight size={17} />
                </>
              )}
            </Button>
          </form>
          <div className="account-help">
            <LockKeyhole size={15} />
            <span>
              Need access? Contact your workspace administrator.{" "}
              <a href="/auth/reset">Forgot your password?</a>{" "}
              <a href="/auth/verify">Resend the confirmation email</a>
            </span>
          </div>
          <div className="sandbox-note">
            <strong>Sandbox environment</strong>
            <p>
              Synthetic accounts only. This workspace is not enabled for
              production financial activity.
            </p>
          </div>
        </div>
        <footer className="login-footer">
          LedgerVigil<span>Evidence before action.</span>
        </footer>
      </section>
    </main>
  );
}
function Access({
  session,
  tenantName,
}: {
  session: Session;
  tenantName: string;
}) {
  return (
    <>
      <div className="settings-grid">
        <section className="card">
          <div className="card-heading">
            <Building2 size={19} />
            <h2>Active organization</h2>
            <span className="live-tag">Connected</span>
          </div>
          <div className="card-content">
            <h3>{tenantName}</h3>
            <code className="id-label">{session.tenant?.id}</code>
            <p className="muted small">
              All workspace access uses this organization’s context.
            </p>
          </div>
        </section>
        <section className="card">
          <div className="card-heading">
            <ShieldCheck size={19} />
            <h2>Your account</h2>
          </div>
          <div className="card-content">
            <h3>{session.user.display_name}</h3>
            <p className="muted">{session.user.email}</p>
            <div className="role-list">
              {session.roles.map((role) => (
                <span className="role" key={role}>
                  {role.toLowerCase().replaceAll("_", " ")}
                </span>
              ))}
            </div>
          </div>
        </section>
      </div>
      <section className="card permissions">
        <div className="card-heading">
          <LockKeyhole size={19} />
          <h2>Assigned permissions</h2>
          <span className="muted small">For this organization</span>
        </div>
        <div className="permission-list">
          {session.permissions.map((p) => (
            <div key={p}>
              <Check size={15} />
              <span>
                <strong>{permissionLabels[p]?.title ?? p}</strong>
                <p>
                  {permissionLabels[p]?.description ??
                    "Assigned by your organization."}
                </p>
                <code>{p}</code>
              </span>
            </div>
          ))}
        </div>
        <div className="card-foot">
          Assigned access does not enable workflows that are not yet available.
          Every action is verified by the service.
        </div>
      </section>
    </>
  );
}
export function Workspace() {
  const auth = useSession(),
    [active, setActive] = useState("overview"),
    [mobile, setMobile] = useState(false),
    [choosing, setChoosing] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const sidebar = useRef<HTMLElement>(null),
    menuButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!mobile) return;
    const panel = sidebar.current;
    const controls = () =>
      Array.from(
        panel?.querySelectorAll<HTMLElement>(
          "a[href],button:not([disabled])",
        ) ?? [],
      ).filter(
        (el) =>
          el.getClientRects().length &&
          getComputedStyle(el).visibility !== "hidden",
      );
    controls()[0]?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setMobile(false);
        requestAnimationFrame(() => menuButton.current?.focus());
      }
      if (event.key === "Tab") {
        const items = controls(),
          first = items[0],
          last = items.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last?.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first?.focus();
        }
      }
    };
    document.addEventListener("keydown", key);
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const media = window.matchMedia("(min-width:851px)");
    const resize = () => {
      if (media.matches) setMobile(false);
    };
    media.addEventListener("change", resize);
    return () => {
      document.removeEventListener("keydown", key);
      document.body.style.overflow = previous;
      media.removeEventListener("change", resize);
    };
  }, [mobile]);
  useEffect(() => {
    const sync = () => {
      const key = window.location.hash.slice(1);
      if (spaces.some((s) => s.id === key)) setActive(key);
      else if (!key) setActive("overview");
    };
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  useEffect(() => {
    setChoosing(false);
    setMobile(false);
  }, [auth.session?.tenant?.id]);
  if (auth.phase === "loading")
    return (
      <main className="center-screen">
        <Brand />
        <div className="loading-state" role="status">
          <LoaderCircle className="spin" />
          <h1>Opening your workspace</h1>
          <p>Checking your session and organization access…</p>
        </div>
      </main>
    );
  if (auth.phase === "error")
    return (
      <main className="center-screen">
        <Brand />
        <section className="picker">
          <h1>Let’s reconnect</h1>
          <Message>{auth.error}</Message>
          <Button onClick={() => void auth.refresh()}>
            Refresh session
            <ArrowRight size={16} />
          </Button>
        </section>
      </main>
    );
  if (!auth.session) return <Login auth={auth} />;
  const session = auth.session;
  if (session.tenant_selection_required || !session.tenant || choosing)
    return (
      <main className="center-screen">
        <Brand />
        <OrganizationPicker
          session={session}
          currentName={auth.tenant?.name}
          onCancel={
            session.tenant
              ? () => {
                  setChoosing(false);
                  requestAnimationFrame(() => heading.current?.focus());
                }
              : undefined
          }
          onSelect={(id) => {
            setChoosing(false);
            void auth.switchTenant(id);
          }}
          onLogout={() => void auth.logout()}
        />
      </main>
    );
  const space = spaces.find((s) => s.id === active) ?? spaces[0],
    tenantName = auth.tenant?.name ?? "Selected organization";
  function navigate(id: string) {
    setActive(id);
    setMobile(false);
    requestAnimationFrame(() => heading.current?.focus());
  }
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to content
      </a>
      <aside
        ref={sidebar}
        id="workspace-navigation"
        className={`sidebar ${mobile ? "is-open" : ""}`}
      >
        <div className="sidebar-brand">
          <Brand />
          <button
            className="icon-button mobile-close"
            aria-label="Close navigation"
            onClick={() => {
              setMobile(false);
              requestAnimationFrame(() => menuButton.current?.focus());
            }}
          >
            <X size={20} />
          </button>
        </div>
        <button
          className="organization-switch"
          title={tenantName}
          onClick={() => setChoosing(true)}
        >
          <span className="org-avatar">
            <Building2 size={20} />
          </span>
          <span className="org-label">
            <strong>{tenantName}</strong>
            <small>Organization workspace</small>
          </span>
          <ChevronDown size={16} />
        </button>
        <p className="nav-label">WORKSPACE</p>
        <nav aria-label="Workspaces">
          {spaces.slice(0, 6).map((s) => (
            <a
              key={s.id}
              href={`#${s.id}`}
              className={`nav-item ${active === s.id ? "active" : ""}`}
              onClick={() => navigate(s.id)}
              aria-current={active === s.id ? "page" : undefined}
              aria-label={s.name}
            >
              <s.icon size={18} />
              <span>{s.short}</span>
              {active === s.id && <span className="nav-active-dot" />}
            </a>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="workspace-note">
            <span className="dot" />
            <span>
              Sandbox workspace<small>Not accounting records</small>
            </span>
          </div>
          <a
            className={`nav-item ${active === "settings" ? "active" : ""}`}
            href="#settings"
            onClick={() => navigate("settings")}
            aria-label="Organization and Integration Settings"
            aria-current={active === "settings" ? "page" : undefined}
          >
            <Settings2 size={18} />
            <span>Organization & settings</span>
          </a>
          <div className="user-block">
            <span className="user-avatar">
              {session.user.display_name.slice(0, 2).toUpperCase()}
            </span>
            <span>
              <strong>{session.user.display_name}</strong>
              <small>
                {session.roles[0]?.toLowerCase().replaceAll("_", " ") ??
                  "Member"}
              </small>
            </span>
            <button
              className="icon-button"
              aria-label="Sign out"
              title="Sign out"
              onClick={() => void auth.logout()}
            >
              <LogOut size={17} />
            </button>
          </div>
        </div>
      </aside>
      {mobile && (
        <button
          className="nav-backdrop"
          aria-label="Close navigation"
          onClick={() => {
            setMobile(false);
            requestAnimationFrame(() => menuButton.current?.focus());
          }}
        />
      )}
      <div className="main-area" inert={mobile || undefined}>
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-button mobile-menu"
              ref={menuButton}
              aria-label="Open navigation"
              aria-controls="workspace-navigation"
              aria-expanded={mobile}
              onClick={() => setMobile(true)}
            >
              <Menu size={21} />
            </button>
            <span>Workspace</span>
            <ChevronRight size={13} />
            <strong>{space.short}</strong>
          </div>
          <div className="topbar-right">
            <WorkspaceSearch
              destinations={spaces}
              onNavigate={navigate}
              disabled={mobile}
            />
            <Sandbox />
            <span className="topbar-divider" />
            <ShieldCheck size={17} />
            <span className="small">Session verified</span>
          </div>
        </header>
        <main id="main-content" className="workspace-main" tabIndex={-1}>
          <div className="page-intro">
            <div>
              <div className="eyebrow">{space.eyebrow}</div>
              <h1 ref={heading} tabIndex={-1}>
                {space.title}
              </h1>
              <p>{space.description}</p>
            </div>
            {active === "overview" && (
              <a
                className="text-link"
                href="#settings"
                onClick={() => navigate("settings")}
              >
                Organization details
                <ArrowUpRight size={16} />
              </a>
            )}
          </div>
          {active === "settings" ? (
            <>
              <Access session={session} tenantName={tenantName} />
              <MembersPanel
                key={`members:${session.tenant.id}:${session.user.id}`}
                csrfToken={session.csrf_token}
                userId={session.user.id}
                canAdminister={session.permissions.includes("administer")}
                onSessionRefresh={() => void auth.refresh()}
              />
              <AccountSecurity
                csrfToken={session.csrf_token}
                onSignedOut={() => void auth.refresh()}
              />
              <Authenticator
                key={`mfa:${session.user.id}:${session.tenant.id}`}
                csrfToken={session.csrf_token}
              />
              <AuditLogPanel
                key={`audit:${session.tenant.id}:${session.user.id}`}
                canRead={session.permissions.includes("audit.read")}
              />
              <ProviderConnectionPanel
                key={session.tenant.id}
                csrfToken={session.csrf_token}
                canAdminister={session.permissions.includes("administer")}
                onSessionRefresh={() => void auth.refresh()}
              />
            </>
          ) : active === "investigations" ? (
            <DetectionReview
              key={`${session.tenant.id}:${session.user.id}`}
              tenantId={session.tenant.id}
              userId={session.user.id}
              csrfToken={session.csrf_token}
              permissions={session.permissions}
              onSessionRefresh={() => void auth.refresh()}
            />
          ) : active === "transactions" ? (
            <TransactionExplorer
              key={session.tenant.id}
              csrfToken={session.csrf_token}
              canImport={session.permissions.includes("source.import")}
              onSessionRefresh={() => void auth.refresh()}
            />
          ) : active === "approvals" ? (
            <ProposalBrowser
              key={session.tenant.id}
              userId={session.user.id}
              tenantId={session.tenant.id}
              csrfToken={session.csrf_token}
              canOverride={canAuthorOverride(
                session.roles,
                session.permissions,
              )}
              canPropose={session.permissions.includes("propose")}
              canDiscuss={
                session.permissions.includes("propose") ||
                session.permissions.includes("review")
              }
              onSessionRefresh={() => void auth.refresh()}
            />
          ) : (
            <>
              <div className="environment-banner">
                <div className="banner-icon">
                  <ShieldCheck size={21} />
                </div>
                <div>
                  <strong>A safe space to investigate.</strong>
                  <p>
                    SANDBOX — not accounting records. Native settlement
                    execution requires independent approval and explicit
                    confirmation.
                  </p>
                </div>
                <span className="outline-tag">SANDBOX</span>
              </div>
              {active === "overview" && (
                <section
                  className="context-summary"
                  aria-label="Your current workspace"
                >
                  <div className="context-organization">
                    <span className="context-symbol">
                      <Building2 size={24} />
                    </span>
                    <div>
                      <span className="eyebrow">ACTIVE ORGANIZATION</span>
                      <h2>{tenantName}</h2>
                      <p>
                        {session.user.display_name}
                        <span aria-hidden="true"> · </span>
                        {session.roles
                          .map((r) => r.toLowerCase().replaceAll("_", " "))
                          .join(", ")}
                      </p>
                    </div>
                  </div>
                  <Button variant="secondary" asChild>
                    <a href="#settings" onClick={() => navigate("settings")}>
                      Review your access
                      <ArrowUpRight size={16} />
                    </a>
                  </Button>
                </section>
              )}
              {active === "overview" ? (
                <FinancialOverview
                  key={session.tenant.id}
                  onSessionRefresh={() => void auth.refresh()}
                />
              ) : (
                <section className="card empty-card">
                  <div className="card-heading">
                    <space.icon size={19} />
                    <h2>
                      {active === "overview"
                        ? "Workspace activity"
                        : space.name}
                    </h2>
                    <span className="outline-tag">
                      {active === "simulator"
                        ? "Proposal-linked results"
                        : "Not available yet"}
                    </span>
                  </div>
                  <div className="empty-content" key={active}>
                    <div className="empty-state-icon" aria-hidden="true">
                      <space.icon size={30} strokeWidth={1.5} />
                    </div>
                    <h2>
                      {active === "overview"
                        ? "Your financial workspace starts here"
                        : active === "simulator"
                          ? "Inspect a proposal’s pinned prediction"
                          : "This workspace is not available yet"}
                    </h2>
                    <p>
                      {space.requires} Your organization and account access are
                      ready to review.
                    </p>
                    <Button variant="secondary" asChild>
                      <a
                        href={
                          active === "simulator" ? "#approvals" : "#settings"
                        }
                        onClick={() =>
                          navigate(
                            active === "simulator" ? "approvals" : "settings",
                          )
                        }
                      >
                        {active === "simulator"
                          ? "Open proposals & results"
                          : "Review organization access"}
                        <ArrowRight size={16} />
                      </a>
                    </Button>
                    <span className="small muted empty-caption">
                      No financial data has been loaded into this view.
                    </span>
                  </div>
                </section>
              )}
              {active === "overview" && (
                <section className="workflow-section">
                  <div className="section-title">
                    <h2>A deliberate path from evidence to action</h2>
                    <span>THE LEDGERVIGIL WORKFLOW</span>
                  </div>
                  <div className="workflow-grid">
                    {[
                      {
                        n: "01",
                        title: "Understand the evidence",
                        text: "Trace transactions and investigate discrepancies at their source.",
                        icon: FileSearch,
                        id: "transactions",
                      },
                      {
                        n: "02",
                        title: "Inspect the impact",
                        text: "Inspect the pinned prediction and policy verdicts for a submitted proposal.",
                        icon: Layers3,
                        id: "approvals",
                      },
                      {
                        n: "03",
                        title: "Verify before action",
                        text: "Keep review, approval and execution accountable and separate.",
                        icon: ShieldCheck,
                        id: "approvals",
                      },
                    ].map((w) => (
                      <a
                        key={w.n}
                        href={`#${w.id}`}
                        onClick={() => navigate(w.id)}
                        className="workflow-card"
                      >
                        <div>
                          <w.icon size={20} />
                          <span>{w.n}</span>
                        </div>
                        <h3>
                          {w.title}
                          <ArrowUpRight size={15} />
                        </h3>
                        <p>{w.text}</p>
                        <small>
                          {w.id === "transactions"
                            ? "Browse records and source evidence"
                            : w.id === "approvals"
                              ? w.n === "02"
                                ? "Inspect submitted results"
                                : "Create and inspect proposals"
                              : "Not yet available"}
                        </small>
                      </a>
                    ))}
                  </div>
                </section>
              )}
            </>
          )}
          <footer className="workspace-footer">
            <span>
              LedgerVigil<span className="footer-dot">·</span>Evidence before
              action.
            </span>
            <span>
              <LockKeyhole size={12} />
              Organization-scoped access
            </span>
          </footer>
        </main>
      </div>
    </div>
  );
}

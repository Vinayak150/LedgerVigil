"use client";
import { useEffect, useRef, useState } from "react";
import {
  BookOpen,
  RefreshCw,
  ArrowDownToLine,
  CalendarDays,
  ListTree,
  AlertCircle,
} from "lucide-react";
import { api } from "@/lib/api";
import type { components } from "@/lib/api.generated";
import { formatAmount, periodRange } from "@/lib/money-format";
import { Button } from "./button";

type Ledger = components["schemas"]["Ledger"];
type Trial = components["schemas"]["TrialBalance"];
type Accounts = components["schemas"]["AccountPage"];
type Periods = components["schemas"]["PeriodList"];
type Tab = "trial" | "accounts" | "periods";
function failure(status: number, error: unknown): string {
  const code =
    error && typeof error === "object" && "code" in error
      ? String(error.code)
      : "";
  if (status === 401)
    return "Your session has ended. Refresh your session to continue.";
  if (status === 403)
    return "You no longer have access to this financial view.";
  if (status === 404) return "This ledger is unavailable to your organization.";
  if (code === "CURSOR_MISMATCH")
    return "This page link has expired. Reload the list to start again.";
  if (status === 422)
    return "This financial view is unavailable for this ledger. No figures have been substituted.";
  return "We couldn’t load this view. No financial figures are shown. Try again.";
}
export function FinancialOverview({
  onSessionRefresh,
}: {
  onSessionRefresh: () => void;
}) {
  const [ledgers, setLedgers] = useState<Ledger[]>([]),
    [cursor, setCursor] = useState<string | null>(null);
  const [ledgerId, setLedgerId] = useState("");
  const [listLoading, setListLoading] = useState(true),
    [listError, setListError] = useState("");
  const [tab, setTab] = useState<Tab>("trial"),
    [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  const [trial, setTrial] = useState<Trial | null>(null),
    [accounts, setAccounts] = useState<Accounts | null>(null),
    [periods, setPeriods] = useState<Periods | null>(null);
  const [revision, setRevision] = useState(0),
    [listRevision, setListRevision] = useState(0),
    [moreBusy, setMoreBusy] = useState(false);
  const moreController = useRef<AbortController | null>(null);
  const requestNumber = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    setListLoading(true);
    setListError("");
    setLedgers([]);
    setLedgerId("");
    setCursor(null);
    (async () => {
      try {
        const r = await api.GET("/api/v1/ledgers", {
          signal: controller.signal,
          params: { query: { limit: "50" } },
        });
        if (controller.signal.aborted) return;
        if (!r.data) {
          setListError(failure(r.response.status, r.error));
          return;
        }
        setLedgers(r.data.items);
        setCursor(r.data.next_cursor);
        setLedgerId(r.data.items[0]?.id ?? "");
      } catch {
        if (!controller.signal.aborted)
          setListError(
            "We couldn’t reach LedgerVigil. Check your connection and retry.",
          );
      } finally {
        if (!controller.signal.aborted) setListLoading(false);
      }
    })();
    return () => controller.abort();
  }, [listRevision]);
  useEffect(() => {
    const controller = new AbortController(),
      current = ++requestNumber.current;
    moreController.current?.abort();
    setMoreBusy(false);
    setTrial(null);
    setAccounts(null);
    setPeriods(null);
    setError("");
    if (!ledgerId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    (async () => {
      try {
        const options = {
          signal: controller.signal,
          params: { path: { ledger_id: ledgerId } },
        };
        if (tab === "trial") {
          const r = await api.GET(
            "/api/v1/ledgers/{ledger_id}/trial-balance",
            options,
          );
          if (current !== requestNumber.current || controller.signal.aborted)
            return;
          if (r.data) setTrial(r.data);
          else setError(failure(r.response.status, r.error));
        } else if (tab === "accounts") {
          const r = await api.GET(
            "/api/v1/ledgers/{ledger_id}/accounts",
            options,
          );
          if (current !== requestNumber.current || controller.signal.aborted)
            return;
          if (r.data) setAccounts(r.data);
          else setError(failure(r.response.status, r.error));
        } else {
          const r = await api.GET(
            "/api/v1/ledgers/{ledger_id}/periods",
            options,
          );
          if (current !== requestNumber.current || controller.signal.aborted)
            return;
          if (r.data) setPeriods(r.data);
          else setError(failure(r.response.status, r.error));
        }
      } catch {
        if (!controller.signal.aborted)
          setError(
            "We couldn’t reach LedgerVigil. Retry to load a fresh view.",
          );
      } finally {
        if (!controller.signal.aborted && current === requestNumber.current)
          setLoading(false);
      }
    })();
    return () => {
      controller.abort();
      moreController.current?.abort();
    };
  }, [ledgerId, tab, revision]);
  async function more(kind: "ledgers" | "accounts" | "periods") {
    if (moreBusy) return;
    const controller = new AbortController();
    moreController.current = controller;
    const current = requestNumber.current;
    setMoreBusy(true);
    try {
      if (kind === "ledgers" && cursor) {
        const r = await api.GET("/api/v1/ledgers", {
          signal: controller.signal,
          params: { query: { cursor } },
        });
        if (controller.signal.aborted) return;
        if (!r.data) {
          setListError(failure(r.response.status, r.error));
          return;
        }
        setLedgers((old) => [
          ...old,
          ...r.data.items.filter((n) => !old.some((o) => o.id === n.id)),
        ]);
        setCursor(r.data.next_cursor);
      } else if (kind === "accounts" && accounts?.next_cursor) {
        const r = await api.GET("/api/v1/ledgers/{ledger_id}/accounts", {
          signal: controller.signal,
          params: {
            path: { ledger_id: ledgerId },
            query: { cursor: accounts.next_cursor },
          },
        });
        if (controller.signal.aborted || current !== requestNumber.current)
          return;
        if (!r.data) {
          setAccounts(null);
          setError(failure(r.response.status, r.error));
          return;
        }
        setAccounts((old) =>
          old
            ? {
                ...r.data,
                items: [
                  ...old.items,
                  ...r.data.items.filter(
                    (n) => !old.items.some((o) => o.id === n.id),
                  ),
                ],
              }
            : null,
        );
      } else if (kind === "periods" && periods?.next_cursor) {
        const r = await api.GET("/api/v1/ledgers/{ledger_id}/periods", {
          signal: controller.signal,
          params: {
            path: { ledger_id: ledgerId },
            query: { cursor: periods.next_cursor },
          },
        });
        if (controller.signal.aborted || current !== requestNumber.current)
          return;
        if (!r.data) {
          setPeriods(null);
          setError(failure(r.response.status, r.error));
          return;
        }
        setPeriods((old) =>
          old
            ? {
                ...r.data,
                items: [
                  ...old.items,
                  ...r.data.items.filter(
                    (n) => !old.items.some((o) => o.id === n.id),
                  ),
                ],
              }
            : null,
        );
      }
    } catch {
      if (!controller.signal.aborted) {
        setTrial(null);
        setAccounts(null);
        setPeriods(null);
        setError(
          "The next page could not be loaded. Reload this view to retry.",
        );
      }
    } finally {
      if (!controller.signal.aborted) setMoreBusy(false);
    }
  }
  const ledger = ledgers.find((l) => l.id === ledgerId);
  return (
    <section
      className="financial-workspace"
      aria-label="Ledger financial overview"
    >
      <div className="ledger-toolbar">
        <div className="ledger-choice">
          <label htmlFor="active-ledger">Active ledger</label>
          <select
            id="active-ledger"
            value={ledgerId}
            disabled={listLoading || !ledgers.length}
            onChange={(e) => setLedgerId(e.target.value)}
          >
            <option value="" disabled>
              {listLoading ? "Loading ledgers…" : "Select a ledger"}
            </option>
            {ledgers.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name} · {l.functional_currency} · {l.id.slice(-6)}
              </option>
            ))}
          </select>
        </div>
        {cursor && (
          <Button
            variant="secondary"
            disabled={moreBusy}
            onClick={() => void more("ledgers")}
          >
            Load more ledgers
          </Button>
        )}
        <Button
          variant="secondary"
          disabled={listLoading || loading}
          onClick={() =>
            ledgerId ? setRevision((n) => n + 1) : setListRevision((n) => n + 1)
          }
        >
          <RefreshCw size={15} />
          Refresh view
        </Button>
      </div>
      {listError && (
        <div className="notice error" role="alert">
          <AlertCircle size={18} />
          <span>{listError}</span>
          <Button
            variant="secondary"
            onClick={() => setListRevision((n) => n + 1)}
          >
            Reload ledgers
          </Button>
        </div>
      )}
      {listLoading && (
        <p role="status" className="financial-status">
          Loading your organization’s ledgers…
        </p>
      )}
      {!listLoading && !listError && !ledgers.length && (
        <div className="card financial-empty">
          <BookOpen size={30} />
          <h2>No ledgers in this organization</h2>
          <p>
            Your administrator can prepare a synthetic ledger. No financial
            records are available to display.
          </p>
        </div>
      )}
      {ledger && (
        <>
          <div className="ledger-meta">
            <span>
              <BookOpen size={15} />
              {ledger.purpose} · {ledger.source.replaceAll("_", " ")}
            </span>
            <span>{ledger.functional_currency}</span>
            <code title={ledger.id}>Ledger {ledger.id.slice(-8)}</code>
          </div>
          <div
            className="financial-tabs"
            role="group"
            aria-label="Financial views"
          >
            {(
              [
                ["trial", "Trial balance", ArrowDownToLine],
                ["accounts", "Chart of accounts", ListTree],
                ["periods", "Periods", CalendarDays],
              ] as const
            ).map(([id, label, Icon]) => (
              <button
                key={id}
                type="button"
                aria-pressed={tab === id}
                onClick={() => setTab(id)}
              >
                <Icon size={17} />
                {label}
              </button>
            ))}
          </div>
          {loading && (
            <div className="card financial-status" role="status">
              Loading a verified financial view…
            </div>
          )}
          {error && (
            <div className="card financial-empty" role="alert">
              <AlertCircle size={26} />
              <h2>Financial view unavailable</h2>
              <p>{error}</p>
              <div className="financial-actions">
                <Button
                  variant="secondary"
                  onClick={() => setRevision((n) => n + 1)}
                >
                  Retry view
                </Button>
                <Button variant="ghost" onClick={onSessionRefresh}>
                  Refresh session
                </Button>
              </div>
            </div>
          )}
          {!loading &&
            !error &&
            trial?.ledger_id === ledgerId &&
            tab === "trial" &&
            trial && (
              <section className="card">
                <div className="card-heading">
                  <h2>Trial balance</h2>
                  <span className="outline-tag">
                    Sequence {trial.as_of_seq}
                  </span>
                </div>
                <div className="financial-disclosure">
                  <span className="outline-tag">
                    Accounting review: {trial.accounting_status}
                  </span>
                  <p>
                    {trial.data_label}. Figures describe sequence{" "}
                    {trial.as_of_seq}. Refresh to request the latest committed
                    view.
                  </p>
                </div>
                <div className="mobile-trial-totals" aria-label="Reported trial balance totals">
                  <div><span>Debit total · {trial.currency}</span><strong>{formatAmount(trial.total_debit.amount, trial.currency)}</strong></div>
                  <div><span>Credit total · {trial.currency}</span><strong>{formatAmount(trial.total_credit.amount, trial.currency)}</strong></div>
                </div>
                <p className="table-scroll-hint">Swipe or scroll the table to see all columns.</p>
                <div
                  className="table-scroll"
                  role="region"
                  aria-label="Trial balance table"
                  tabIndex={0}
                >
                  <table className="financial-table">
                    <caption className="sr-only">
                      Trial balance in {trial.currency}, sequence{" "}
                      {trial.as_of_seq}. Amounts and totals are supplied by the
                      ledger service.
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">Account</th>
                        <th scope="col">Type</th>
                        <th scope="col" className="money-column">
                          Debit · {trial.currency}
                        </th>
                        <th scope="col" className="money-column">
                          Credit · {trial.currency}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {trial.lines.map((l) => (
                        <tr key={l.account_id}>
                          <th scope="row">
                            <code>{l.code}</code>
                            <span>{l.name}</span>
                            {l.is_contra && <small>Contra account</small>}
                          </th>
                          <td>{l.type.toLowerCase()}</td>
                          <td className="money-column">
                            {formatAmount(l.debit.amount, l.debit.currency)}
                          </td>
                          <td className="money-column">
                            {formatAmount(l.credit.amount, l.credit.currency)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot>
                      <tr>
                        <th scope="row" colSpan={2}>
                          Total · {trial.currency}
                        </th>
                        <td className="money-column">
                          {formatAmount(
                            trial.total_debit.amount,
                            trial.currency,
                          )}
                        </td>
                        <td className="money-column">
                          {formatAmount(
                            trial.total_credit.amount,
                            trial.currency,
                          )}
                        </td>
                      </tr>
                    </tfoot>
                  </table>
                </div>
                {!trial.lines.length && (
                  <p className="financial-status">
                    No postings at this sequence. Both reported totals are zero.
                  </p>
                )}
                <div className="card-foot">
                  Debit and credit columns show net account balances. Totals
                  come directly from the API.
                </div>
              </section>
            )}
          {!loading &&
            !error &&
            accounts?.ledger_id === ledgerId &&
            tab === "accounts" &&
            accounts && (
              <section className="card">
                <div className="card-heading">
                  <h2>Chart of accounts</h2>
                  <span className="muted small">
                    {accounts.items.length} loaded
                  </span>
                </div>
                <div
                  className="table-scroll"
                  role="region"
                  aria-label="Chart of accounts table"
                  tabIndex={0}
                >
                  <table className="financial-table">
                    <caption className="sr-only">
                      Accounts in the selected ledger
                    </caption>
                    <thead>
                      <tr>
                        <th scope="col">Account</th>
                        <th scope="col">Type</th>
                        <th scope="col">Normal side</th>
                        <th scope="col">Status</th>
                      </tr>
                    </thead>
                    <tbody>
                      {accounts.items.map((a) => (
                        <tr key={a.id}>
                          <th scope="row">
                            <code>{a.code}</code>
                            <span>{a.name}</span>
                          </th>
                          <td>
                            {a.type.toLowerCase()}
                            {a.is_contra ? " · contra" : ""}
                          </td>
                          <td>{a.normal_side.toLowerCase()}</td>
                          <td>
                            <span className="account-status">
                              {a.active ? "Active" : "Inactive"}
                            </span>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {!accounts.items.length && (
                  <p className="financial-status">
                    No accounts in this ledger.
                  </p>
                )}
                {accounts.next_cursor && (
                  <div className="financial-status">
                    <Button
                      variant="secondary"
                      disabled={moreBusy}
                      onClick={() => void more("accounts")}
                    >
                      Load more accounts
                    </Button>
                  </div>
                )}
              </section>
            )}
          {!loading &&
            !error &&
            periods?.ledger_id === ledgerId &&
            tab === "periods" &&
            periods && (
              <section className="card">
                <div className="card-heading">
                  <h2>Accounting periods</h2>
                  <span className="muted small">
                    End dates shown inclusively
                  </span>
                </div>
                <div className="period-list">
                  {periods.items.map((p) => (
                    <div className="period-row" key={p.id}>
                      <CalendarDays size={19} />
                      <div>
                        <strong>{periodRange(p.start_date, p.end_date)}</strong>
                        <small>
                          {p.close_provenance === "LEGACY_UNAUDITED"
                            ? "Legacy close · unaudited"
                            : p.status === "CLOSED"
                              ? "Audited period close"
                              : "Available for posting"}
                        </small>
                      </div>
                      <span
                        className={`period-status ${p.status.toLowerCase()}`}
                      >
                        {p.status.toLowerCase()}
                      </span>
                    </div>
                  ))}
                </div>
                {!periods.items.length && (
                  <p className="financial-status">No periods in this ledger.</p>
                )}
                {periods.next_cursor && (
                  <div className="financial-status">
                    <Button
                      variant="secondary"
                      disabled={moreBusy}
                      onClick={() => void more("periods")}
                    >
                      Load more periods
                    </Button>
                  </div>
                )}
                <div className="card-foot">
                  Period status is read from the ledger. Closing controls are
                  not available in this view.
                </div>
              </section>
            )}
        </>
      )}
    </section>
  );
}

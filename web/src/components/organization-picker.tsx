"use client";
import { useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Building2,
  Check,
  LogOut,
  Search,
} from "lucide-react";
import type { Session } from "@/lib/api";
import { Button } from "./button";
export function OrganizationPicker({
  session,
  currentName,
  onSelect,
  onLogout,
  onCancel,
}: {
  session: Session;
  currentName?: string;
  onSelect: (id: string) => void;
  onLogout: () => void;
  onCancel?: () => void;
}) {
  const [query, setQuery] = useState("");
  const tenants = session.available_tenants.filter((t) =>
    (t.id + " " + (t.id === session.tenant?.id ? (currentName ?? "") : ""))
      .toLowerCase()
      .includes(query.toLowerCase().trim()),
  );
  return (
    <section className="picker organization-picker">
      <div className="picker-top">
        <div className="section-icon">
          <Building2 />
        </div>
        <span className="outline-tag">ORGANIZATION ACCESS</span>
      </div>
      <h1>Select an organization</h1>
      <p className="muted account-identity">
        Signed in as <strong>{session.user.email}</strong>
      </p>
      <p className="muted">
        Select the context for your work. Your permissions are checked
        separately for each organization.
      </p>
      {session.available_tenants.length > 1 && (
        <div className="organization-filter">
          <Search size={17} />
          <label className="sr-only" htmlFor="organization-query">
            Filter organizations by ID
          </label>
          <input
            id="organization-query"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Filter by organization ID"
            autoComplete="off"
          />
        </div>
      )}
      <div className="tenant-list">
        {tenants.map((t) => {
          const current = t.id === session.tenant?.id;
          return (
            <button
              key={t.id}
              className={`tenant-option ${current ? "current" : ""}`}
              onClick={() =>
                current && onCancel ? onCancel() : onSelect(t.id)
              }
            >
              <span className="tenant-icon">
                <Building2 size={21} />
              </span>
              <span>
                {current && currentName ? currentName : "Organization"}
                <code>{t.id}</code>
                {current && (
                  <small className="current-context">
                    <Check size={12} />
                    Current organization
                  </small>
                )}
              </span>
              <ArrowRight size={18} />
            </button>
          );
        })}
      </div>
      {!tenants.length && (
        <p role="status" className="search-empty">
          {session.available_tenants.length
            ? "No matching organization. Check the ID or clear your filter."
            : "No active organization membership. Contact your administrator."}
        </p>
      )}
      <p className="small muted">
        Other organizations are listed by verified ID. Names become available
        after selection.
      </p>
      <div className="picker-actions">
        {onCancel && (
          <Button variant="secondary" onClick={onCancel}>
            <ArrowLeft size={16} />
            Back to workspace
          </Button>
        )}
        <Button variant="ghost" onClick={onLogout}>
          <LogOut size={16} />
          Sign out
        </Button>
      </div>
    </section>
  );
}

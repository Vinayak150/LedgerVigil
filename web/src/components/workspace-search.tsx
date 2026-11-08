"use client";
import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, Search, X, type LucideIcon } from "lucide-react";
type Destination = {
  id: string;
  name: string;
  short: string;
  icon: LucideIcon;
};
export function WorkspaceSearch({
  destinations,
  onNavigate,
  disabled = false,
}: {
  destinations: Destination[];
  disabled?: boolean;
  onNavigate: (id: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null),
    input = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const results = destinations.filter((d) =>
    (d.name + " " + d.short).toLowerCase().includes(query.toLowerCase().trim()),
  );
  function open() {
    setQuery("");
    dialog.current?.showModal();
    input.current?.focus();
  }
  function choose(id: string) {
    dialog.current?.close();
    window.location.hash = id;
    onNavigate(id);
  }
  useEffect(() => {
    if (disabled) return;
    const key = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        if (document.querySelector("dialog[open]") && !dialog.current?.open)
          return;
        if (dialog.current?.open) dialog.current.close();
        else {
          setQuery("");
          dialog.current?.showModal();
          input.current?.focus();
        }
      }
    };
    document.addEventListener("keydown", key);
    return () => document.removeEventListener("keydown", key);
  }, [disabled]);
  return (
    <>
      <button
        className="workspace-search-trigger"
        onClick={open}
        aria-label="Find a workspace"
        aria-haspopup="dialog"
        disabled={disabled}
      >
        <Search size={17} />
        <span>Find a workspace</span>
        <kbd>⌘ / Ctrl K</kbd>
      </button>
      <dialog
        className="workspace-search-dialog"
        ref={dialog}
        aria-labelledby="workspace-search-title"
        onClick={(e) => {
          if (e.target === e.currentTarget) {
            const r = e.currentTarget.getBoundingClientRect();
            if (
              e.clientX < r.left ||
              e.clientX > r.right ||
              e.clientY < r.top ||
              e.clientY > r.bottom
            )
              e.currentTarget.close();
          }
        }}
      >
        <div className="search-dialog-heading">
          <h2 id="workspace-search-title">Go to workspace</h2>
          <button
            className="icon-button"
            aria-label="Close workspace search"
            onClick={() => dialog.current?.close()}
          >
            <X size={18} />
          </button>
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (results[0]) choose(results[0].id);
          }}
        >
          <label className="sr-only" htmlFor="workspace-query">
            Search workspaces
          </label>
          <div className="workspace-search-input">
            <Search size={19} />
            <input
              id="workspace-query"
              ref={input}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search workspaces…"
              autoComplete="off"
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") {
                  e.preventDefault();
                  dialog.current
                    ?.querySelector<HTMLButtonElement>(".workspace-result")
                    ?.focus();
                }
              }}
            />
          </div>
        </form>
        <div className="workspace-results" aria-label="Matching workspaces">
          {results.map((d, i) => (
            <button
              key={d.id}
              className="workspace-result"
              onClick={() => choose(d.id)}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                  e.preventDefault();
                  const buttons =
                    dialog.current?.querySelectorAll<HTMLButtonElement>(
                      ".workspace-result",
                    );
                  if (e.key === "ArrowUp" && i === 0) input.current?.focus();
                  else
                    buttons?.[
                      Math.max(
                        0,
                        Math.min(
                          results.length - 1,
                          i + (e.key === "ArrowDown" ? 1 : -1),
                        ),
                      )
                    ]?.focus();
                }
              }}
            >
              <d.icon size={19} />
              <span>
                {d.name}
                <small>
                  {d.id === "settings"
                    ? "Your organization and assigned access"
                    : "Workspace overview · financial services pending"}
                </small>
              </span>
              <ArrowUpRight size={16} />
            </button>
          ))}
          {!results.length && (
            <p className="search-empty" role="status">
              No workspaces match “{query}”. Try “transactions” or “settings”.
            </p>
          )}
        </div>
        <div className="search-dialog-footer">
          <span>Workspace navigation only</span>
          <span>
            <kbd>Esc</kbd> to close
          </span>
        </div>
      </dialog>
    </>
  );
}

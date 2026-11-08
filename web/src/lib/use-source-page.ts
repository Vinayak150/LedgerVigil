"use client";
import { useCallback, useEffect, useRef, useState } from "react";
export type SourcePage<T> = { items: T[]; next_cursor: string | null };
export type PageLoader<T> = (
  cursor: string | undefined,
  signal: AbortSignal,
) => Promise<SourcePage<T>>;
// Each selector owns its cancellation and cursor. A changed parent/filter mounts a fresh selector.
export function useSourcePage<T extends { id: string }>(loader: PageLoader<T>) {
  const [items, setItems] = useState<T[]>([]),
    [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false),
    [error, setError] = useState("");
  const active = useRef<AbortController | null>(null),
    generation = useRef(0);
  const load = useCallback(
    async (next?: string) => {
      active.current?.abort();
      const controller = new AbortController();
      active.current = controller;
      const run = ++generation.current;
      setLoading(true);
      setError("");
      if (!next) {
        setItems([]);
        setCursor(null);
      }
      try {
        const page = await loader(next, controller.signal);
        if (controller.signal.aborted || run !== generation.current) return;
        setItems((old) =>
          next
            ? [
                ...old,
                ...page.items.filter((i) => !old.some((o) => o.id === i.id)),
              ]
            : page.items,
        );
        setCursor(page.next_cursor);
      } catch (e) {
        if (!controller.signal.aborted && run === generation.current)
          setError(e instanceof Error ? e.message : "Source view unavailable.");
      } finally {
        if (!controller.signal.aborted && run === generation.current)
          setLoading(false);
      }
    },
    [loader],
  );
  useEffect(() => {
    void load();
    return () => {
      generation.current++;
      active.current?.abort();
    };
  }, [load]);
  return {
    items,
    cursor,
    loading,
    error,
    reload: () => void load(),
    more: () => cursor && void load(cursor),
  };
}
export function sourceError(status: number): Error {
  return new Error(
    status === 401
      ? "Your session has ended. Refresh your session to continue."
      : status === 403
        ? "Your account cannot read these sources."
        : status === 404
          ? "This source is unavailable in your organization."
          : status === 400
            ? "This page link is no longer valid. Reload the sources."
            : "Sources could not be loaded. Retry to request a fresh view.",
  );
}

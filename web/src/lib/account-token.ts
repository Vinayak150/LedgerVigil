// Single-use account tokens arrive in the URL fragment (#token=...). Read once, then removed from the address bar and the
// history entry; never written to storage. Returns null when the fragment holds no well-formed token.
const TOKEN = /^[A-Za-z0-9_-]{43}$/;

export function parseFragmentToken(hash: string): string | null {
  const params = new URLSearchParams(
    hash.startsWith("#") ? hash.slice(1) : hash,
  );
  const value = params.get("token");
  return value && TOKEN.test(value) ? value : null;
}

export function readFragmentToken(): string | null {
  const value = parseFragmentToken(window.location.hash);
  if (window.location.hash)
    window.history.replaceState(null, "", window.location.pathname);
  return value;
}

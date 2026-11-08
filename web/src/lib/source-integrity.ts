export type TextIntegrity = "MATCH" | "MISMATCH" | "UNAVAILABLE";
/** Check the exact UTF-8 text returned by the API, without Unicode or newline normalization. */
export async function checkSourceText(
  text: string,
  sha256: string | null | undefined,
  byteLength: number,
): Promise<TextIntegrity> {
  if (
    !sha256 ||
    !/^[0-9a-f]{64}$/.test(sha256) ||
    !Number.isSafeInteger(byteLength) ||
    byteLength < 0 ||
    !globalThis.crypto?.subtle
  )
    return "UNAVAILABLE";
  const bytes = new TextEncoder().encode(text);
  if (bytes.byteLength !== byteLength) return "MISMATCH";
  try {
    const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
    const actual = Array.from(new Uint8Array(digest), (b) =>
      b.toString(16).padStart(2, "0"),
    ).join("");
    return actual === sha256 ? "MATCH" : "MISMATCH";
  } catch {
    return "UNAVAILABLE";
  }
}

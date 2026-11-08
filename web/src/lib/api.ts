import createClient from "openapi-fetch";
import type { paths, components } from "./api.generated";
// The schema generates every request and response type. FastAPI owns all sessions.
export const api = createClient<paths>({
  credentials: "same-origin",
  cache: "no-store",
  fetch: (request: Request) =>
    fetch(request, {
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(15_000)]),
    }),
});
export type Session = components["schemas"]["Session"];
export type Tenant = components["schemas"]["Tenant"];
export function problemMessage(response: Response, login = false): string {
  if (response.status === 401)
    return login
      ? "We couldn’t sign you in. Check your email and password."
      : "Your session has ended. Please sign in again.";
  if (response.status === 403)
    return "This request could not be authorized. Refresh your session and try again.";
  if (response.status === 404)
    return "This organization is unavailable to your account.";
  if (response.status === 429)
    return "Too many sign-in attempts. Please wait before trying again.";
  if (response.status === 422)
    return "Check the information you entered and try again.";
  return "The service is temporarily unavailable. Please try again.";
}
export const offlineMessage =
  "We couldn’t reach LedgerVigil. Check your connection and try again.";

import createClient from "openapi-fetch";
import type { paths, components } from "./api.optional.generated";
// Typed client for the gated contracts (detection: LV_DETECTION_ENABLED, QuickBooks sandbox: LV_PROVIDER_SANDBOX_ENABLED),
// generated from api/openapi.full.json. When a gate is off the server has no such routes and answers 404; callers show
// "not enabled on this server" for that case. The default client (api.ts) and its contract are unchanged.
export const optionalApi = createClient<paths>({
  credentials: "same-origin",
  cache: "no-store",
  fetch: (request: Request) =>
    fetch(request, {
      signal: AbortSignal.any([request.signal, AbortSignal.timeout(15_000)]),
    }),
});
export type Finding = components["schemas"]["DetectionFinding"];
export type DetectionRun = components["schemas"]["DetectionRun"];
export type DetectionConfiguration =
  components["schemas"]["DetectionConfiguration"];
export type ProviderConnection = components["schemas"]["Connection"];
export type ProviderConnectionList = components["schemas"]["ConnectionList"];
export type ProviderJob = components["schemas"]["Job"];
export type ProviderObject = components["schemas"]["ProviderObject"];

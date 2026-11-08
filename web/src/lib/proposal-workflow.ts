import type { components } from "./api.generated";
export type Proposal = components["schemas"]["Proposal"];
export type Simulation = components["schemas"]["SimulationRun"];
export type Action = "submit" | "withdraw" | "resimulate" | "revalidate";
export type Attempt = {
  action: Action;
  proposalId: string;
  etag: string;
  key: string;
  startedAt: number;
};
export function attemptStorage(
  userId: string,
  tenantId: string,
  proposalId: string,
) {
  return `lv.workflow.1:${userId}:${tenantId}:${proposalId}`;
}
export function parseAttempt(
  raw: string | null,
  proposalId: string,
): Attempt | null {
  try {
    const a = JSON.parse(raw ?? "null");
    return a &&
      a.proposalId === proposalId &&
      ["submit", "withdraw", "resimulate", "revalidate"].includes(a.action) &&
      typeof a.etag === "string" &&
      /^"v[1-9]\d*"$/.test(a.etag) &&
      typeof a.key === "string" &&
      /^[a-zA-Z0-9-]{1,64}$/.test(a.key) &&
      Number.isSafeInteger(a.startedAt) &&
      a.startedAt > 0
      ? a
      : null;
  } catch {
    return null;
  }
}
// Stop well before the server's 24-hour cache expires. Reading the head never proves an old request did not commit.
export function retryWindow(a: Attempt, now = Date.now()) {
  return now >= a.startedAt && now - a.startedAt < 23 * 60 * 60 * 1000;
}
export function problemCode(error: unknown): string {
  return error &&
    typeof error === "object" &&
    "code" in error &&
    typeof error.code === "string"
    ? error.code
    : "";
}
export function problemReason(error: unknown): string {
  const e = record(error);
  return typeof e.reason === "string" && /^[A-Z0-9_]{1,100}$/.test(e.reason)
    ? e.reason
    : "";
}
export function uncertainResponse(status: number, error: unknown) {
  return (
    status >= 500 ||
    (status >= 200 && status < 300) ||
    (status === 409 && problemCode(error) === "REQUEST_IN_PROGRESS")
  );
}
export function workflowError(status: number, error: unknown) {
  const code = problemReason(error) || problemCode(error);
  if (code === "REQUEST_IN_PROGRESS")
    return "This attempt is still in progress. Check the current proposal or retry the same attempt.";
  if (status === 401)
    return "Your session has ended. Refresh your session before continuing.";
  if (status === 403)
    return "Your account cannot perform this action. Maker identity, current permissions and session protection are checked by the server.";
  if (status === 404)
    return "The proposal or a required reference is unavailable to this organization.";
  if (status === 412)
    return "The proposal changed after you opened it. Refresh its current state before starting another attempt.";
  if (code === "STAGED_DOCUMENT_RESERVED")
    return "A cited statement is reserved by another proposal. Inspect the current statement; no reservation was acquired by this attempt.";
  if (code === "CAPABILITY_UNAVAILABLE" || code === "UNSUPPORTED_BY_PROFILE")
    return "This draft is outside the supported native settlement submission scope.";
  if (status === 422)
    return "The server refused this attempt. Review the reason and saved draft before making a deliberate new attempt.";
  if (status === 409)
    return "The proposal is no longer in a state that allows this action. Refresh its current state.";
  return "The response did not confirm the outcome. Check the current proposal before retrying the same attempt.";
}
export function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
export function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? value.filter((v) => v && typeof v === "object" && !Array.isArray(v))
    : [];
}
export function text(value: unknown): string {
  return typeof value === "string" ? value : "Unavailable";
}
export function resultLabel(run: Simulation): {
  title: string;
  tone: string;
  description: string;
} {
  const v = record(run.outcome.verification);
  if (run.status === "REJECTED")
    return {
      title: "Simulation refused",
      tone: "error",
      description: "No policy result was produced for this run.",
    };
  if (v.status !== "EVALUATED")
    return {
      title: "Policies not evaluated",
      tone: "warning",
      description:
        "This historical profile has no evaluated policy verdicts. It cannot authorize an action.",
    };
  switch (run.outcome.aggregate) {
    case "BLOCKED_INSUFFICIENT":
      return {
        title: "Blocked · evidence insufficient",
        tone: "warning",
        description:
          "The recorded evidence does not satisfy every required check. Synthetic evidence remains synthetic.",
      };
    case "BLOCKED_FAIL":
      return {
        title: "Blocked · checks failed",
        tone: "error",
        description:
          "One or more checks failed on the pinned inputs. Inspect the verdicts below.",
      };
    case "CLEAR":
      return {
        title: "Checks clear on pinned inputs",
        tone: "neutral",
        description:
          "This computed result is not accounting approval or current execution eligibility. Independent approval is a separate recorded decision. Execution is unavailable.",
      };
    default:
      return {
        title: "Unrecognized policy result",
        tone: "warning",
        description: "Inspect the recorded outcome. No readiness is inferred.",
      };
  }
}

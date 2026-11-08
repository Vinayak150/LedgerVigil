import type { components } from "./api.generated";
export type PublicGroup = components["schemas"]["Group"];
export const PUBLIC_PLAN_MANIFEST =
  "3c7dbcb169405d9d0f8abcff095c9f30df46ca6b1b38fb31f894ed1657ea0523";
export const SOURCE_COLUMNS = [
  "Invoice",
  "Stock code",
  "Description",
  "Quantity",
  "Invoice date",
  "Unit price",
  "Customer ID",
  "Country",
] as const;
export function groupLabel(group: PublicGroup): string {
  return group.source_identity[0] === "invoice"
    ? `Invoice ${group.source_identity[1]}`
    : `Missing invoice · ${group.source_identity.slice(1).join(" · ")}`;
}
export function assertGroup(group: PublicGroup): void {
  if (
    !/^[a-f0-9]{64}$/.test(group.group_id) ||
    group.posting_eligibility !== "UNAVAILABLE" ||
    group.accounting_status !== "UNREVIEWED" ||
    !["HOLD", "CANDIDATE_REVIEW"].includes(group.status) ||
    (group.status === "HOLD" && group.amount !== null)
  )
    throw new Error(
      "The source response is inconsistent. No invoice is shown.",
    );
}
export function planError(status: number, registering = false): Error {
  if (registering && status === 503)
    return new Error(
      "The source service is unavailable. Retry opening the dataset to check this registration’s result.",
    );
  return new Error(
    status === 401
      ? "Your session has ended. Refresh your session to continue."
      : status === 403
        ? "Your account does not have permission for this source request."
        : status === 404
          ? "This source is unavailable in your organization."
          : status === 409
            ? "Another registration request may be in progress. Retry to check its result."
            : status === 400
              ? "This page link is no longer valid. Refresh the invoice list."
              : status === 503
                ? "The verified source index is unavailable. Retry later; no empty-dataset result is implied."
                : "The public source could not be loaded. Retry or refresh your session.",
  );
}

import type { components, paths } from "./api.generated";
export type Ledger = components["schemas"]["Ledger"];
export type Statement = components["schemas"]["StagedDocument"];
export type Payment = components["schemas"]["DocumentSummary"];
export type Evidence = components["schemas"]["EvidenceRef"];
export type CreateBody =
  paths["/api/v1/proposals"]["post"]["requestBody"]["content"]["application/json"];
export function evidenceKey(ref: Pick<Evidence, "kind" | "id">): string {
  return `${ref.kind}:${ref.id}`;
}
export function createBody(
  ledger: Ledger,
  statement: Statement,
  payments: Payment[],
  evidence: Evidence[],
  rationale: string,
): CreateBody {
  if (
    ledger.purpose !== "SANDBOX" ||
    ledger.source !== "NATIVE" ||
    statement.ledger_id !== ledger.id ||
    !payments.length ||
    payments.length > 1000
  )
    throw new Error(
      "Choose a sandbox ledger, a statement and at least one payment in that ledger.",
    );
  if (
    new Set(payments.map((p) => p.id)).size !== payments.length ||
    payments.some(
      (p) =>
        p.ledger_id !== ledger.id ||
        p.type !== "PAYMENT" ||
        !p.amount ||
        p.amount.currency !== statement.gross.currency,
    )
  )
    throw new Error(
      "Payment references must be distinct, in this ledger and in the statement currency.",
    );
  if (rationale.length > 4000 || evidence.length > 100)
    throw new Error(
      "Use at most 4,000 rationale characters and 100 evidence references.",
    );
  return {
    ledger_id: ledger.id,
    target_adapter: "NATIVE",
    operations: [
      {
        type: "RecordSettlement",
        schema_version: 1,
        processor_ref: statement.processor_ref,
        payout_id: statement.payout_id,
        payout_date: statement.payout_date,
        statement_document_id: statement.id,
        gross: { ...statement.gross },
        fee: { ...statement.fee },
        net: { ...statement.net },
        items: payments.map((p) => ({
          document_id: p.id,
          kind: "PAYMENT",
          amount: { ...p.amount! },
        })),
      },
    ],
    evidence_refs: [
      ...new Map(
        evidence.map((e) => [
          evidenceKey(e),
          {
            kind: e.kind as
              "STAGED_DOCUMENT" | "SOURCE_RECORD" | "DOCUMENT_VERSION",
            id: e.id,
          },
        ]),
      ).values(),
    ],
    rationale: rationale || null,
  };
}
export function createFailure(status: number): string {
  if (status === 401)
    return "Your session has ended. Your selections remain here while this window stays open.";
  if (status === 403)
    return "Your account is not authorized to create drafts. Refresh your session before continuing.";
  if (status === 404)
    return "A selected source is unavailable in this ledger. Review the source selection.";
  if (status === 422)
    return "The server refused this draft. Review the issues below; no draft was confirmed.";
  return "The create result is not confirmed. Keep this window open and retry the identical request to recover its result.";
}

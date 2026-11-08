// Display descriptions only. Permissions and authorization always come from FastAPI.
export const permissionLabels: Record<
  string,
  { title: string; description: string }
> = {
  read: {
    title: "View workspace",
    description: "Read records available to your organization.",
  },
  propose: {
    title: "Prepare proposals",
    description: "Create proposed changes when that workflow is available.",
  },
  review: {
    title: "Review proposals",
    description: "Review changes subject to separation-of-duties rules.",
  },
  execute: {
    title: "Execute approved changes",
    description: "Execute only when all server-side gates permit it.",
  },
  "source.import": {
    title: "Import source evidence",
    description: "Import and reconcile source records when available.",
  },
  "ledger.bootstrap": {
    title: "Set up ledgers",
    description: "Initialize a ledger through its controlled setup flow.",
  },
  regression: {
    title: "Run regression checks",
    description: "Run the available verification scenarios.",
  },
  "evidence.export": {
    title: "Export evidence",
    description: "Export available evidence and proof packs.",
  },
  "audit.read": {
    title: "Read audit history",
    description: "Inspect the organization’s available audit trail.",
  },
  administer: {
    title: "Administer organization",
    description:
      "Manage configured administrative workflows; no financial approval is implied.",
  },
};

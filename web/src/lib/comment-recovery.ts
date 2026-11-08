export type CommentScope = {
  tenantId: string;
  userId: string;
  proposalId: string;
};
export type CommentRecovery = CommentScope & {
  schema: 1;
  body: string;
  request: { key: string; version: number } | null;
  blockedVersion: number | null;
};
export function commentStorageKey(scope: CommentScope): string {
  return `ledgervigil:comment:v1:${scope.tenantId}:${scope.userId}:${scope.proposalId}`;
}
export function readCommentRecovery(
  raw: string | null,
  scope: CommentScope,
): CommentRecovery | null {
  if (!raw || raw.length > 50000) return null;
  try {
    const value = JSON.parse(raw);
    if (
      value.schema !== 1 ||
      value.tenantId !== scope.tenantId ||
      value.userId !== scope.userId ||
      value.proposalId !== scope.proposalId ||
      typeof value.body !== "string" ||
      Array.from(value.body).length > 4000 ||
      (value.blockedVersion !== null &&
        (!Number.isSafeInteger(value.blockedVersion) ||
          value.blockedVersion < 1)) ||
      (value.request !== null &&
        (!value.request ||
          !/^[a-f0-9-]{36}$/.test(value.request.key) ||
          !Number.isSafeInteger(value.request.version) ||
          value.request.version < 1 ||
          !value.body.trim()))
    )
      return null;
    return value;
  } catch {
    return null;
  }
}

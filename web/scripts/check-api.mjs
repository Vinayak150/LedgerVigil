import { readFileSync, mkdtempSync, rmSync, existsSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
const source = process.env.LV_OPENAPI_SOURCE || resolve("../api/openapi.json");
if (!existsSync(source))
  throw new Error(
    "Owning API contract missing. Set LV_OPENAPI_SOURCE when running outside app/web.",
  );
if (!readFileSync(source).equals(readFileSync("openapi.json")))
  throw new Error(
    "Frontend OpenAPI snapshot differs from backend. Review API changes before regenerating.",
  );
const dir = mkdtempSync(join(tmpdir(), "lv-api-"));
try {
  const generated = join(dir, "api.d.ts");
  execFileSync(
    process.execPath,
    [
      "node_modules/openapi-typescript/bin/cli.js",
      "openapi.json",
      "-o",
      generated,
    ],
    { stdio: "pipe" },
  );
  if (
    !readFileSync(generated).equals(readFileSync("src/lib/api.generated.d.ts"))
  )
    throw new Error(
      "Generated API types have drifted. Run npm run generate:api.",
    );
  console.log("Owning OpenAPI and generated TypeScript match byte for byte.");
} finally {
  rmSync(dir, { recursive: true, force: true });
}

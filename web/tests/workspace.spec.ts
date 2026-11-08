import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { readFileSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
const credentialsPath = process.env.LV_TEST_CREDENTIALS;
if (!credentialsPath)
  throw new Error(
    "Set LV_TEST_CREDENTIALS to a private synthetic-account credentials file.",
  );
const credentials: Record<string, string> = JSON.parse(
  readFileSync(credentialsPath, "utf8"),
);
function user(role: string) {
  const email = Object.keys(credentials).find((e) => e.startsWith(role + "."));
  if (!email) throw new Error("Synthetic role missing");
  return { email, password: credentials[email] };
}
async function login(page: Page, role = "multi") {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Sign in to your workspace" }),
  ).toBeVisible();
  const u = user(role);
  await page.getByLabel("Email address").fill(u.email);
  await page.getByLabel("Password", { exact: true }).fill(u.password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  if (role === "multi") {
    await expect(
      page.getByRole("heading", { name: "Select an organization" }),
    ).toBeVisible();
    await page.locator(".tenant-option").first().click();
  }
  await expect(
    page.getByRole("heading", { name: "A clearer view of your finances." }),
  ).toBeVisible();
}
async function a11y(page: Page, name: string) {
  const result = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21aa"])
    .analyze();
  writeFileSync(
    `evidence/axe-${name}.json`,
    JSON.stringify(
      result.violations.map((v) => ({
        id: v.id,
        impact: v.impact,
        nodes: v.nodes.map((n) => ({
          target: n.target,
          summary: n.failureSummary,
        })),
      })),
      null,
      2,
    ),
  );
  expect(result.violations.map((v) => v.id)).toEqual([]);
}
test("sign-in screen: keyboard, accessibility and responsive layout", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByLabel("Email address")).toBeVisible();
  await page.getByLabel("Email address").focus();
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Password", { exact: true })).toBeFocused();
  await page.screenshot({
    path: "evidence/sign-in-desktop.png",
    fullPage: true,
  });
  await a11y(page, "login");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "evidence/sign-in-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
test("real login, tenant switching, cookie rotation, restore and logout", async ({
  page,
  context,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await login(page);
  const first = await page.request.get("/api/v1/me");
  const before = await first.json();
  const cookie = (await context.cookies()).find(
    (c) => c.name === "lv_session_local",
  );
  expect(Boolean(cookie?.httpOnly && cookie.sameSite === "Lax")).toBe(true);
  expect(
    await page.evaluate(() => document.cookie.includes("lv_session")),
  ).toBe(false);
  expect(
    await page.evaluate(() => localStorage.length + sessionStorage.length),
  ).toBe(0);
  await page.screenshot({
    path: "evidence/workspace-desktop.png",
    fullPage: true,
  });
  await page
    .getByRole("link", {
      name: "Organization and Integration Settings",
      exact: true,
    })
    .click();
  await expect(page.getByText(before.tenant.id, { exact: true })).toBeVisible();
  await expect(page.getByText("propose", { exact: true })).toBeVisible();
  await page.screenshot({
    path: "evidence/access-desktop.png",
    fullPage: true,
  });
  await page.locator(".organization-switch").click();
  const options = await page.locator(".tenant-option").all();
  for (const option of options) {
    if (!(await option.innerText()).includes(before.tenant.id)) {
      await option.click();
      break;
    }
  }
  await expect(
    page.getByRole("heading", { name: "Your workspace, in context." }),
  ).toBeVisible();
  const after = await (await page.request.get("/api/v1/me")).json();
  expect(after.tenant.id !== before.tenant.id).toBe(true);
  expect(after.csrf_token !== before.csrf_token).toBe(true);
  expect(
    (await context.cookies()).find((c) => c.name === "lv_session_local")
      ?.value !== cookie?.value,
  ).toBe(true);
  await expect(page.getByText(before.tenant.id, { exact: true })).toHaveCount(
    0,
  );
  await page.reload();
  await expect(page.getByText(after.tenant.id, { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Sign in to your workspace" }),
  ).toBeVisible();
  expect((await page.request.get("/api/v1/me")).status()).toBe(401);
  expect(errors).toEqual([]);
});
test("workspace accessibility and mobile navigation", async ({ page }) => {
  await login(page, "analyst");
  await a11y(page, "workspace");
  await page
    .getByRole("link", {
      name: "Organization and Integration Settings",
      exact: true,
    })
    .click();
  await a11y(page, "settings");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Open navigation" }).click();
  await page
    .getByRole("link", { name: "Financial Command Center", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "A clearer view of your finances." }),
  ).toBeFocused();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "evidence/workspace-mobile.png",
    fullPage: true,
  });
  await a11y(page, "mobile");
});
test("server refuses missing CSRF and unowned tenant; administrator has no financial permissions", async ({
  page,
}) => {
  await login(page, "administrator");
  const s = await (await page.request.get("/api/v1/me")).json();
  expect(s.permissions.includes("administer")).toBe(true);
  for (const p of ["propose", "review", "execute"])
    expect(s.permissions.includes(p)).toBe(false);
  expect(
    (
      await page.request.post("/api/v1/auth/switch-tenant", {
        data: { tenant_id: s.tenant.id },
      })
    ).status(),
  ).toBe(403);
  expect(
    (
      await page.request.post("/api/v1/auth/switch-tenant", {
        data: { tenant_id: "00000000-0000-0000-0000-000000000000" },
        headers: {
          "X-CSRF-Token": s.csrf_token,
          Origin: "http://127.0.0.1:3000",
        },
      })
    ).status(),
  ).toBe(404);
  expect(
    (
      await page.request.post("/api/v1/auth/logout", {
        headers: {
          "X-CSRF-Token": s.csrf_token,
          Origin: "https://untrusted.invalid",
        },
      })
    ).status(),
  ).toBe(403);
  expect(
    (await (await page.request.get("/api/v1/me")).json()).tenant.id ===
      s.tenant.id,
  ).toBe(true);
});
test("real invalid password stays generic and clears password field", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("Email address").fill(user("analyst").email);
  await page
    .getByLabel("Password", { exact: true })
    .fill("incorrect-test-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.locator(".notice[role=alert]")).toHaveText(
    "We couldn’t sign you in. Check your email and password.",
  );
  await expect(page.getByLabel("Password", { exact: true })).toHaveValue("");
});
test("UI fault injection: rate limit respects Retry-After", async ({
  page,
}) => {
  await page.route("**/api/v1/auth/login", (r) =>
    r.fulfill({
      status: 429,
      headers: { "Retry-After": "3" },
      contentType: "application/json",
      body: "{}",
    }),
  );
  await page.goto("/");
  await page.getByLabel("Email address").fill("synthetic@example.invalid");
  await page.getByLabel("Password", { exact: true }).fill("synthetic-test");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("button", { name: /Try again in/ }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true }),
  ).toBeEnabled({ timeout: 6000 });
});
test("UI fault injection: offline restore is recoverable", async ({ page }) => {
  await page.route("**/api/v1/me", (r) => r.abort());
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Let’s reconnect" }),
  ).toBeVisible();
  await page.unroute("**/api/v1/me");
  await page.getByRole("button", { name: "Refresh session" }).click();
  await expect(
    page.getByRole("heading", { name: "Sign in to your workspace" }),
  ).toBeVisible();
});
test("UI fault injection: failed logout never claims success", async ({
  page,
}) => {
  await login(page, "controller");
  await page.route("**/api/v1/auth/logout", (r) => r.abort());
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.locator(".notice[role=alert]")).toContainText(
    "couldn’t confirm sign-out",
  );
  expect((await page.request.get("/api/v1/me")).status()).toBe(200);
  await page.unroute("**/api/v1/auth/logout");
  await page.getByRole("button", { name: "Refresh session" }).click();
  await expect(
    page.getByRole("heading", { name: "A clearer view of your finances." }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Sign in to your workspace" }),
  ).toBeVisible();
});
test("logout propagates to another tab without storing tokens", async ({
  page,
  context,
}) => {
  await login(page, "analyst");
  const other = await context.newPage();
  await other.goto("/");
  await expect(
    other.getByRole("heading", { name: "A clearer view of your finances." }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(
    other.getByRole("heading", { name: "Sign in to your workspace" }),
  ).toBeVisible();
  await other.close();
});
test("real administrative revocation removes access on revalidation", async ({
  page,
}) => {
  await login(page, "automation");
  const app = process.env.LV_TEST_APP;
  if (!app) throw new Error("Set LV_TEST_APP to verified backend app");
  execFileSync(
    `${app}/.venv/bin/python`,
    [
      "-m",
      "tools.identity_admin",
      "revoke-sessions",
      "--email",
      user("automation").email,
    ],
    { cwd: app, stdio: "pipe" },
  );
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Sign in to your workspace" }),
  ).toBeVisible();
  expect((await page.request.get("/api/v1/me")).status()).toBe(401);
});

test("UI clock: idle expiry clears identity without background keep-alive", async ({
  page,
}) => {
  await page.clock.install();
  await login(page, "analyst");
  await page.clock.fastForward(31 * 60 * 1000);
  await expect(
    page.getByRole("heading", { name: "Sign in to your workspace" }),
  ).toBeVisible();
  await expect(page.locator(".notice[role=alert]")).toContainText(
    "Your session has ended",
  );
  await expect(page.locator(".organization-switch")).toHaveCount(0);
});

test("UI fault injection: a stalled session request times out with recovery", async ({
  page,
}) => {
  await page.route("**/api/v1/me", async () => {});
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Let’s reconnect" }),
  ).toBeVisible({ timeout: 20000 });
  await expect(
    page.getByRole("button", { name: "Refresh session" }),
  ).toBeVisible();
});

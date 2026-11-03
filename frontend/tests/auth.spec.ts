import { expect, test } from "@playwright/test";

// Runs against a stack started with TRACKER_AUTH=on and a test admin created with `python -m tracker.users create`.
const username = process.env.TEST_ADMIN_USERNAME;
const password = process.env.TEST_ADMIN_PASSWORD;
test.skip(!username || !password, "Sin TEST_ADMIN_USERNAME/TEST_ADMIN_PASSWORD: la pila no tiene login");

test("sign-in, account panel and sign-out", async ({ page, context }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Inicia sesión" })).toBeVisible();
  await page.getByLabel("Usuario").fill(username!);
  await page.getByLabel("Contraseña").fill("not-the-password-1");
  await page.getByRole("button", { name: "Entrar" }).click();
  await expect(page.locator(".message.error")).toContainText("incorrectos");
  await page.getByLabel("Contraseña").fill(password!);
  await page.getByRole("button", { name: "Entrar" }).click();
  await expect(page.getByText(`${username} · administrador`)).toBeVisible();
  const [session] = (await context.cookies()).filter(c => c.name === "tracker_session");
  expect(session.httpOnly && session.sameSite === "Strict").toBeTruthy();
  await page.getByRole("button", { name: /Usuarios y cuenta/ }).first().click();
  await expect(page.getByRole("heading", { name: "Auditoría" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(0);
  await page.getByRole("button", { name: "Salir" }).click();
  await expect(page.getByRole("heading", { name: "Inicia sesión" })).toBeVisible();
  expect((await page.request.get("/api/wallets")).status()).toBe(401);
});

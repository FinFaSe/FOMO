import { test, expect } from "@playwright/test";

test("defaults, live backend, accessible navigation and responsive layout", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByText("Backend conectado", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Market cap máximo de entrada")).toHaveValue("250000");
  await expect(page.getByLabel("ROI realizado mínimo (%)")).toHaveValue("500");
  await expect(page.getByLabel("Win rate mínimo (%)")).toHaveValue("60");
  await expect(page.getByLabel("Posiciones evaluables mínimas")).toHaveValue("30");
  await expect(page.getByLabel("Ventana de análisis")).toHaveValue("90");
  await expect(page.getByRole("checkbox")).toBeChecked();
  await page.getByLabel("Ventana de análisis").selectOption("30");
  await page.getByRole("button", { name: "Aplicar filtros" }).click();
  await expect(page.getByRole("status")).toContainText("Filtros aplicados");
  await page.getByRole("button", { name: "Ranking validado" }).click();
  await expect(page.getByRole("heading", { name: "Ranking de evidencia validada" })).toBeVisible();
  await expect(page.getByText("Aún no hay traders que superen la verificación.")).toBeVisible();
  await page.getByRole("button", { name: "Fuentes y método" }).click();
  await expect(page.getByRole("heading", { name: "Fuentes disponibles" })).toBeVisible();
  await expect(page.getByText("Requiere FOMO_API_KEY", { exact: true })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  expect(overflow).toBe(false);
  expect(errors).toEqual([]);
});

test("a malformed wallet cannot be added and backend error is visible", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Backend conectado", { exact: true })).toBeVisible();
  await page.getByLabel("Dirección Solana", { exact: true }).fill("not-a-solana-wallet");
  await page.getByRole("button", { name: "+ Añadir wallet" }).click();
  await expect(page.getByRole("alert").filter({ hasText: "Datos inválidos" })).toBeVisible();
});

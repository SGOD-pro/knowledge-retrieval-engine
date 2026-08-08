import { test, expect } from '@playwright/test';

test('has title and logs in', async ({ page }) => {
  await page.goto('/');

  // Should see the dummy login screen
  await expect(page.getByText('KRE Login')).toBeVisible();
  
  // Click login
  await page.getByRole('button', { name: 'Sign In to Workspace' }).click();

  // Should see workspace
  await expect(page.getByText('KRE Intelligence').first()).toBeVisible();
  await expect(page.getByPlaceholder('Ask a question about the document...')).toBeVisible();
});

test('submits query and renders 3 panes', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Sign In to Workspace' }).click();

  // Submit query for a document we know was ingested successfully (e.g. CSV or DOCX)
  const input = page.getByPlaceholder('Ask a question about the document...');
  await input.fill('What bills were passed?');
  await page.getByRole('button', { name: 'Send' }).click();

  // Verify left pane (PDF mock or generic viewer)
  await expect(page.getByText('Document Viewer')).toBeVisible();

  // Wait for the query to resolve (API call might take a few seconds)
  // We don't check for exact mock text anymore. Just that an answer appears.
  // Wait for the "Reasoned Answer" or "Fast Match" badge which indicates completion
  await expect(page.getByText(/Reasoned Answer|Fast Match/)).toBeVisible({ timeout: 15000 });
  
  // Verify center pane path badges
  await expect(page.getByText('BM25')).toBeVisible();
  
  await expect(page.getByText(/Sources/).first()).toBeVisible();
});

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

  // Submit query
  const input = page.getByPlaceholder('Ask a question about the document...');
  await input.fill('What is the refund policy?');
  await input.press('Enter');

  // Wait for mock response (1.5s delay)
  await expect(page.getByText('The Indian healthcare sector is expected to grow')).toBeVisible({ timeout: 5000 });

  // Verify center pane badges
  await expect(page.getByText('Reasoned Answer')).toBeVisible();
  await expect(page.getByText('BM25')).toBeVisible();
  
  // Verify right pane citations
  await expect(page.getByText('Cited Sources')).toBeVisible();
  await expect(page.getByText('Healthcare in India is expected to reach USD 280 billion...')).toBeVisible();

  // Verify left pane fallback state for PDF selection
  // The first citation (PDF) is selected by default in our app state
  await expect(page.getByText('Mock PDF Page Render')).toBeVisible();
});

import { test, expect } from '@playwright/test';

test.describe('KRE Enterprise Platform — Full End-to-End Test Suite', () => {

  test('1. Authentication Flow (Sign In & Navigation)', async ({ page }) => {
    await page.goto('/login');

    // Verify Auth Card Branding & Heading
    await expect(page.getByText('Knowledge Retrieval Engine')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Sign In' })).toBeVisible();

    // Verify Inputs & Fill
    const emailInput = page.getByPlaceholder('name@organization.com');
    await expect(emailInput).toBeVisible();
    await emailInput.fill('alexandra.chen@enterprise.com');

    const passwordInput = page.getByPlaceholder('••••••••');
    await expect(passwordInput).toBeVisible();
    await passwordInput.fill('securepassword123');

    // Toggle Remember Me
    const rememberMe = page.getByLabel('Remember me');
    await expect(rememberMe).toBeVisible();
    await rememberMe.click();

    // Verify SSO Buttons
    await expect(page.getByRole('button', { name: 'Google' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Microsoft' })).toBeVisible();

    // Submit Sign In
    const signInBtn = page.getByRole('button', { name: 'Sign In', exact: true });
    await expect(signInBtn).toBeVisible();
    await signInBtn.click();

    // Verify Navigation to Workspaces
    await expect(page).toHaveURL(/.*\/workspaces/);
    await expect(page.getByRole('heading', { name: 'Workspaces' })).toBeVisible();
  });

  test('2. Workspace Management Flow (Search, List & Create)', async ({ page }) => {
    await page.goto('/workspaces');

    // Check Header & Search
    await expect(page.getByRole('heading', { name: 'Workspaces' })).toBeVisible();
    const searchInput = page.getByPlaceholder('Search workspaces...');
    await expect(searchInput).toBeVisible();

    // Check Seeded Workspaces
    await expect(page.getByText('Finance Docs')).toBeVisible();
    await expect(page.getByText('Legal Contracts')).toBeVisible();
    await expect(page.getByText('Engineering R&D')).toBeVisible();

    // Search filter test
    await searchInput.fill('Legal');
    await expect(page.getByText('Legal Contracts')).toBeVisible();
    await expect(page.getByText('Engineering R&D')).not.toBeVisible();
    await searchInput.fill('');

    // Open Create Workspace Modal
    const createCard = page.getByText('Create New Workspace');
    await expect(createCard).toBeVisible();
    await createCard.click();

    // Fill Workspace Form using exact placeholders
    const nameInput = page.getByPlaceholder('e.g., Engineering R&D');
    await expect(nameInput).toBeVisible();
    await nameInput.fill('AI Research Batch 2026');

    const descInput = page.getByPlaceholder(/Briefly describe the types of documents/i);
    await descInput.fill('Candidate resumes and NLP benchmark papers.');

    // Submit Create Workspace
    const createBtn = page.getByRole('button', { name: 'Create Workspace' });
    await createBtn.click();

    // Verify new workspace redirected to upload or appears in store
    await expect(page).toHaveURL(/.*\/workspaces\/.*\/upload/);
  });

  test('3. Document Upload & Library Flow', async ({ page }) => {
    await page.goto('/workspaces/ws_001/upload');

    // Header & description
    await expect(page.getByRole('heading', { name: 'Add Knowledge to Workspace' })).toBeVisible();
    await expect(page.getByText('Drag and drop files here')).toBeVisible();

    // Format chips container
    await expect(page.getByText('PDF', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('DOCX', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('CSV', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('PPTX', { exact: true }).first()).toBeVisible();

    // Processing callout
    await expect(page.getByText('KRE AI Processing')).toBeVisible();

    // Document list
    await expect(page.getByText('Workspace Documents')).toBeVisible();
    await expect(page.getByText('Senior_Dev_Resume_John_Doe.pdf')).toBeVisible();
    await expect(page.getByText('Marketing_Manager_Q3_Recruit.docx')).toBeVisible();
  });

  test('4. 3-Pane Chat Interface & Citation Resolution Flow', async ({ page }) => {
    await page.goto('/workspaces/ws_001/chat');

    // 1. Left Sidebar (History)
    await expect(page.getByRole('button', { name: 'Data Scientist Pipeline' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'New Chat' })).toBeVisible();

    // 2. Center Pane Header & Metrics
    await expect(page.getByRole('heading', { name: 'Data Scientist Pipeline' })).toBeVisible();
    await expect(page.getByText('Latency', { exact: true })).toBeVisible();
    await expect(page.getByText('Faithfulness', { exact: true })).toBeVisible();

    // 3. Check existing conversation message
    await expect(page.getByText('Summarize the machine learning experience for Candidate A')).toBeVisible();
    await expect(page.getByText('Candidate A has 4 years of applied machine learning experience')).toBeVisible();

    // 4. Verify interactive citation badges [1] and [2]
    const citation1 = page.locator('button[title*="citation [1]"]').first();
    await expect(citation1).toBeVisible();
    await expect(citation1).toHaveText('1');

    const citation2 = page.locator('button[title*="citation [2]"]').first();
    await expect(citation2).toBeVisible();
    await expect(citation2).toHaveText('2');

    // 5. Verify retrieval badges
    await expect(page.getByText(/Fast Match|Full Pipeline/)).toBeVisible();
    await expect(page.getByText('Reasoned Answer')).toBeVisible();

    // 6. Test submitting a new question
    const chatInput = page.getByPlaceholder('Ask about this candidate...');
    await expect(chatInput).toBeVisible();
    await chatInput.fill('What models were deployed by Candidate A?');
    await page.locator('button[type="submit"]').click();

    // Verify new user message appears in chat
    await expect(page.getByText('What models were deployed by Candidate A?')).toBeVisible();
  });

  test('5. Document Viewer & Citation Bounding Box Highlighting', async ({ page }) => {
    await page.goto('/workspaces/ws_001/chat');

    // Ensure Right Pane is open with candidate resume header
    await expect(page.getByRole('heading', { name: 'ALEXANDRA CHEN' })).toBeVisible();
    await expect(page.getByText('Senior Product Designer & ML Specialist')).toBeVisible();

    // Verify Zoom Controls
    await expect(page.getByTitle('Zoom In')).toBeVisible();
    await expect(page.getByTitle('Zoom Out')).toBeVisible();
    await expect(page.getByText('100%')).toBeVisible();

    // Zoom in test
    await page.getByTitle('Zoom In').click();
    await expect(page.getByText('115%')).toBeVisible();
    await page.getByTitle('Zoom Out').click();
    await expect(page.getByText('100%')).toBeVisible();

    // Verify Bounding Box 1 and 2
    const box1 = page.locator('#citation-box-1');
    await expect(box1).toBeVisible();
    await expect(box1).toContainText('SENIOR PRODUCT DESIGNER');

    const box2 = page.locator('#citation-box-2');
    await expect(box2).toBeVisible();
    await expect(box2).toContainText('ML INFRASTRUCTURE & MODEL DEPLOYMENT');

    // Click Box 2 to activate highlight
    await box2.click();
    await expect(box2).toHaveClass(/border-\[#c96442\]/);

    // Verify Pagination indicator
    await expect(page.getByText('Page 1 of 12').first()).toBeVisible();
  });

  test('6. OKF Knowledge Graph Visualization Flow', async ({ page }) => {
    await page.goto('/workspaces/ws_001/chat');

    // Switch to Graph View
    const graphTab = page.getByRole('button', { name: /Graph View/i });
    await expect(graphTab).toBeVisible();
    await graphTab.click();

    // Verify Graph Subheader
    await expect(page.getByText('OKF Knowledge Graph Visualization')).toBeVisible();
    await expect(page.getByText(/Nodes •.*Edges/)).toBeVisible();

    // Verify SVG Canvas rendered
    const svgCanvas = page.locator('svg[viewBox="0 0 400 360"]');
    await expect(svgCanvas).toBeVisible();

    // Verify Graph Nodes text labels
    await expect(svgCanvas.getByText('TechFlow Inc.')).toBeVisible();

    // Click node in SVG to inspect entity details
    const candNode = svgCanvas.locator('circle').first();
    await candNode.click();

    // Verify Entity Details Card updates
    await expect(page.getByText('Entity Details')).toBeVisible();
    await expect(page.getByText('Candidate A (Alexandra Chen)')).toBeVisible();
    await expect(page.getByText('Senior Product Designer')).toBeVisible();
  });

  test('7. System Benchmarks & Metrics Visualization Flow', async ({ page }) => {
    await page.goto('/benchmarks');

    // Top Header & Status Banner
    await expect(page.getByRole('heading', { name: 'System Benchmarks' })).toBeVisible();
    await expect(page.getByRole('heading', { name: /Production Model/ })).toBeVisible();
    await expect(page.getByText(/PASSING ALL|FAILING TARGETS/)).toBeVisible();

    // 4 KPI Cards
    await expect(page.getByText('p95 Latency')).toBeVisible();
    await expect(page.getByText('Recall@5')).toBeVisible();
    await expect(page.getByText('Faithfulness')).toBeVisible();
    await expect(page.getByText('LLM Activation')).toBeVisible();

    // Verify KPI Delta descriptions
    await expect(page.getByText(/vs target|vs baseline|On target/i).first()).toBeVisible();

    // Latency Chart Card
    await expect(page.getByRole('heading', { name: 'Latency Over Time (p95)' })).toBeVisible();

    // Time Range Pills
    const btn24h = page.getByRole('button', { name: '24h' });
    const btn7d = page.getByRole('button', { name: '7d' });
    const btn30d = page.getByRole('button', { name: '30d' });

    await expect(btn24h).toBeVisible();
    await expect(btn7d).toBeVisible();
    await expect(btn30d).toBeVisible();

    // Switch time range pill
    await btn24h.click();
    await expect(btn24h).toHaveClass(/bg-\[#c96442\]/);
    await btn7d.click();
    await expect(btn7d).toHaveClass(/bg-\[#c96442\]/);

    // Verify Recharts SVG Chart rendering
    const chartContainer = page.locator('.recharts-responsive-container');
    await expect(chartContainer).toBeVisible();
  });

});

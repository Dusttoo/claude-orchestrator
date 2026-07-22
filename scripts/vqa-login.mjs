#!/usr/bin/env node
// vqa-login.mjs -- log in through the real UI once and save a Playwright
// storageState, so vqa-capture.mjs can shoot authenticated surfaces unattended.
//
// This logs in like a user (fills the login form, waits for the redirect) rather
// than hand-crafting Supabase session cookies, so it stays correct as the auth
// stack changes. Run it once per session/tenant; the saved state is reused for
// every capture until it expires.
//
// Inputs via env:
//   BASE_URL        origin of the surface, e.g. http://localhost:3000
//   VQA_EMAIL       test-account email
//   VQA_PASSWORD    test-account password
//   STORAGE_STATE   output path for the saved state JSON (default .vqa/state.json)
//   LOGIN_PATH      login route (default /admin/login)
//   POST_LOGIN_PATH a route that only an authed user reaches; success = we land here
//                   without bouncing back to LOGIN_PATH (default /admin)
//   EMAIL_SELECTOR / PASSWORD_SELECTOR / SUBMIT_SELECTOR -- override if the form differs
//
// The selector defaults (input[name=email], input[name=password],
// button[type=submit]) fit a conventional login form; override them via env for
// any app whose form differs.

import { chromium } from 'playwright';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

const baseUrl = (process.env.BASE_URL || 'http://localhost:3000').replace(/\/$/, '');
const email = process.env.VQA_EMAIL;
const password = process.env.VQA_PASSWORD;
const statePath = process.env.STORAGE_STATE || '.vqa/state.json';
const loginPath = process.env.LOGIN_PATH || '/admin/login';
const postLoginPath = process.env.POST_LOGIN_PATH || '/admin';
const emailSel = process.env.EMAIL_SELECTOR || 'input[name="email"]';
const passSel = process.env.PASSWORD_SELECTOR || 'input[name="password"]';
const submitSel = process.env.SUBMIT_SELECTOR || 'button[type="submit"]';

if (!email || !password) {
  console.error('VQA_EMAIL and VQA_PASSWORD are required to mint an authed storageState.');
  process.exit(2);
}

mkdirSync(dirname(statePath), { recursive: true });

const browser = await chromium.launch();
const ctx = await browser.newContext();
const page = await ctx.newPage();

try {
  await page.goto(baseUrl + loginPath, { waitUntil: 'networkidle', timeout: 30000 });
  await page.fill(emailSel, email);
  await page.fill(passSel, password);
  await Promise.all([
    page.waitForLoadState('networkidle'),
    page.click(submitSel),
  ]);

  // Confirm we are actually authenticated: visiting a protected route should not
  // bounce back to the login page.
  await page.goto(baseUrl + postLoginPath, { waitUntil: 'networkidle', timeout: 30000 });
  if (page.url().includes(loginPath)) {
    console.error(`Login appears to have failed: ${postLoginPath} redirected back to ${loginPath}.`);
    console.error('Check VQA_EMAIL/VQA_PASSWORD and that the account has access to this host.');
    process.exit(1);
  }

  await ctx.storageState({ path: statePath });
  console.log(`Saved authed storageState -> ${statePath}`);
} catch (e) {
  console.error(`vqa-login failed: ${e.message}`);
  process.exit(1);
} finally {
  await ctx.close();
  await browser.close();
}

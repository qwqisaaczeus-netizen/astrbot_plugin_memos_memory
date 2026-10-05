const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");
const { PNG } = require("pngjs");
const pixelmatchModule = require("pixelmatch");
const pixelmatch = pixelmatchModule.default || pixelmatchModule;

const baseUrl = process.argv[2] || "http://127.0.0.1:8769";
const outputDir = process.argv[3] || path.join(process.cwd(), "browser-test11");
fs.mkdirSync(outputDir, { recursive: true });

function changedPixels(beforePath, afterPath) {
  const before = PNG.sync.read(fs.readFileSync(beforePath));
  const after = PNG.sync.read(fs.readFileSync(afterPath));
  if (before.width !== after.width || before.height !== after.height) return -1;
  return pixelmatch(before.data, after.data, null, before.width, before.height, { threshold: 0.08 });
}

(async () => {
  const edgeCandidates = [
    process.env.PLAYWRIGHT_EDGE_PATH,
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  ].filter(Boolean);
  const executablePath = edgeCandidates.find(candidate => fs.existsSync(candidate));
  const browser = await chromium.launch({ headless: true, executablePath });
  const results = [];
  try {
    for (const viewport of [
      { name: "desktop", width: 1440, height: 900 },
      { name: "tablet", width: 1024, height: 768 },
      { name: "mobile", width: 390, height: 844 },
    ]) {
      const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        ignoreHTTPSErrors: true,
      });
      const page = await context.newPage();
      const errors = [];
      page.on("pageerror", error => errors.push(String(error)));
      await page.goto(`${baseUrl}/house.html?preview=1`, { waitUntil: "domcontentloaded", timeout: 30000 });
      await page.waitForFunction(() => window.__houseRigDebugControl && window.__houseRigDebug, null, { timeout: 15000 });
      await page.evaluate(() => window.__houseRigDebugControl.stopTour());
      await page.waitForFunction(() => window.__houseRigDebug().live2d.loaded, null, { timeout: 30000 });
      const beforeState = await page.evaluate(() => window.__houseRigDebug().live2d);
      const beforePath = path.join(outputDir, `${viewport.name}-live2d-before.png`);
      const afterPath = path.join(outputDir, `${viewport.name}-live2d-after.png`);
      await page.locator("#character-live2d").screenshot({ path: beforePath });
      await page.waitForTimeout(1300);
      const idleState = await page.evaluate(() => window.__houseRigDebug().live2d);
      await page.locator("#character-live2d").screenshot({ path: afterPath });
      const characterBox = await page.locator("#character").boundingBox();
      if (!characterBox) throw new Error("character bounding box unavailable");
      await page.mouse.move(
        characterBox.x + characterBox.width * 0.93,
        characterBox.y + characterBox.height * 0.18,
      );
      await page.waitForTimeout(360);
      const pointerState = await page.evaluate(() => window.__houseRigDebug().live2d);
      const pointerPath = path.join(outputDir, `${viewport.name}-live2d-pointer-edge.png`);
      await page.locator("#character-live2d").screenshot({ path: pointerPath });
      await page.mouse.move(2, 2);
      await page.waitForTimeout(1100);
      const returnedState = await page.evaluate(() => window.__houseRigDebug().live2d);
      await page.locator("#character").click({ position: { x: 120, y: 220 } });
      await page.waitForTimeout(500);
      const actionState = await page.evaluate(() => window.__houseRigDebug().live2d);
      const fullPath = path.join(outputDir, `${viewport.name}-house.png`);
      await page.screenshot({ path: fullPath, fullPage: true });
      const valuesA = beforeState.micro_motion.values || {};
      const valuesB = idleState.micro_motion.values || {};
      const parameterDelta = Object.keys(valuesB).reduce(
        (sum, key) => sum + Math.abs(Number(valuesB[key] || 0) - Number(valuesA[key] || 0)), 0,
      );
      results.push({
        viewport,
        loaded: idleState.loaded,
        active: idleState.active,
        failed: idleState.failed,
        error: idleState.error,
        driven: idleState.micro_motion.driven_parameters.length,
        physics: idleState.micro_motion.physics_parameters.length,
        parameterDelta,
        action: actionState.micro_motion.action,
        pointerTracking: pointerState.micro_motion.pointer_tracking,
        pointerHead: pointerState.micro_motion.head_values,
        pointerLimits: pointerState.micro_motion.pointer_limits,
        returnedHead: returnedState.micro_motion.head_values,
        pointerSafe: (
          Math.abs(Number(pointerState.micro_motion.head_values.x || 0)) <= .75
          && Math.abs(Number(pointerState.micro_motion.head_values.y || 0)) <= .50
          && Math.abs(Number(pointerState.micro_motion.head_values.z || 0)) <= .35
          && Math.abs(Number(pointerState.micro_motion.head_values.eyeX || 0)) <= .15
          && Math.abs(Number(pointerState.micro_motion.head_values.eyeY || 0)) <= .11
        ),
        returnedToCenter: (
          Math.abs(Number(returnedState.micro_motion.head_values.x || 0)) <= .75
          && Math.abs(Number(returnedState.micro_motion.head_values.y || 0)) <= .50
          && Math.abs(Number(returnedState.micro_motion.head_values.z || 0)) <= .35
        ),
        changedPixels: changedPixels(beforePath, afterPath),
        pageErrors: errors,
        screenshot: fullPath,
      });
      await context.close();
    }
  } finally {
    await browser.close();
  }
  fs.writeFileSync(path.join(outputDir, "results.json"), JSON.stringify(results, null, 2));
  console.log(JSON.stringify(results, null, 2));
  if (results.some(item => !item.loaded || !item.active || item.failed || item.error || item.driven !== 8 || item.physics !== 4 || item.parameterDelta <= 0.001 || item.changedPixels <= 20 || !item.pointerTracking || !item.pointerSafe || !item.returnedToCenter || item.pageErrors.length)) {
    process.exitCode = 1;
  }
})();

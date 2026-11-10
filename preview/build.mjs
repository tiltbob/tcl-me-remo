// Bundles the preview harness into preview/.build/ (index.html, app.js, steam.css).
//
// Dev-only: the plugin bundle (dist/index.js) is built by rollup from src/index.tsx and never
// sees this directory. Usage: node preview/build.mjs   (or: pnpm run preview:build)

import { execFileSync } from "node:child_process";
import { copyFileSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import * as esbuild from "esbuild-wasm";

const here = dirname(fileURLToPath(import.meta.url));
const root = dirname(here);
export const outDir = join(here, ".build");

/** What the real service answers to get_config and status, straight from the Python code. */
function fixture() {
  let text;
  try {
    text = execFileSync("python3", [join(here, "make_fixture.py")], { cwd: root, encoding: "utf8" });
  } catch (error) {
    throw new Error(`preview/make_fixture.py failed (it needs python3 3.11+ and py_modules/): ${error.message}`);
  }
  return JSON.parse(text);
}

export async function buildPreview() {
  mkdirSync(outDir, { recursive: true });
  const data = JSON.stringify(fixture()).replace(/</g, "\\u003c");
  try {
    await esbuild.build({
      absWorkingDir: root,
      entryPoints: [join(here, "main.tsx")],
      outfile: join(outDir, "app.js"),
      bundle: true,
      format: "iife",
      target: "es2020",
      jsx: "automatic",
      sourcemap: "linked",
      logLevel: "warning",
      // The whole trick: the plugin's two Decky imports resolve to the stand-ins.
      alias: {
        "@decky/ui": join(here, "mocks", "decky-ui.tsx"),
        "@decky/api": join(here, "mocks", "decky-api.ts"),
      },
      define: { "process.env.NODE_ENV": '"development"' },
    });
  } finally {
    await esbuild.stop();
  }
  copyFileSync(join(here, "steam.css"), join(outDir, "steam.css"));
  writeFileSync(
    join(outDir, "index.html"),
    `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tcl-me-remo preview</title>
<link rel="stylesheet" href="steam.css">
</head>
<body data-view="qam">
<div id="root"></div>
<script>window.__FIXTURE__ = ${data};</script>
<script src="app.js"></script>
</body>
</html>
`,
  );
  return pathToFileURL(join(outDir, "index.html")).href;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const url = await buildPreview();
  console.log(`preview built: ${url}`);
  console.log("add ?scenario=default|unpaired|no-service|stopped|failed and &path=/tcl-me-remo/setup");
}

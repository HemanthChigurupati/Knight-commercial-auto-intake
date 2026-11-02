// Bundles src/ into dist/ (committed, so the server needs only Python).
//   npm install && npm run build        rebuild after changing src/
import * as esbuild from "esbuild";
import { cpSync, mkdirSync } from "node:fs";

mkdirSync("dist", { recursive: true });
cpSync("index.html", "dist/index.html");
cpSync("src/styles.css", "dist/styles.css");

const options = {
  entryPoints: ["src/main.jsx"],
  bundle: true,
  minify: true,
  sourcemap: false,
  jsx: "automatic",
  target: ["es2020"],
  outfile: "dist/app.js",
  define: { "process.env.NODE_ENV": '"production"' },
  logLevel: "info",
};

if (process.argv.includes("--watch")) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
} else {
  await esbuild.build(options);
}

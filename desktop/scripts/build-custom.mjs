import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const desktop = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repository = path.dirname(desktop);
function run(command, args, cwd = desktop) {
  const result = spawnSync(command, args, { cwd, stdio: "inherit" });
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(`${command} failed (${result.status})`);
}
function git(args) {
  const result = spawnSync("git", args, { cwd: repository, encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr);
  return result.stdout.trim();
}
if (git(["diff", "HEAD", "--name-only"])) throw new Error("请先提交本次源码变更，确保构建可追溯");
const untrackedInputs = git(["ls-files", "--others", "--exclude-standard", "--", "backend", "desktop", "frontend"])
  .split("\n")
  .filter((name) => name && !name.endsWith("/.DS_Store"));
if (untrackedInputs.length) throw new Error(`存在未提交的构建输入：${untrackedInputs.join(", ")}`);
const sourceCommit = git(["rev-parse", "HEAD"]);
const version = JSON.parse(readFileSync(path.join(desktop, "package.json"), "utf8")).version;
const output = mkdtempSync(path.join(tmpdir(), "openfic-custom-wheel-"));
run("uv", ["build", "--wheel", "--out-dir", output], path.join(repository, "backend"));
const wheels = readdirSync(output).filter((name) => name.endsWith(".whl"));
if (wheels.length !== 1 || !wheels[0].startsWith(`openfic-${version}-`)) {
  throw new Error("后端 wheel 与桌面版本不匹配");
}
const directory = path.join(desktop, "resources", "custom-backend");
mkdirSync(directory, { recursive: true });
copyFileSync(path.join(output, wheels[0]), path.join(directory, wheels[0]));
const sha256 = createHash("sha256").update(readFileSync(path.join(directory, wheels[0]))).digest("hex");
writeFileSync(path.join(directory, "manifest.json"), `${JSON.stringify({ version, sourceCommit, upstreamTag: git(["describe", "--tags", "--match", "v*", "--abbrev=0"]), wheel: wheels[0], sha256 }, null, 2)}\n`);
run("pnpm", ["build:setup"]);
run("pnpm", ["build:main"]);
run("pnpm", ["exec", "electron-builder", "--config", "electron-builder.yml", "--mac", "--arm64", "--dir", "--publish", "never"]);
const application = path.join(desktop, "dist-electron", "mac-arm64", "OpenFic.app");
run("codesign", ["--force", "--deep", "--sign", "-", application]);
run("codesign", ["--verify", "--deep", application]);
console.log(`Custom OpenFic ${version} built from ${sourceCommit}`);

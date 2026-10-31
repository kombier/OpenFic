import { app } from "electron";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import path from "node:path";

export interface BundledBackend {
  wheelPath: string;
  sha256: string;
  buildId: string;
}

export async function getBundledBackend(expectedVersion: string): Promise<BundledBackend> {
  const directory = app.isPackaged
    ? path.join(process.resourcesPath, "custom-backend")
    : path.join(app.getAppPath(), "resources", "custom-backend");
  const manifest = JSON.parse(await readFile(path.join(directory, "manifest.json"), "utf8")) as {
    version: string;
    wheel: string;
    sha256: string;
    buildId: string;
  };
  if (
    manifest.version !== expectedVersion ||
    !/^[a-f0-9-]{36}$/.test(manifest.buildId) ||
    typeof manifest.wheel !== "string" ||
    path.basename(manifest.wheel) !== manifest.wheel ||
    !manifest.wheel.endsWith(".whl") ||
    !/^[a-f0-9]{64}$/.test(manifest.sha256)
  ) {
    throw new Error("定制后端清单与当前 App 不匹配，请重新构建完整安装包");
  }
  const wheelPath = path.join(directory, manifest.wheel);
  const digest = createHash("sha256").update(await readFile(wheelPath)).digest("hex");
  if (digest !== manifest.sha256) throw new Error("定制后端 wheel 校验失败，拒绝改装官方后端");
  return { wheelPath, sha256: digest, buildId: manifest.buildId };
}

interface SpawnCommand {
  command: string;
  args: string[];
}

export function resolveOpenFicCliPath(venvPythonPath: string): string {
  return process.platform === "win32"
    ? venvPythonPath.replace(/python\.exe$/i, "openfic.exe")
    : venvPythonPath.replace(/python$/i, "openfic");
}
export function createOpenFicVersionCommand(venvPythonPath: string): SpawnCommand {
  return {
    command: venvPythonPath,
    args: ["-c", 'from importlib.metadata import version; print(version("openfic"))'],
  };
}

export function createOpenFicInstallCommand(
  venvPythonPath: string,
  version: string,
  forceReinstall = false,
  wheelPath?: string,
): Omit<SpawnCommand, "command"> {
  return {
    args: ["pip", "install", "--python", venvPythonPath, ...(forceReinstall ? ["--reinstall-package", "openfic"] : []), wheelPath ?? `openfic==${version}`],
  };
}

export function createOpenFicBuildIdentityCommand(venvPythonPath: string): SpawnCommand {
  return {
    command: venvPythonPath,
    args: [
      "-c",
      'import json; from importlib.metadata import distribution; print(json.loads(distribution("openfic").locate_file("app/_custom_build.json").read_text())["buildId"])',
    ],
  };
}

export function createOpenFicServeCommand(venvPythonPath: string, port: number): SpawnCommand {
  return {
    command: resolveOpenFicCliPath(venvPythonPath),
    args: ["serve", "--host", "0.0.0.0", "--port", String(port)],
  };
}

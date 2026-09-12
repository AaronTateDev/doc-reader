#!/usr/bin/env node
import { spawn, spawnSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const launcher = join(packageRoot, "run-doc-reader");
const isWindows = process.platform === "win32";
const windowsLauncher = join(packageRoot, "run-doc-reader.ps1");
const managedRoot = join(homedir(), ".doc-reader-managed");
const nativeApp = join(managedRoot, "Doc Reader.app");
const nativeExecutable = join(nativeApp, "Contents", "MacOS", "DocReader");
const launchAgentLabel = "com.docreader.tray";
const launchAgentPlist = join(homedir(), "Library", "LaunchAgents", `${launchAgentLabel}.plist`);
const servicesItem = join(homedir(), "Library", "Services", "Read with Doc Reader.workflow");
const stdoutLog = join(homedir(), "Library", "Logs", "doc-reader-tray.log");
const stderrLog = join(homedir(), "Library", "Logs", "doc-reader-tray.err.log");
const userApplicationsDir = join(homedir(), "Applications");
const applicationsApp = join(userApplicationsDir, "Doc Reader.app");
const webPort = Number(process.env.DOC_READER_WEB_PORT || 8766);
const webHost = process.env.DOC_READER_WEB_HOST || "127.0.0.1";
const webLaunchAgentLabel = "com.docreader.web";
const webLaunchAgentPlist = join(homedir(), "Library", "LaunchAgents", `${webLaunchAgentLabel}.plist`);
const webStdoutLog = join(homedir(), "Library", "Logs", "doc-reader-web.log");
const webStderrLog = join(homedir(), "Library", "Logs", "doc-reader-web.err.log");
const readinessStatusFile = process.env.DOC_READER_READINESS_STATUS_FILE || join(managedRoot, "tailnet-readiness.json");
const ttsMacPort = Number(process.env.DOC_READER_TTS_MAC_PORT || 8772);
const ttsMacHost = process.env.DOC_READER_TTS_MAC_HOST || "127.0.0.1";
const ttsMacUrl = process.env.DOC_READER_TTS_MAC_URL || `http://${ttsMacHost}:${ttsMacPort}`;
const ttsMacDevice = process.env.DOC_READER_TTS_MAC_DEVICE || "cpu";
const ttsMacSttModel = process.env.DOC_READER_MAC_STT_MODEL || process.env.DOC_READER_STT_MODEL || "base";
const ttsMacSttComputeType = process.env.DOC_READER_MAC_STT_COMPUTE_TYPE || process.env.DOC_READER_STT_COMPUTE_TYPE || "int8";
const ttsUmbraUrl = process.env.DOC_READER_TTS_UMBRA_URL || "http://100.72.151.28:8771";
const ttsUmbraHost = process.env.DOC_READER_UMBRA_SSH_HOST || "Umbra";
const ttsUmbraRoot = process.env.DOC_READER_UMBRA_TTS_ROOT || "C:/Users/codyr/.doc-reader-tts";
const shouldAutoStartRemoteSpeech = process.env.DOC_READER_REMOTE_SPEECH_AUTOSTART === "1";
const shouldCheckRemoteSpeech = shouldAutoStartRemoteSpeech || process.env.DOC_READER_REMOTE_SPEECH_CHECK === "1";
const ttsLaunchAgentLabel = "com.docreader.tts-local";
const ttsLaunchAgentPlist = join(homedir(), "Library", "LaunchAgents", `${ttsLaunchAgentLabel}.plist`);
const ttsStdoutLog = join(homedir(), "Library", "Logs", "doc-reader-tts-local.log");
const ttsStderrLog = join(homedir(), "Library", "Logs", "doc-reader-tts-local.err.log");
const ttsLocalRoot = join(managedRoot, "tts-local");
const ttsLocalVenv = join(ttsLocalRoot, ".venv");
const ttsLocalPython = join(ttsLocalVenv, "bin", "python");
const ttsMacEngines = "kokoro,whisper";
const launchServicesRegister = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister";
const spacyEnglishModelUrl = "https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl";
const cacheRoot =
  process.env.DOC_READER_CACHE_DIR ||
  process.env.XDG_CACHE_HOME ||
  (process.platform === "darwin"
    ? join(homedir(), "Library", "Caches")
    : join(homedir(), ".cache"));
const venvDir = process.env.DOC_READER_VENV_DIR || join(cacheRoot, "doc-reader", "npm-venv");

const cliEnv = {
  ...process.env,
  DOC_READER_VENV_DIR: venvDir,
  PYTHONPATH: packageRoot + (process.env.PYTHONPATH ? `:${process.env.PYTHONPATH}` : ""),
};
const scriptEnv = { ...process.env };

function usage() {
  console.log(`read-docs

macOS app bootstrapper:
  read-docs install          Install/update the managed app agent and Services item
  read-docs start            Start the installed menu-bar agent
  read-docs open             Open the installed Doc Reader app
  read-docs dock             Refresh ~/Applications/Doc Reader.app for Dock/Spotlight use
  read-docs restart          Refresh the managed copy and restart the agent
  read-docs stop             Stop the running agent without uninstalling it
  read-docs status           Show managed app, LaunchAgent, and Services status
  read-docs doctor           Check app, tailnet, and speech backend readiness
  read-docs ensure           Start configured tailnet/speech dependencies and check readiness
  read-docs uninstall        Remove LaunchAgent and Services integration
  read-docs web              Run the local web app in the foreground
  read-docs web-start        Install/start the local web app agent on 127.0.0.1:${webPort}
  read-docs web-stop         Stop the local web app agent
  read-docs web-status       Show local web app health
  read-docs tailscale        Start web app and expose it on the tailnet at :${webPort}
  read-docs tts-umbra-install Install/update the remote speech service
  read-docs tts-umbra-start   Start the remote speech service
  read-docs tts-umbra-stop    Stop the remote speech service
  read-docs tts-umbra-status  Show remote speech service health
  read-docs tts-mac-start     Install/start local Mac Kokoro/Whisper service
  read-docs tts-mac-stop      Stop local Mac Kokoro/Whisper service
  read-docs tts-mac-status    Show local Mac Kokoro/Whisper service health
  read-docs tts-status        Show all DocReader TTS service health
  read-docs tts-bench         Benchmark Chatterbox/Kokoro/macOS voices
  read-docs tts-samples       Generate benchmark sample audio files

Document CLI:
  read-docs <file> [options] Stream a document through the local GPU-first reader
  read-docs cli <file> [...] Same as above, explicit CLI form

Windows (run from a source checkout; uses .venv next to bin/):
  read-docs install          Build the Python env (CUDA PyTorch if NVIDIA), start services
  read-docs start            Start local Kokoro/Whisper service, web app, tray helper
  read-docs stop             Stop all Doc Reader processes
  read-docs restart          Stop then start
  read-docs status           Show service health (also web-status, tts-status)
  read-docs doctor           Check Python, CUDA, Kokoro, ffmpeg, espeak, microphone
  read-docs open             Open the web app, starting services if needed
  read-docs enable-startup   Launch at login (Startup folder shortcut)
  read-docs disable-startup  Remove the login shortcut
  read-docs web              Run the web app in the foreground
  read-docs tts-local-start  Start only the local speech service (also tts-mac-start)

The macOS app path uses launchd and a native menu-bar app; the Windows path uses
run-doc-reader.ps1 and a PySide6 tray helper. Linux can use the document CLI.`);
}

const windowsCommandMap = {
  install: ["start"],
  start: ["start"],
  app: ["start"],
  "--tray": ["start"],
  open: ["open"],
  restart: ["restart"],
  stop: ["stop"],
  status: ["status"],
  doctor: ["doctor"],
  ensure: ["start", "--no-open"],
  web: ["run-web"],
  "web-start": ["web-start"],
  "web-stop": ["web-stop"],
  "web-status": ["status"],
  "tts-local-start": ["tts-start"],
  "tts-local-stop": ["tts-stop"],
  "tts-local-status": ["status"],
  "tts-mac-start": ["tts-start"],
  "tts-mac-stop": ["tts-stop"],
  "tts-mac-status": ["status"],
  "tts-status": ["status"],
  "enable-startup": ["enable-startup"],
  "disable-startup": ["disable-startup"],
  uninstall: ["stop", "--then-disable-startup"],
  remove: ["stop", "--then-disable-startup"],
};

function runWindowsLauncher(launcherArgs) {
  return run("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", windowsLauncher, ...launcherArgs], {
    env: scriptEnv,
  });
}

async function runWindowsCommand(commandName, extraArgs = []) {
  const mapped = windowsCommandMap[commandName];
  if (!mapped) {
    return null;
  }
  if (mapped[1] === "--then-disable-startup") {
    const stopCode = await runWindowsLauncher(["stop"]);
    const disableCode = await runWindowsLauncher(["disable-startup"]);
    return stopCode || disableCode;
  }
  return runWindowsLauncher([...mapped, ...extraArgs]);
}

function run(command, args, options = {}) {
  return new Promise((resolveExitCode) => {
    const child = spawn(command, args, {
      env: options.env || process.env,
      stdio: "inherit",
    });

    child.on("error", (error) => {
      console.error(`[doc-reader] Failed to start ${command}: ${error.message}`);
      resolveExitCode(1);
    });

    child.on("exit", (code, signal) => {
      if (signal) {
        process.kill(process.pid, signal);
        return;
      }
      resolveExitCode(code ?? 0);
    });
  });
}

function runSilent(command, args, options = {}) {
  const result = spawnSync(command, args, {
    env: options.env || process.env,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  return {
    status: result.status ?? 1,
    stdout: result.stdout || "",
    stderr: result.stderr || "",
    error: result.error,
  };
}

function runCapture(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      env: options.env || process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timeout = options.timeoutMs
      ? setTimeout(() => {
          child.kill("SIGTERM");
        }, options.timeoutMs)
      : null;

    child.stdout.on("data", (chunk) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.on("error", (error) => {
      if (timeout) {
        clearTimeout(timeout);
      }
      reject(error);
    });
    child.on("exit", (code, signal) => {
      if (timeout) {
        clearTimeout(timeout);
      }
      if (code === 0) {
        resolve({ stdout, stderr, code: 0 });
        return;
      }
      const error = new Error(stderr.trim() || stdout.trim() || `${command} exited with ${signal || code}`);
      error.stdout = stdout;
      error.stderr = stderr;
      error.code = code ?? 1;
      reject(error);
    });
  });
}

function macOnly(commandName) {
  if (process.platform === "darwin") {
    return true;
  }
  console.error(`[doc-reader] '${commandName}' is only supported by the macOS app bootstrapper.`);
  console.error("[doc-reader] Use 'read-docs <file>' for the cross-platform document CLI.");
  return false;
}

function uid() {
  return typeof process.getuid === "function" ? process.getuid() : process.env.UID;
}

function launchAgentTarget() {
  return `gui/${uid()}/${launchAgentLabel}`;
}

function launchAgentDomain() {
  return `gui/${uid()}`;
}

function webLaunchAgentTarget() {
  return `gui/${uid()}/${webLaunchAgentLabel}`;
}

function ttsLaunchAgentTarget() {
  return `gui/${uid()}/${ttsLaunchAgentLabel}`;
}

function isLaunchAgentLoaded() {
  const result = spawnSync("launchctl", ["print", launchAgentTarget()], {
    stdio: ["ignore", "ignore", "ignore"],
  });
  return result.status === 0;
}

function launchAgentInfo(target) {
  const result = spawnSync("launchctl", ["print", target], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  if (result.status !== 0 || !result.stdout) {
    return { loaded: false, running: false, state: "not loaded", pid: "" };
  }
  const state = (result.stdout.match(/^\s*state = (.+)$/m) || [])[1] || "loaded";
  const pid = (result.stdout.match(/^\s*pid = (\d+)$/m) || [])[1] || "";
  return {
    loaded: true,
    running: state === "running",
    state,
    pid,
  };
}

function launchAgentStateLabel(target) {
  if (process.platform !== "darwin") {
    return "not available";
  }
  const info = launchAgentInfo(target);
  if (!info.loaded) {
    return "not running";
  }
  if (info.running) {
    return info.pid ? `running (pid ${info.pid})` : "running";
  }
  return `not running (${info.state})`;
}

function isWebLaunchAgentLoaded() {
  const result = spawnSync("launchctl", ["print", webLaunchAgentTarget()], {
    stdio: ["ignore", "ignore", "ignore"],
  });
  return result.status === 0;
}

function isTtsLaunchAgentLoaded() {
  const result = spawnSync("launchctl", ["print", ttsLaunchAgentTarget()], {
    stdio: ["ignore", "ignore", "ignore"],
  });
  return result.status === 0;
}

function processSnapshot() {
  const result = spawnSync("ps", ["-axww", "-o", "pid=,ppid=,command="], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  if (result.status !== 0 || !result.stdout) {
    return [];
  }
  return result.stdout
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      const match = line.match(/^(\d+)\s+(\d+)\s+(.+)$/);
      if (!match) {
        return null;
      }
      return {
        pid: Number(match[1]),
        ppid: Number(match[2]),
        command: match[3],
      };
    })
    .filter(Boolean);
}

function descendantPids(rootPid, snapshot) {
  const childrenByParent = new Map();
  for (const entry of snapshot) {
    if (!childrenByParent.has(entry.ppid)) {
      childrenByParent.set(entry.ppid, []);
    }
    childrenByParent.get(entry.ppid).push(entry.pid);
  }

  const descendants = [];
  const queue = [...(childrenByParent.get(rootPid) || [])];
  while (queue.length > 0) {
    const pid = queue.shift();
    descendants.push(pid);
    queue.push(...(childrenByParent.get(pid) || []));
  }
  return descendants;
}

function killPids(pids, signal = null) {
  const unique = [...new Set(pids)].filter((pid) => pid > 0 && pid !== process.pid);
  if (unique.length === 0) {
    return;
  }
  const args = signal ? [`-${signal}`, ...unique.map(String)] : unique.map(String);
  spawnSync("kill", args, { stdio: ["ignore", "ignore", "ignore"] });
}

async function cleanupReaderProcesses() {
  if (process.platform !== "darwin") {
    return 0;
  }

  const snapshot = processSnapshot();
  const readerRoots = snapshot.filter((entry) => {
    if (entry.pid === process.pid) {
      return false;
    }
    return entry.command.includes(" -m doc_reader ");
  });

  if (readerRoots.length === 0) {
    return 0;
  }

  const targets = [];
  for (const root of readerRoots) {
    targets.push(...descendantPids(root.pid, snapshot).reverse(), root.pid);
  }

  killPids(targets, "TERM");
  await new Promise((resolve) => setTimeout(resolve, 500));

  const remaining = new Set(processSnapshot().map((entry) => entry.pid));
  killPids(targets.filter((pid) => remaining.has(pid)), "KILL");
  console.log(`[doc-reader] Cleaned up ${readerRoots.length} reader process(es).`);
  return 0;
}

async function cleanupNativeAppProcesses({ keepLaunchAgent = false } = {}) {
  if (process.platform !== "darwin") {
    return 0;
  }

  const keepPid = keepLaunchAgent ? Number(launchAgentInfo(launchAgentTarget()).pid || 0) : 0;
  const snapshot = processSnapshot();
  const appRoots = snapshot.filter((entry) => {
    if (entry.pid === process.pid || entry.pid === keepPid) {
      return false;
    }
    return (
      entry.command.includes("/Doc Reader.app/Contents/MacOS/DocReader") ||
      entry.command.includes("/DocReader.app/Contents/MacOS/DocReader")
    );
  });

  if (appRoots.length === 0) {
    return 0;
  }

  const targets = [];
  for (const root of appRoots) {
    targets.push(...descendantPids(root.pid, snapshot).reverse(), root.pid);
  }

  killPids(targets, "TERM");
  await new Promise((resolve) => setTimeout(resolve, 500));

  const remaining = new Set(processSnapshot().map((entry) => entry.pid));
  killPids(targets.filter((pid) => remaining.has(pid)), "KILL");
  console.log(`[doc-reader] Cleaned up ${appRoots.length} native app process(es).`);
  return 0;
}

async function cleanupWebProcesses() {
  if (process.platform !== "darwin") {
    return 0;
  }

  const snapshot = processSnapshot();
  const webRoots = snapshot.filter((entry) => {
    if (entry.pid === process.pid) {
      return false;
    }
    return entry.command.includes(" -m doc_reader.webapp");
  });

  if (webRoots.length === 0) {
    return 0;
  }

  const targets = [];
  for (const root of webRoots) {
    targets.push(...descendantPids(root.pid, snapshot).reverse(), root.pid);
  }

  killPids(targets, "TERM");
  await new Promise((resolve) => setTimeout(resolve, 500));

  const remaining = new Set(processSnapshot().map((entry) => entry.pid));
  killPids(targets.filter((pid) => remaining.has(pid)), "KILL");
  console.log(`[doc-reader] Cleaned up ${webRoots.length} web process(es).`);
  return 0;
}

async function runScript(name, args = []) {
  return run(join(packageRoot, name), args, { env: scriptEnv });
}

async function prepareManagedRuntime() {
  return runScript("enable-startup", ["--prepare-managed-only"]);
}

function bundleIdentifier(appPath) {
  const infoPlist = join(appPath, "Contents", "Info.plist");
  const result = spawnSync("/usr/libexec/PlistBuddy", ["-c", "Print :CFBundleIdentifier", infoPlist], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  return result.status === 0 ? result.stdout.trim() : "";
}

function applicationsAppState() {
  if (!existsSync(applicationsApp)) {
    return "missing";
  }
  try {
    const existing = lstatSync(applicationsApp);
    if (existing.isSymbolicLink()) {
      return "needs refresh (symlink)";
    }
    if (!existing.isDirectory()) {
      return "unexpected path";
    }
    return "installed";
  } catch {
    return "unknown";
  }
}

function installApplicationsApp() {
  if (!macOnly("dock")) {
    return 1;
  }
  if (!existsSync(nativeApp)) {
    console.error("[doc-reader] Managed app is missing. Run 'read-docs install' first.");
    return 1;
  }
  mkdirSync(userApplicationsDir, { recursive: true });
  if (existsSync(applicationsApp)) {
    let existing;
    try {
      existing = lstatSync(applicationsApp);
    } catch {
      existing = null;
    }
    if (existing?.isSymbolicLink()) {
      unlinkSync(applicationsApp);
    } else if (existing?.isDirectory()) {
      const existingBundleId = bundleIdentifier(applicationsApp);
      if (existingBundleId !== "com.sproutseeds.read-docs") {
        console.error(`[doc-reader] Applications app already exists and is not Doc Reader: ${applicationsApp}`);
        return 1;
      }
      rmSync(applicationsApp, { recursive: true, force: true });
    } else {
      console.error(`[doc-reader] Applications app path already exists and is not an app bundle: ${applicationsApp}`);
      return 1;
    }
  }
  cpSync(nativeApp, applicationsApp, { recursive: true });
  registerInstalledApp();
  console.log(`[doc-reader] Applications app ready: ${applicationsApp}`);
  console.log("[doc-reader] Launch it from Applications, Spotlight, the Dock, or 'read-docs open'.");
  return 0;
}

function registerInstalledApp() {
  if (process.platform !== "darwin" || !existsSync(nativeApp) || !existsSync(launchServicesRegister)) {
    return;
  }
  spawnSync(launchServicesRegister, ["-f", nativeApp], {
    stdio: ["ignore", "ignore", "ignore"],
  });
  if (existsSync(applicationsApp)) {
    spawnSync(launchServicesRegister, ["-f", applicationsApp], {
      stdio: ["ignore", "ignore", "ignore"],
    });
  }
}

async function openInstalledApp() {
  if (!macOnly("open")) {
    return 1;
  }
  const appToOpen = existsSync(applicationsApp) ? applicationsApp : nativeApp;
  if (!existsSync(appToOpen)) {
    console.error("[doc-reader] Managed app is missing. Run 'read-docs install' first.");
    return 1;
  }
  return run("open", [appToOpen]);
}

async function installApp() {
  if (!macOnly("install")) {
    return 1;
  }
  await cleanupNativeAppProcesses();
  const appExitCode = await runScript("enable-startup");
  if (appExitCode !== 0) {
    return appExitCode;
  }
  registerInstalledApp();
  const dockExitCode = applicationsAppState() === "installed" ? 0 : installApplicationsApp();
  const serviceExitCode = await runScript("install-context-menu-service");
  const webExitCode = await startWebAgent();
  const macSpeechExitCode = await refreshMacSpeechLaunchAgentIfNeeded();
  await ensureDocReaderReadiness({ startDependencies: true, quiet: false });
  const nativeCleanupExitCode = await cleanupNativeAppProcesses({ keepLaunchAgent: true });
  return dockExitCode || serviceExitCode || webExitCode || macSpeechExitCode || nativeCleanupExitCode;
}

async function restartApp() {
  if (!macOnly("restart")) {
    return 1;
  }
  await cleanupNativeAppProcesses();
  const appExitCode = await runScript("enable-startup");
  if (appExitCode !== 0) {
    return appExitCode;
  }
  registerInstalledApp();
  const dockExitCode = applicationsAppState() === "installed" ? 0 : installApplicationsApp();
  const webExitCode = await startWebAgent();
  const macSpeechExitCode = await refreshMacSpeechLaunchAgentIfNeeded();
  await ensureDocReaderReadiness({ startDependencies: true, quiet: false });
  const nativeCleanupExitCode = await cleanupNativeAppProcesses({ keepLaunchAgent: true });
  return dockExitCode || webExitCode || macSpeechExitCode || nativeCleanupExitCode;
}

async function startAgent() {
  if (!macOnly("start")) {
    return 1;
  }
  if (!existsSync(launchAgentPlist)) {
    console.error("[doc-reader] App agent is not installed yet.");
    console.error("[doc-reader] Run 'read-docs install' first.");
    return 1;
  }
  const webExitCode = await startWebAgent();
  const macSpeechExitCode = await refreshMacSpeechLaunchAgentIfNeeded();
  await ensureDocReaderReadiness({ startDependencies: true, quiet: false });
  if (!isLaunchAgentLoaded()) {
    const bootstrapExitCode = await run("launchctl", [
      "bootstrap",
      launchAgentDomain(),
      launchAgentPlist,
    ]);
    if (bootstrapExitCode !== 0 && !isLaunchAgentLoaded()) {
      return bootstrapExitCode;
    }
  }
  spawnSync("launchctl", ["enable", launchAgentTarget()], {
    stdio: ["ignore", "ignore", "ignore"],
  });
  const agentExitCode = await run("launchctl", ["kickstart", "-k", launchAgentTarget()]);
  const nativeCleanupExitCode = await cleanupNativeAppProcesses({ keepLaunchAgent: true });
  return webExitCode || macSpeechExitCode || agentExitCode || nativeCleanupExitCode;
}

async function stopAgent() {
  if (!macOnly("stop")) {
    return 1;
  }
  if (!isLaunchAgentLoaded()) {
    console.log("[doc-reader] App agent is not running.");
    return cleanupReaderProcesses();
  }
  const agentExitCode = await run("launchctl", ["bootout", launchAgentTarget()]);
  const cleanupExitCode = await cleanupReaderProcesses();
  const nativeCleanupExitCode = await cleanupNativeAppProcesses();
  return agentExitCode || cleanupExitCode || nativeCleanupExitCode;
}

function webLocalUrl() {
  return `http://${webHost}:${webPort}`;
}

async function waitForWebHealth(timeoutMs = 30000) {
  const started = Date.now();
  while (Date.now() - started <= timeoutMs) {
    try {
      const payload = await fetchJson(`${webLocalUrl()}/healthz`, 1500);
      if (payload.ok) {
        return true;
      }
    } catch {
      // Keep polling until launchd has finished starting the web app.
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return false;
}

function tailscaleHost() {
  const result = spawnSync("tailscale", ["status", "--json"], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  if (result.status !== 0 || !result.stdout) {
    return "";
  }
  try {
    const payload = JSON.parse(result.stdout);
    return String(payload?.Self?.DNSName || "").replace(/\.$/, "");
  } catch {
    return "";
  }
}

function writeWebLaunchAgent() {
  const python = join(managedRoot, ".venv", "bin", "python");
  mkdirSync(dirname(webLaunchAgentPlist), { recursive: true });
  mkdirSync(dirname(webStdoutLog), { recursive: true });
  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${webLaunchAgentLabel}</string>

    <key>ProgramArguments</key>
    <array>
      <string>${python}</string>
      <string>-m</string>
      <string>doc_reader.webapp</string>
      <string>--host</string>
      <string>${webHost}</string>
      <string>--port</string>
      <string>${webPort}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ProcessType</key>
    <string>Interactive</string>

    <key>StandardOutPath</key>
    <string>${webStdoutLog}</string>

    <key>StandardErrorPath</key>
    <string>${webStderrLog}</string>

    <key>WorkingDirectory</key>
    <string>${managedRoot}</string>

    <key>EnvironmentVariables</key>
    <dict>
      <key>DOC_READER_VENV_DIR</key>
      <string>${join(managedRoot, ".venv")}</string>
      <key>DOC_READER_MANAGED_ROOT</key>
      <string>${managedRoot}</string>
      <key>PYTHONPATH</key>
      <string>${managedRoot}</string>
      <key>DOC_READER_TTS_UMBRA_URL</key>
      <string>${ttsUmbraUrl}</string>
      <key>DOC_READER_TTS_MAC_URL</key>
      <string>${ttsMacUrl}</string>
    </dict>
  </dict>
</plist>
`;
  writeFileSync(webLaunchAgentPlist, plist);
}

function writeTtsLaunchAgent() {
  mkdirSync(dirname(ttsLaunchAgentPlist), { recursive: true });
  mkdirSync(dirname(ttsStdoutLog), { recursive: true });
  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${ttsLaunchAgentLabel}</string>

    <key>ProgramArguments</key>
    <array>
      <string>${ttsLocalPython}</string>
      <string>-m</string>
      <string>doc_reader.tts_service</string>
      <string>--host</string>
      <string>${ttsMacHost}</string>
      <string>--port</string>
      <string>${ttsMacPort}</string>
      <string>--engines</string>
      <string>${ttsMacEngines}</string>
      <string>--device</string>
      <string>${ttsMacDevice}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ProcessType</key>
    <string>Interactive</string>

    <key>StandardOutPath</key>
    <string>${ttsStdoutLog}</string>

    <key>StandardErrorPath</key>
    <string>${ttsStderrLog}</string>

    <key>WorkingDirectory</key>
    <string>${managedRoot}</string>

    <key>EnvironmentVariables</key>
    <dict>
      <key>PYTHONPATH</key>
      <string>${managedRoot}</string>
      <key>DOC_READER_TTS_MAC_URL</key>
      <string>${ttsMacUrl}</string>
      <key>DOC_READER_TTS_DEVICE</key>
      <string>${ttsMacDevice}</string>
      <key>DOC_READER_STT_MODEL</key>
      <string>${ttsMacSttModel}</string>
      <key>DOC_READER_STT_COMPUTE_TYPE</key>
      <string>${ttsMacSttComputeType}</string>
      <key>DOC_READER_STT_PRELOAD</key>
      <string>0</string>
    </dict>
  </dict>
</plist>
`;
  writeFileSync(ttsLaunchAgentPlist, plist);
}

function macSpeechLaunchAgentNeedsRefresh() {
  if (!existsSync(ttsLaunchAgentPlist)) {
    return false;
  }
  try {
    const plist = readFileSync(ttsLaunchAgentPlist, "utf8");
    return (
      !plist.includes(`<string>${ttsMacEngines}</string>`) ||
      !plist.includes("<key>DOC_READER_STT_MODEL</key>") ||
      !plist.includes("<key>DOC_READER_STT_COMPUTE_TYPE</key>")
    );
  } catch (_error) {
    return true;
  }
}

async function refreshMacSpeechLaunchAgentIfNeeded() {
  if (!macSpeechLaunchAgentNeedsRefresh()) {
    return 0;
  }
  console.log("[doc-reader] Refreshing Mac speech service for local Whisper fallback.");
  return startMacTts();
}

async function ensureMacTtsRuntime() {
  if (!macOnly("tts-mac-start")) {
    return 1;
  }
  const prepareExitCode = await prepareManagedRuntime();
  if (prepareExitCode !== 0) {
    return prepareExitCode;
  }
  mkdirSync(ttsLocalRoot, { recursive: true });
  if (spawnSync("which", ["espeak-ng"], { stdio: ["ignore", "ignore", "ignore"] }).status !== 0) {
    if (spawnSync("which", ["brew"], { stdio: ["ignore", "ignore", "ignore"] }).status === 0) {
      const brewExitCode = await run("brew", ["install", "espeak-ng"]);
      if (brewExitCode !== 0) {
        return brewExitCode;
      }
    } else {
      console.error("[doc-reader] espeak-ng is required for Kokoro and Homebrew was not found.");
      return 1;
    }
  }
  const venvExitCode = await run("uv", ["venv", "--python", "3.11", ttsLocalVenv]);
  if (venvExitCode !== 0) {
    return venvExitCode;
  }
  return run("uv", [
    "pip",
    "install",
    "--python",
    ttsLocalPython,
    "kokoro",
    "faster-whisper",
    "soundfile",
    "torch",
    "torchvision",
    "torchaudio",
    spacyEnglishModelUrl,
  ]);
}

async function startMacTts() {
  const runtimeExitCode = await ensureMacTtsRuntime();
  if (runtimeExitCode !== 0) {
    return runtimeExitCode;
  }
  writeTtsLaunchAgent();
  if (isTtsLaunchAgentLoaded()) {
    await run("launchctl", ["bootout", ttsLaunchAgentTarget()]);
    await new Promise((resolve) => setTimeout(resolve, 600));
  }
  const bootstrapExitCode = await run("launchctl", [
    "bootstrap",
    launchAgentDomain(),
    ttsLaunchAgentPlist,
  ]);
  if (bootstrapExitCode !== 0 && !isTtsLaunchAgentLoaded()) {
    return bootstrapExitCode;
  }
  spawnSync("launchctl", ["enable", ttsLaunchAgentTarget()], {
    stdio: ["ignore", "ignore", "ignore"],
  });
  const kickstartExitCode = await run("launchctl", ["kickstart", "-k", ttsLaunchAgentTarget()]);
  if (kickstartExitCode === 0) {
    console.log(`[doc-reader] Mac TTS: ${ttsMacUrl}`);
  }
  return kickstartExitCode;
}

async function stopMacTts() {
  if (!macOnly("tts-mac-stop")) {
    return 1;
  }
  if (!isTtsLaunchAgentLoaded()) {
    console.log("[doc-reader] Mac TTS agent is not running.");
    return 0;
  }
  return run("launchctl", ["bootout", ttsLaunchAgentTarget()]);
}

async function healthLine(label, url) {
  try {
    const started = Date.now();
    const payload = await fetchJson(`${url.replace(/\/$/, "")}/healthz`, 3000);
    const elapsed = Date.now() - started;
    const device = payload.device?.cuda_device || payload.device?.requested || "unknown";
    return `${label}: ${payload.ok ? "ok" : "not ok"} (${elapsed}ms, ${device}) ${url}`;
  } catch (error) {
    return `${label}: not reachable (${error.message}) ${url}`;
  }
}

async function fetchJson(url, timeoutMs = 3000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(url, { signal: controller.signal });
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

async function macTtsStatus() {
  const lines = [
    "Doc Reader Mac TTS status",
    `Local URL: ${ttsMacUrl}`,
    `LaunchAgent: ${existsSync(ttsLaunchAgentPlist) ? "installed" : "missing"} (${ttsLaunchAgentPlist})`,
    `Agent state: ${launchAgentStateLabel(ttsLaunchAgentTarget())}`,
    `Logs: ${ttsStdoutLog}`,
    `Errors: ${ttsStderrLog}`,
    await healthLine("Health", ttsMacUrl),
  ];
  console.log(lines.join("\n"));
  return 0;
}

function umbraRootWin() {
  return ttsUmbraRoot.replace(/\//g, "\\");
}

function umbraScriptPath(name) {
  return `${ttsUmbraRoot}/${name}`;
}

async function runUmbra(command) {
  return run("ssh", [ttsUmbraHost, "cmd", "/c", command]);
}

async function copyUmbraFile(localPath, remotePath) {
  return run("scp", [localPath, `${ttsUmbraHost}:${remotePath}`]);
}

function writeUmbraTempFiles() {
  const tempRoot = join(tmpdir(), `doc-reader-tts-${process.pid}`);
  mkdirSync(tempRoot, { recursive: true });
  const runCmd = join(tempRoot, "run-tts.cmd");
  const stopPs1 = join(tempRoot, "stop-tts.ps1");
  const initPy = join(tempRoot, "__init__.py");
  const rootWin = umbraRootWin();
  writeFileSync(runCmd, `@echo off
setlocal
set ROOT=${rootWin}
cd /d "%ROOT%"
set PYTHONPATH=%ROOT%
set DOC_READER_TTS_HOST=100.72.151.28
set DOC_READER_TTS_PORT=8771
set DOC_READER_TTS_ENGINES=chatterbox,kokoro,whisper
set DOC_READER_TTS_DEVICE=cuda
set DOC_READER_STT_MODEL=large-v3
set DOC_READER_STT_BEAM_SIZE=1
set DOC_READER_STT_PRELOAD=1
set DOC_READER_KOKORO_PRELOAD=1
set PATH=%ROOT%\.venv\Lib\site-packages\torch\lib;%PATH%
if not exist logs mkdir logs
:restart
"%ROOT%\\.venv\\Scripts\\python.exe" -m doc_reader.tts_service --host %DOC_READER_TTS_HOST% --port %DOC_READER_TTS_PORT% --engines %DOC_READER_TTS_ENGINES% --device cuda >> "%ROOT%\\logs\\tts.log" 2>> "%ROOT%\\logs\\tts.err.log"
echo [%DATE% %TIME%] doc-reader-tts exited with %ERRORLEVEL%, restarting in 5 seconds >> "%ROOT%\\logs\\tts.err.log"
timeout /t 5 /nobreak >nul
goto restart
`);
  writeFileSync(stopPs1, `$procs = Get-CimInstance Win32_Process | Where-Object {
  ($_.CommandLine -like '*doc_reader.tts_service*' -or $_.CommandLine -like '*run-tts.cmd*') -and
  $_.ProcessId -ne $PID
}
foreach ($proc in $procs) {
  Stop-Process -Id $proc.ProcessId -Force
}
`);
  writeFileSync(initPy, "");
  return { runCmd, stopPs1, initPy };
}

async function installUmbraTts() {
  const rootWin = umbraRootWin();
  let exitCode = await runUmbra(`if not exist "${rootWin}\\doc_reader" mkdir "${rootWin}\\doc_reader"`);
  if (exitCode !== 0) return exitCode;
  exitCode = await runUmbra(`if not exist "${rootWin}\\logs" mkdir "${rootWin}\\logs"`);
  if (exitCode !== 0) return exitCode;

  const tempFiles = writeUmbraTempFiles();
  for (const [local, remote] of [
    [tempFiles.initPy, `${ttsUmbraRoot}/doc_reader/__init__.py`],
    [join(packageRoot, "doc_reader", "tts_service.py"), `${ttsUmbraRoot}/doc_reader/tts_service.py`],
    [tempFiles.runCmd, umbraScriptPath("run-tts.cmd")],
    [tempFiles.stopPs1, umbraScriptPath("stop-tts.ps1")],
  ]) {
    exitCode = await copyUmbraFile(local, remote);
    if (exitCode !== 0) return exitCode;
  }

  exitCode = await runUmbra(`if exist "${rootWin}\\.venv" if not exist "${rootWin}\\.venv\\pyvenv.cfg" rmdir /s /q "${rootWin}\\.venv"`);
  if (exitCode !== 0) return exitCode;
  exitCode = await runUmbra(`if not exist "${rootWin}\\.venv\\pyvenv.cfg" uv venv --python 3.11 "${rootWin}\\.venv"`);
  if (exitCode !== 0) return exitCode;
  exitCode = await runUmbra(`uv pip install --python "${rootWin}\\.venv\\Scripts\\python.exe" --index-url https://download.pytorch.org/whl/cu124 torch==2.6.0 torchaudio==2.6.0 torchvision==0.21.0`);
  if (exitCode !== 0) return exitCode;
  exitCode = await runUmbra(`uv pip install --python "${rootWin}\\.venv\\Scripts\\python.exe" --extra-index-url https://download.pytorch.org/whl/cu124 --index-strategy unsafe-best-match chatterbox-tts kokoro faster-whisper soundfile ${spacyEnglishModelUrl}`);
  if (exitCode !== 0) return exitCode;
  exitCode = await runUmbra(`schtasks /Create /TN DocReaderTTS /SC ONLOGON /TR "\\"${rootWin}\\run-tts.cmd\\"" /F`);
  if (exitCode !== 0) return exitCode;
  console.log("[doc-reader] Umbra TTS service installed.");
  return startUmbraTts();
}

async function startUmbraTts() {
  const rootWin = umbraRootWin();
  await runUmbra(`powershell.exe -NoProfile -ExecutionPolicy Bypass -File "${rootWin}\\stop-tts.ps1"`);
  const exitCode = await runUmbra("schtasks /Run /TN DocReaderTTS");
  if (exitCode !== 0) {
    return exitCode;
  }
  for (let attempt = 0; attempt < 20; attempt += 1) {
    try {
      const payload = await fetchJson(`${ttsUmbraUrl}/healthz`, 2000);
      if (payload.ok) {
        console.log(`[doc-reader] Umbra TTS: ${ttsUmbraUrl}`);
        return 0;
      }
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }
  console.error("[doc-reader] Umbra TTS task started but health is not reachable yet.");
  return 1;
}

async function stopUmbraTts() {
  const rootWin = umbraRootWin();
  await runUmbra("schtasks /End /TN DocReaderTTS");
  return runUmbra(`powershell.exe -NoProfile -ExecutionPolicy Bypass -File "${rootWin}\\stop-tts.ps1"`);
}

async function umbraTtsStatus() {
  await runUmbra("schtasks /Query /TN DocReaderTTS");
  console.log(await healthLine("Umbra TTS", ttsUmbraUrl));
  return 0;
}

async function ttsStatus() {
  const lines = [
    await healthLine("Umbra Chatterbox/Kokoro", ttsUmbraUrl),
    await healthLine("Mac Kokoro", ttsMacUrl),
  ];
  console.log(lines.join("\n"));
  return 0;
}

async function loadTailnetApp() {
  const vendored = join(packageRoot, "vendor", "tailnet-app", "index.mjs");
  if (existsSync(vendored)) {
    return import(pathToFileURL(vendored).href);
  }
  try {
    return await import("@sproutseeds/tailnet-app");
  } catch {
    const sibling = resolve(packageRoot, "..", "tailnet-app", "lib", "index.mjs");
    if (!existsSync(sibling)) {
      throw new Error("@sproutseeds/tailnet-app is not installed and no sibling checkout was found.");
    }
    return import(pathToFileURL(sibling).href);
  }
}

function docReaderTailnetDependencies({ startDependencies }) {
  const selfCommand = fileURLToPath(import.meta.url);
  const dependencies = [
    {
      name: "mac-speech",
      healthUrl: `${ttsMacUrl.replace(/\/$/, "")}/healthz`,
      required: true,
      feature: "Mac-local speech",
      timeoutMs: 45000,
    },
  ];

  if (shouldCheckRemoteSpeech) {
    const remoteSpeech = {
      name: "remote-speech",
      healthUrl: `${ttsUmbraUrl.replace(/\/$/, "")}/healthz`,
      required: false,
      feature: "remote speech service",
      timeoutMs: 30000,
    };
    if (startDependencies && shouldAutoStartRemoteSpeech) {
      remoteSpeech.autoStart = true;
      remoteSpeech.startCommand = [process.execPath, selfCommand, "tts-umbra-start"];
    }
    dependencies.push(remoteSpeech);
  }

  if (startDependencies) {
    dependencies[0].autoStart = true;
    dependencies[0].startCommand = [process.execPath, selfCommand, "tts-mac-start"];
  }

  return dependencies;
}

async function ensureDocReaderReadiness({
  startDependencies = false,
  json = false,
  quiet = false,
} = {}) {
  const { runTailnetEnsure } = await loadTailnetApp();
  const result = await runTailnetEnsure(
    {
      appName: "doc-reader",
      host: webHost,
      port: webPort,
      httpsPort: webPort,
      healthPath: "/healthz",
      autoServe: startDependencies && process.env.DOC_READER_TAILNET_AUTOSERVE !== "0",
      dependencies: docReaderTailnetDependencies({ startDependencies }),
      timeoutMs: 6000,
    },
    {
      runner: runCapture,
    },
  );

  if (json) {
    console.log(JSON.stringify(result, null, 2));
  } else if (!quiet) {
    printDocReaderEnsure(result);
  }
  writeDocReaderReadinessStatus(result);
  return result;
}

function writeDocReaderReadinessStatus(result) {
  const state = result.ok ? "ready" : result.ready ? "degraded" : "blocked";
  mkdirSync(dirname(readinessStatusFile), { recursive: true });
  writeFileSync(
    readinessStatusFile,
    `${JSON.stringify({
      schema: "tailnet-app.readiness/1",
      appName: "doc-reader",
      generatedAt: new Date().toISOString(),
      state,
      ok: result.ok,
      ready: result.ready,
      degraded: result.degraded,
      blocked: !result.ok,
      context: result.context,
      checks: result.checks,
      dependencies: result.dependencies,
      started: result.started,
      failed: result.failed,
      nextSteps: result.nextSteps,
    }, null, 2)}\n`,
    "utf8",
  );
}

function printDocReaderEnsure(result) {
  const state = result.ok ? "ready" : result.ready ? "degraded" : "not ready";
  console.log(`[doc-reader] Startup orchestration: ${state}`);
  if (result.context?.deviceUrl) {
    console.log(`[doc-reader] Tailnet URL: ${result.context.deviceUrl}`);
  }
  if (result.deviceServe?.attempted) {
    console.log(
      `[doc-reader] Tailnet Serve: ${result.deviceServe.ok ? "configured" : `not configured (${result.deviceServe.error})`}`,
    );
  }
  for (const dependency of result.dependencies || []) {
    const dependencyState = dependency.ok ? "ready" : dependency.required ? "blocked" : "degraded";
    const started = dependency.start?.attempted ? `, start ${dependency.start.ok ? "ok" : "failed"}` : "";
    console.log(`[doc-reader] ${dependency.name}: ${dependencyState}${started}`);
  }
  for (const nextStep of result.nextSteps || []) {
    console.log(`[doc-reader] Next: ${nextStep}`);
  }
}

async function runTtsBench(extraArgs = []) {
  const prepareExitCode = await prepareManagedRuntime();
  if (prepareExitCode !== 0) {
    return prepareExitCode;
  }
  const python = join(managedRoot, ".venv", "bin", "python");
  return run(python, ["-m", "doc_reader.tts_bench", ...extraArgs], {
    env: {
      ...process.env,
      PYTHONPATH: managedRoot,
      DOC_READER_MANAGED_ROOT: managedRoot,
      DOC_READER_TTS_UMBRA_URL: ttsUmbraUrl,
      DOC_READER_TTS_MAC_URL: ttsMacUrl,
    },
  });
}

async function runWebForeground() {
  const prepareExitCode = await run(launcher, ["--prepare-only"], { env: cliEnv });
  if (prepareExitCode !== 0) {
    return prepareExitCode;
  }
  const python = process.platform === "win32"
    ? join(venvDir, "Scripts", "python.exe")
    : join(venvDir, "bin", "python");
  return run(
    python,
    ["-m", "doc_reader.webapp", "--host", webHost, "--port", String(webPort)],
    {
      env: {
        ...cliEnv,
        DOC_READER_MANAGED_ROOT: managedRoot,
        DOC_READER_WEB_HOST: webHost,
        DOC_READER_WEB_PORT: String(webPort),
        DOC_READER_TTS_UMBRA_URL: ttsUmbraUrl,
        DOC_READER_TTS_MAC_URL: ttsMacUrl,
      },
    },
  );
}

async function startWebAgent() {
  if (!macOnly("web-start")) {
    return 1;
  }
  const prepareExitCode = await prepareManagedRuntime();
  if (prepareExitCode !== 0) {
    return prepareExitCode;
  }
  writeWebLaunchAgent();
  if (isWebLaunchAgentLoaded()) {
    await run("launchctl", ["bootout", webLaunchAgentTarget()]);
    await new Promise((resolve) => setTimeout(resolve, 600));
  }
  const cleanupExitCode = await cleanupWebProcesses();
  if (cleanupExitCode !== 0) {
    return cleanupExitCode;
  }
  let bootstrapExitCode = 1;
  let bootstrapError = "";
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const bootstrapResult = runSilent("launchctl", [
      "bootstrap",
      launchAgentDomain(),
      webLaunchAgentPlist,
    ]);
    bootstrapExitCode = bootstrapResult.status;
    bootstrapError = bootstrapResult.stderr.trim() || bootstrapResult.stdout.trim();
    if (bootstrapExitCode === 0 || isWebLaunchAgentLoaded()) {
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 800));
  }
  if (bootstrapExitCode !== 0 && !isWebLaunchAgentLoaded()) {
    if (bootstrapError) {
      console.error(bootstrapError);
    }
    return bootstrapExitCode;
  }
  spawnSync("launchctl", ["enable", webLaunchAgentTarget()], {
    stdio: ["ignore", "ignore", "ignore"],
  });
  const kickstartExitCode = await run("launchctl", ["kickstart", "-k", webLaunchAgentTarget()]);
  if (kickstartExitCode === 0) {
    const healthy = await waitForWebHealth();
    if (!healthy) {
      console.error(`[doc-reader] Web app did not become healthy at ${webLocalUrl()}/healthz.`);
      return 1;
    }
    console.log(`[doc-reader] Web app: ${webLocalUrl()}`);
  }
  return kickstartExitCode;
}

async function stopWebAgent() {
  if (!macOnly("web-stop")) {
    return 1;
  }
  let exitCode = 0;
  if (isWebLaunchAgentLoaded()) {
    exitCode = await run("launchctl", ["bootout", webLaunchAgentTarget()]);
  } else {
    console.log("[doc-reader] Web app agent is not running.");
  }
  const cleanupExitCode = await cleanupWebProcesses();
  return exitCode || cleanupExitCode;
}

async function webStatus() {
  const lines = [
    "Doc Reader web status",
    `Local URL: ${webLocalUrl()}`,
    `LaunchAgent: ${existsSync(webLaunchAgentPlist) ? "installed" : "missing"} (${webLaunchAgentPlist})`,
    `Agent state: ${launchAgentStateLabel(webLaunchAgentTarget())}`,
    `Logs: ${webStdoutLog}`,
    `Errors: ${webStderrLog}`,
  ];
  try {
    const response = await fetch(`${webLocalUrl()}/healthz`);
    const payload = await response.json();
    lines.push(`Health: ${payload.ok ? "ok" : "not ok"}`);
  } catch {
    lines.push("Health: not reachable");
  }
  const host = tailscaleHost();
  if (host) {
    lines.push(`Tailnet URL: https://${host}:${webPort}`);
  }
  console.log(lines.join("\n"));
  return 0;
}

async function exposeWebOnTailscale() {
  if (!macOnly("tailscale")) {
    return 1;
  }
  const startExitCode = await startWebAgent();
  if (startExitCode !== 0) {
    return startExitCode;
  }
  const serveExitCode = await run("tailscale", [
    "serve",
    "--bg",
    "--https",
    String(webPort),
    webLocalUrl(),
  ]);
  if (serveExitCode !== 0) {
    return serveExitCode;
  }
  const host = tailscaleHost();
  if (host) {
    console.log(`[doc-reader] Tailnet URL: https://${host}:${webPort}`);
  }
  return 0;
}

function status() {
  const lines = [
    "Doc Reader app status",
    `Platform: ${process.platform === "darwin" ? "macOS" : process.platform}`,
    `Managed app: ${existsSync(managedRoot) ? "installed" : "missing"} (${managedRoot})`,
    `Native shell: ${existsSync(nativeExecutable) ? "installed" : "missing"} (${nativeApp})`,
    `LaunchAgent: ${existsSync(launchAgentPlist) ? "installed" : "missing"} (${launchAgentPlist})`,
    `Agent state: ${launchAgentStateLabel(launchAgentTarget())}`,
    `Services item: ${existsSync(servicesItem) ? "installed" : "missing"} (${servicesItem})`,
    `Applications app: ${applicationsAppState()} (${applicationsApp})`,
    `Web agent: ${launchAgentStateLabel(webLaunchAgentTarget())} (${webLocalUrl()})`,
    `Mac TTS agent: ${launchAgentStateLabel(ttsLaunchAgentTarget())} (${ttsMacUrl})`,
    `Umbra TTS: ${ttsUmbraUrl}`,
    `Logs: ${stdoutLog}`,
    `Errors: ${stderrLog}`,
  ];
  console.log(lines.join("\n"));
  return 0;
}

async function uninstallApp() {
  if (!macOnly("uninstall")) {
    return 1;
  }
  const serviceExitCode = await runScript("uninstall-context-menu-service");
  const agentExitCode = await runScript("disable-startup");
  return serviceExitCode || agentExitCode;
}

async function runCli(args) {
  if (isWindows) {
    return runWindowsLauncher(["cli", ...args]);
  }
  mkdirSync(dirname(venvDir), { recursive: true });
  const prepareExitCode = await run(launcher, ["--prepare-only"], { env: cliEnv });
  if (prepareExitCode !== 0) {
    return prepareExitCode;
  }
  const python = process.platform === "win32"
    ? join(venvDir, "Scripts", "python.exe")
    : join(venvDir, "bin", "python");
  return run(python, ["-m", "doc_reader", ...args], { env: cliEnv });
}

const args = process.argv.slice(2);
const command = args[0];

let windowsExitCode = null;
if (isWindows && command && windowsCommandMap[command]) {
  windowsExitCode = await runWindowsCommand(command, args.slice(1));
}

if (windowsExitCode !== null) {
  process.exitCode = windowsExitCode;
} else if (!command || command === "--help" || command === "-h" || command === "help") {
  usage();
  process.exitCode = 0;
} else if (command === "install") {
  process.exitCode = await installApp();
} else if (command === "start" || command === "app" || command === "--tray") {
  process.exitCode = await startAgent();
} else if (command === "open") {
  process.exitCode = await openInstalledApp();
} else if (command === "dock") {
  process.exitCode = installApplicationsApp();
} else if (command === "restart") {
  process.exitCode = await restartApp();
} else if (command === "stop") {
  process.exitCode = await stopAgent();
} else if (command === "status") {
  process.exitCode = status();
} else if (command === "doctor") {
  const result = await ensureDocReaderReadiness({ startDependencies: false, json: args.includes("--json") });
  process.exitCode = result.ok ? 0 : 1;
} else if (command === "ensure") {
  const result = await ensureDocReaderReadiness({ startDependencies: true, json: args.includes("--json") });
  process.exitCode = result.ready ? 0 : 1;
} else if (command === "web") {
  process.exitCode = await runWebForeground();
} else if (command === "web-start") {
  process.exitCode = await startWebAgent();
} else if (command === "web-stop") {
  process.exitCode = await stopWebAgent();
} else if (command === "web-status") {
  process.exitCode = await webStatus();
} else if (command === "tailscale" || command === "tailnet") {
  process.exitCode = await exposeWebOnTailscale();
} else if (command === "tts-umbra-install") {
  process.exitCode = await installUmbraTts();
} else if (command === "tts-umbra-start") {
  process.exitCode = await startUmbraTts();
} else if (command === "tts-umbra-stop") {
  process.exitCode = await stopUmbraTts();
} else if (command === "tts-umbra-status") {
  process.exitCode = await umbraTtsStatus();
} else if (command === "tts-mac-start") {
  process.exitCode = await startMacTts();
} else if (command === "tts-mac-stop") {
  process.exitCode = await stopMacTts();
} else if (command === "tts-mac-status") {
  process.exitCode = await macTtsStatus();
} else if (command === "tts-status") {
  process.exitCode = await ttsStatus();
} else if (command === "tts-bench" || command === "tts-samples") {
  process.exitCode = await runTtsBench(args.slice(1));
} else if (command === "uninstall" || command === "remove") {
  process.exitCode = await uninstallApp();
} else if (command === "disable-startup") {
  process.exitCode = await (macOnly("disable-startup") ? runScript("disable-startup") : Promise.resolve(1));
} else if (command === "install-service") {
  process.exitCode = await (macOnly("install-service") ? runScript("install-context-menu-service") : Promise.resolve(1));
} else if (command === "uninstall-service") {
  process.exitCode = await (macOnly("uninstall-service") ? runScript("uninstall-context-menu-service") : Promise.resolve(1));
} else if (command === "cli") {
  process.exitCode = await runCli(args.slice(1));
} else {
  process.exitCode = await runCli(args);
}

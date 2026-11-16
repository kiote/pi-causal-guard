// Finds the installed pi SDK for the Node tests: env PI_SDK_ROOT (the pi-coding-agent package folder),
// else `npm root -g` + /@earendil-works/pi-coding-agent.
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

let root;

export function piSdkRoot() {
  if (root) return root;
  if (process.env.PI_SDK_ROOT) return (root = process.env.PI_SDK_ROOT);
  // npm is a .cmd file on Windows, so it runs through a shell (one command string: no unescaped args).
  const npmRoot = execFileSync('npm root -g', { encoding: 'utf8', shell: true, stdio: ['ignore', 'pipe', 'ignore'] }).trim();
  return (root = join(npmRoot, '@earendil-works', 'pi-coding-agent'));
}

// file:// URL of a path inside the pi-coding-agent package.
export const piSdkUrl = (...parts) => pathToFileURL(join(piSdkRoot(), ...parts)).href;

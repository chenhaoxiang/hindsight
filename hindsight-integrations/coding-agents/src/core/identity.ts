import { readFileSync } from "node:fs";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { version as LOADED_VERSION } from "../../package.json";

/** The JSON import is bundled at build time. Reading package.json again must not relabel an
 *  already-running host when an installer replaces files on disk. */
export function integrationIdentity(packageFile?: string) {
  const here = dirname(fileURLToPath(import.meta.url));
  const root = basename(here) === "dist" ? dirname(here) : join(here, "../..");
  let diskVersion: string | null = null;
  try {
    const pkg = JSON.parse(readFileSync(packageFile ?? join(root, "package.json"), "utf8"));
    if (
      typeof pkg?.version === "string" &&
      /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(pkg.version)
    ) {
      diskVersion = pkg.version;
    }
  } catch {
    // A missing/partially replaced package file is unknown, not the version in memory.
  }
  return {
    package: "@vectorize-io/hindsight-coding-agents",
    loaded_version: LOADED_VERSION,
    disk_version: diskVersion,
    disk_matches_loaded: diskVersion === null ? null : diskVersion === LOADED_VERSION,
  };
}

/** API URLs may themselves carry credentials. Diagnostics keep the endpoint, never userinfo,
 *  query parameters or fragments; neither a token nor an unparseable value is echoed. */
export function diagnosticEndpoint(value: string | undefined): string | null {
  try {
    const url = new URL(value ?? "");
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    url.username = "";
    url.password = "";
    url.search = "";
    url.hash = "";
    return url.toString().replace(/\/$/, "");
  } catch {
    return null;
  }
}

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { diagnosticEndpoint, integrationIdentity } from "./identity";
import { version } from "../../package.json";

describe("integration identity", () => {
  it("does not relabel the loaded module when the disk package changes", () => {
    const dir = mkdtempSync(join(tmpdir(), "hs-identity-"));
    const file = join(dir, "package.json");
    try {
      writeFileSync(file, JSON.stringify({ version }));
      expect(integrationIdentity(file).disk_matches_loaded).toBe(true);
      writeFileSync(file, JSON.stringify({ version: "999.0.0-fixture.1" }));
      expect(integrationIdentity(file)).toEqual({
        package: "@vectorize-io/hindsight-coding-agents",
        loaded_version: version,
        disk_version: "999.0.0-fixture.1",
        disk_matches_loaded: false,
      });
      writeFileSync(file, JSON.stringify({ version: "1.2.0-fork.1+build.2" }));
      expect(integrationIdentity(file).disk_version).toBe("1.2.0-fork.1+build.2");
      writeFileSync(file, "{");
      expect(integrationIdentity(file)).toMatchObject({
        loaded_version: version,
        disk_version: null,
        disk_matches_loaded: null,
      });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("does not echo credentials embedded in a diagnostic endpoint", () => {
    expect(
      diagnosticEndpoint(
        "https://user:synthetic-password@fixture.invalid/api?token=synthetic-token#private"
      )
    ).toBe("https://fixture.invalid/api");
    expect(diagnosticEndpoint("synthetic-secret-not-a-url")).toBeNull();
    expect(diagnosticEndpoint("file:///private/config")).toBeNull();
  });
});

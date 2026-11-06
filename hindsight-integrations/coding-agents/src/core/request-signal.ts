import { AsyncLocalStorage } from "node:async_hooks";

// A client is shared by concurrent tool calls. A mutable client.signal would let cancelling one
// call abort another, or background ingestion; async-local scope follows only this tool's work.
const signals = new AsyncLocalStorage<AbortSignal | undefined>();

export function withRequestSignal<T>(
  signal: AbortSignal | undefined,
  run: () => Promise<T>
): Promise<T> {
  return signals.run(signal, run);
}

export function currentRequestSignal(): AbortSignal | undefined {
  return signals.getStore();
}

export function requestDeadline(timeoutMs: number): AbortSignal {
  const external = currentRequestSignal();
  external?.throwIfAborted();
  const deadline = AbortSignal.timeout(timeoutMs);
  return external ? AbortSignal.any([external, deadline]) : deadline;
}

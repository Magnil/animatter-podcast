"""Add a stream idle watchdog to dsh-opendesign-go.

The plugin declares `streamIdleTimeoutMs` but never uses it: the only deadline is
a single `requestTimeoutMs` (30 minutes). A gateway that accepts the connection
and then stops sending therefore looks like "thinking" for up to half an hour,
with no error and no way to tell it apart from slow work.

This patch arms a timer that resets on every received chunk and fires the abort
when nothing arrives for `streamIdleTimeoutMs`.
"""

from __future__ import annotations

import pathlib
import sys

#: Default target is the installed copy. Pass a path to patch a pnpm patch
#: working copy instead (``node_modules/.pnpm_patches/<pkg>/lib/index.js``).
TARGET = pathlib.Path(
    sys.argv[1]
    if len(sys.argv) > 1
    else pathlib.Path.home() / ".dsh/profiles/desktop/node_modules/dsh-opendesign-go/lib/index.js"
)

HELPERS = '''var StreamIdleError = class extends Error {
  constructor(ms) {
    super(`OpenDesign stream stalled for ${ms}ms`);
    this.name = "StreamIdleError";
    this.idleMs = ms;
  }
};
/**
 * Watchdog against a stalled stream.
 *
 * `requestTimeoutMs` alone cannot express "no data for N seconds": it is one
 * deadline for the whole request, so a gateway that stops sending mid-stream
 * holds the UI in a thinking state until that deadline expires. This timer is
 * kicked on every chunk and aborts when nothing arrives.
 * @param controller - controller whose signal is passed to the request.
 * @param idleMs - how long to wait for data before aborting; 0 disables.
 * @returns handle with `kick` (reset) and `stop` (cancel).
 */
function createIdleWatchdog(controller, idleMs) {
  let timer;
  const stop = () => {
    if (timer !== void 0) {
      clearTimeout(timer);
      timer = void 0;
    }
  };
  const kick = () => {
    if (!(idleMs > 0)) return;
    stop();
    timer = setTimeout(() => controller.abort(new StreamIdleError(idleMs)), idleMs);
    if (typeof timer.unref === "function") timer.unref();
  };
  return { kick, stop };
}
async function* consumeSse(body) {'''

OLD_STREAM_HEAD = "  async *stream(options) {"
OLD_STREAM_END = '    throw lastFailure ?? new LlmError3("OpenDesign: no usable account", MISSING_CREDENTIAL);\n  }\n'

NEW_STREAM = '''  async *stream(options) {
    const config = this.#options.config();
    const candidates = config.autoSwitch ? accountsOf(config).map((account) => account.apiKeyEnv) : [config.apiKeyEnv];
    let lastFailure;
    for (const ref of candidates) {
      const apiKey = await this.#apiKey(ref);
      // One controller per attempt. The watchdog aborts through it, which also
      // tears down the socket; otherwise a stalled upstream leaks until the
      // whole-request deadline.
      const idleController = new AbortController();
      const signal = options.signal === void 0 ? idleController.signal : AbortSignal.any([options.signal, idleController.signal]);
      const watchdog = createIdleWatchdog(idleController, config.streamIdleTimeoutMs);
      let usage;
      try {
        // Armed before the request, so a gateway that accepts the connection and
        // then sends nothing at all is caught too — not only a mid-stream stall.
        watchdog.kick();
        const response = await dispatch({ ...options, signal }, config, apiKey, this.#imageResolverFor(options.model));
        if (!response.ok) {
          lastFailure = classifyGatewayFailure(await readFailure(response));
          if (config.autoSwitch && isRetryableAccountFailure(lastFailure) && candidates.indexOf(ref) < candidates.length - 1) continue;
          throw lastFailure;
        }
        const body = response.body;
        if (body === null) throw emptyResponseFailure();
        for await (const chunk of consumeSse(body)) {
          watchdog.kick();
          if (chunk.type === "block-end") this.#options.onAccountSettled?.(ref);
          if (chunk.type === "usage") usage = chunk.usage;
          yield chunk;
        }
      } catch (cause) {
        const reason = idleController.signal.aborted === true ? idleController.signal.reason : void 0;
        if (reason instanceof StreamIdleError) {
          throw new LlmError3(`OpenDesign stream stalled: no data received for ${config.streamIdleTimeoutMs}ms`, GATEWAY_CODES.timeout, { cause: reason });
        }
        throw cause;
      } finally {
        watchdog.stop();
      }
      if (usage !== void 0) this.#options.onUsage?.({ model: options.model, accountRef: ref, usage });
      return;
    }
    throw lastFailure ?? new LlmError3("OpenDesign: no usable account", MISSING_CREDENTIAL);
  }
'''


def main() -> int:
    source = TARGET.read_text(encoding="utf-8")

    if "createIdleWatchdog" in source:
        print("already patched; nothing to do")
        return 0

    if source.count("async function* consumeSse(body) {") != 1:
        print("FAIL: could not locate consumeSse uniquely")
        return 1
    source = source.replace("async function* consumeSse(body) {", HELPERS, 1)

    if source.count(OLD_STREAM_HEAD) != 1:
        print("FAIL: could not locate stream() uniquely")
        return 1
    start = source.index(OLD_STREAM_HEAD)
    end = source.index(OLD_STREAM_END, start) + len(OLD_STREAM_END)
    source = source[:start] + NEW_STREAM + source[end:]

    TARGET.write_text(source, encoding="utf-8")
    print("patched:", TARGET)
    return 0


if __name__ == "__main__":
    sys.exit(main())

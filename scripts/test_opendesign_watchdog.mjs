/**
 * Verifies the idle watchdog against a server that stalls on purpose.
 *
 * Three cases matter:
 *  1. A stream that sends nothing at all must abort at the idle timeout,
 *     not at the 30-minute request deadline.
 *  2. A healthy stream must complete untouched.
 *  3. A stream that goes quiet *after* some data must also abort — that is the
 *     failure that produced the "thinking forever" report.
 */

import { createServer } from "node:http";
import { OpenDesignAdapter } from "dsh-opendesign-go";

const IDLE_MS = 600;

function startServer(behaviour) {
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (c) => (body += c));
    req.on("end", () => {
      res.writeHead(200, {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        connection: "keep-alive",
      });
      behaviour(res);
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, port: server.address().port }));
  });
}

function makeAdapter(port, idleMs) {
  const config = {
    baseURL: `http://127.0.0.1:${port}`,
    apiKeyEnv: "TEST_KEY",
    autoSwitch: false,
    accounts: [],
    modelParams: {},
    streamIdleTimeoutMs: idleMs,
    requestTimeoutMs: 1_800_000,
  };
  return new OpenDesignAdapter({
    config: () => config,
    resolveApiKey: async () => "sk-test",
    imageDataUrl: undefined,
    onAccountSettled: () => {},
    onUsage: () => {},
  });
}

const request = { model: "deepseek-v4.1-flash", messages: [{ role: "user", content: "hi" }] };

async function consume(adapter) {
  const chunks = [];
  for await (const chunk of adapter.stream(request)) chunks.push(chunk);
  return chunks;
}

function fail(message) {
  console.log(`FAIL: ${message}`);
  process.exitCode = 1;
}

async function main() {
  // Case 1: headers arrive, then silence. The old code waited 30 minutes here.
  {
    const { server, port } = await startServer(() => {
      /* accept the request, never write a byte, never end */
    });
    const started = Date.now();
    try {
      await consume(makeAdapter(port, IDLE_MS));
      fail("silent stream completed instead of aborting");
    } catch (error) {
      const elapsed = Date.now() - started;
      const ok = elapsed < IDLE_MS + 1500;
      const isTimeout = error.code === "TIMEOUT" || /stalled/i.test(error.message);
      if (!isTimeout) fail(`wrong error: code=${error.code} message=${error.message}`);
      if (!ok) fail(`took ${elapsed}ms, expected ~${IDLE_MS}ms`);
      if (ok && isTimeout) console.log(`ok  silent stream aborted at ${elapsed}ms: ${error.message}`);
    } finally {
      server.closeAllConnections?.();
      server.close();
    }
  }

  // Case 2: healthy stream must be unaffected.
  {
    const { server, port } = await startServer((res) => {
      res.write('data: {"choices":[{"delta":{"content":"he"}}]}\n\n');
      setTimeout(() => {
        res.write('data: {"choices":[{"delta":{"content":"llo"}}]}\n\n');
        res.write("data: [DONE]\n\n");
        res.end();
      }, 60);
    });
    try {
      const chunks = await consume(makeAdapter(port, IDLE_MS));
      const text = chunks
        .filter((c) => c.type === "text-delta")
        .map((c) => c.text)
        .join("");
      if (text === "hello") console.log(`ok  healthy stream produced "${text}"`);
      else fail(`expected "hello", got "${text}" (chunks: ${JSON.stringify(chunks).slice(0, 200)})`);
    } catch (error) {
      fail(`healthy stream threw: ${error.message}`);
    } finally {
      server.closeAllConnections?.();
      server.close();
    }
  }

  // Case 3: starts talking, then goes quiet midway — the reported symptom.
  {
    const { server, port } = await startServer((res) => {
      res.write('data: {"choices":[{"delta":{"content":"started"}}]}\n\n');
      /* then nothing */
    });
    const started = Date.now();
    try {
      await consume(makeAdapter(port, IDLE_MS));
      fail("stalled-midway stream completed instead of aborting");
    } catch (error) {
      const elapsed = Date.now() - started;
      const isTimeout = error.code === "TIMEOUT" || /stalled/i.test(error.message);
      if (!isTimeout) fail(`wrong error: ${error.message}`);
      else if (elapsed > IDLE_MS + 1500) fail(`took ${elapsed}ms`);
      else console.log(`ok  mid-stream stall aborted at ${elapsed}ms`);
    } finally {
      server.closeAllConnections?.();
      server.close();
    }
  }

  // Case 4: watchdog disabled (0) must not abort a slow-but-alive stream.
  {
    const { server, port } = await startServer((res) => {
      setTimeout(() => {
        res.write('data: {"choices":[{"delta":{"content":"late"}}]}\n\n');
        res.write("data: [DONE]\n\n");
        res.end();
      }, IDLE_MS * 2);
    });
    try {
      const chunks = await consume(makeAdapter(port, 0));
      const text = chunks.filter((c) => c.type === "text-delta").map((c) => c.text).join("");
      if (text === "late") console.log("ok  idleMs=0 disables the watchdog");
      else fail(`expected "late", got "${text}"`);
    } catch (error) {
      fail(`idleMs=0 should not abort: ${error.message}`);
    } finally {
      server.closeAllConnections?.();
      server.close();
    }
  }
}

main().then(() => {
  console.log(process.exitCode === 1 ? "\nRESULT: failures above" : "\nRESULT: all watchdog cases passed");
});

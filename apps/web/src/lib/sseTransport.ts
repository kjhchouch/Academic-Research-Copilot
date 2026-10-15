/**
 * SSE transport for `useChat`.
 *
 * The gateway (apps/api) streams its own event vocabulary — `token` / `done` /
 * `error` — not the AI SDK's `UIMessageChunk` protocol. This class is the only
 * place that knows both, and because it is TypeScript the mapping is checked
 * against the installed SDK at compile time instead of silently drifting.
 *
 * `TextStreamChatTransport` was the obvious candidate and does not work here:
 * it decodes a `text/plain` body straight into text deltas and has no idea what
 * an `event:` line is. `DefaultChatTransport` would work only if Python emitted
 * the SDK's internal chunk format, which couples the gateway to a package it
 * does not depend on.
 *
 * Only `processResponseStream` is implemented. Request building, abort, headers
 * and non-2xx handling are inherited from `HttpChatTransport`.
 */

import { HttpChatTransport } from "ai";
import type { UIMessage, UIMessageChunk } from "ai";

/** One parsed SSE event. `data` is the joined multi-line data field. */
type SseEvent = { event: string; data: string };

/**
 * Split a raw event block into fields, per the SSE spec.
 *
 * Comment lines (leading `:`) are skipped — that is how sse-starlette sends its
 * 15s keepalive ping, and a proxy-facing keepalive must not become a message.
 * Multi-line `data:` fields join with "\n"; only ONE leading space after the
 * colon is stripped, since any further spaces are payload.
 */
function parseSseEvent(block: string): SseEvent | null {
  let event = "message";
  const dataLines: string[] = [];

  for (const line of block.split(/\r\n|\n|\r/)) {
    if (line === "" || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") event = value;
    else if (field === "data") dataLines.push(value);
  }

  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join("\n") };
}

/**
 * Event boundary is a blank line. sse-starlette defaults to `\r\n` separators,
 * so the blank line arrives as `\r\n\r\n`; the spec also permits `\n\n` and
 * `\r\r`. The trailing remainder stays in the buffer — it may be a partial
 * event, or a lone `\r` whose `\n` lands in the next chunk.
 */
const EVENT_BOUNDARY = /\r\n\r\n|\n\n|\r\r/;

export class SseChatTransport extends HttpChatTransport<UIMessage> {
  protected processResponseStream(
    stream: ReadableStream<Uint8Array<ArrayBufferLike>>,
  ): ReadableStream<UIMessageChunk> {
    const decoder = new TextDecoder();
    let buffer = "";
    let textId: string | null = null;
    let sawDone = false;
    let sawError = false;

    return stream
      .pipeThrough(
        new TransformStream<Uint8Array<ArrayBufferLike>, string>({
          transform(chunk, controller) {
            // stream: true — a multi-byte UTF-8 char can straddle two chunks,
            // which matters here because the payload is Chinese.
            controller.enqueue(decoder.decode(chunk, { stream: true }));
          },
          flush(controller) {
            const tail = decoder.decode();
            if (tail) controller.enqueue(tail);
          },
        }),
      )
      .pipeThrough(
        new TransformStream<string, UIMessageChunk>({
          start(controller) {
            controller.enqueue({ type: "start" });
          },

          transform(text, controller) {
            buffer += text;
            const blocks = buffer.split(EVENT_BOUNDARY);
            buffer = blocks.pop() ?? "";

            for (const block of blocks) {
              const parsed = parseSseEvent(block);
              if (!parsed) continue;

              switch (parsed.event) {
                case "token": {
                  const { text: delta } = JSON.parse(parsed.data) as { text?: string };
                  if (!delta) break;
                  if (textId === null) {
                    textId = "sse-text-0";
                    controller.enqueue({ type: "text-start", id: textId });
                  }
                  controller.enqueue({ type: "text-delta", id: textId, delta });
                  break;
                }
                case "done":
                  sawDone = true;
                  break;
                case "error": {
                  const { message } = JSON.parse(parsed.data) as { message?: string };
                  sawError = true;
                  controller.enqueue({
                    type: "error",
                    errorText: message ?? "gateway reported an unknown error",
                  });
                  break;
                }
                default:
                  // D3 adds route/tool/citation events. Ignoring unknown names
                  // means an older frontend survives a newer gateway.
                  break;
              }
            }
          },

          flush(controller) {
            if (textId !== null) controller.enqueue({ type: "text-end", id: textId });
            // A stream that ends without `done` was cut off — server crash, proxy
            // timeout, or a dropped connection. Surface it rather than showing a
            // reply that silently stopped mid-sentence.
            if (!sawDone && !sawError) {
              controller.enqueue({
                type: "error",
                errorText: "stream closed before the gateway sent `done`",
              });
            }
            controller.enqueue({ type: "finish" });
          },
        }),
      );
  }
}

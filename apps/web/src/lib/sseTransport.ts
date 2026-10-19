/**
 * SSE transport for `useChat`.
 *
 * The gateway (apps/api) streams its own event vocabulary — `token` / `tool` /
 * `done` / `error` — not the AI SDK's `UIMessageChunk` protocol. This class is
 * the only place that knows both, and because it is TypeScript the mapping is
 * checked against the installed SDK at compile time instead of silently
 * drifting.
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
import type { Citation } from "@/lib/api";

/** One parsed SSE event. `data` is the joined multi-line data field. */
type SseEvent = { event: string; data: string };

/**
 * Payload of the gateway's `tool` event.
 *
 * `result` is a JSON value, not a JSON string: LangChain stringifies whatever a
 * tool returns, and the gateway parses it back out so this side does not have to
 * double-decode. It can still be a plain string when the tool returned prose, so
 * the UI renders it defensively.
 */
type ToolEvent = {
  status: "start" | "end";
  name?: string | null;
  id?: string | null;
  args?: unknown;
  result?: unknown;
};

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
    let textSeq = 0;
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
                    // A fresh id per segment: a tool call closes the current text
                    // part, and reusing the id would collide with the part the
                    // SDK has already committed to the message.
                    textId = `sse-text-${textSeq++}`;
                    controller.enqueue({ type: "text-start", id: textId });
                  }
                  controller.enqueue({ type: "text-delta", id: textId, delta });
                  break;
                }
                case "tool": {
                  const evt = JSON.parse(parsed.data) as ToolEvent;
                  if (!evt.id) break;
                  const toolName = evt.name ?? "tool";
                  // Close the open text part first. ReAct interleaves prose and
                  // tool calls, and a tool part landing inside an unfinished text
                  // part is what makes the SDK's part ordering unpredictable.
                  if (textId !== null) {
                    controller.enqueue({ type: "text-end", id: textId });
                    textId = null;
                  }
                  if (evt.status === "start") {
                    controller.enqueue({
                      type: "tool-input-start",
                      toolCallId: evt.id,
                      toolName,
                    });
                    // The gateway sends args whole rather than as input deltas,
                    // so `input-available` follows immediately. Without it the
                    // part stays in `input-streaming` forever and the UI has no
                    // way to show what the tool was asked to do.
                    controller.enqueue({
                      type: "tool-input-available",
                      toolCallId: evt.id,
                      toolName,
                      input: evt.args,
                    });
                  } else {
                    controller.enqueue({
                      type: "tool-output-available",
                      toolCallId: evt.id,
                      output: evt.result,
                    });
                  }
                  break;
                }
                case "citation": {
                  // The gateway assigns citation_id from a paper a tool actually
                  // returned, so this is data, not model prose. Carried as a
                  // `data-citation` part on the assistant message rather than as
                  // text: the UI turns it into a reference-panel entry and a [n]
                  // chip lookup, and it must never be concatenated into the reply.
                  // The id keys the part, so a re-sent citation replaces instead
                  // of duplicating, while distinct ids stay distinct.
                  const citation = JSON.parse(parsed.data) as Citation;
                  if (typeof citation?.citation_id !== "number") break;
                  controller.enqueue({
                    type: "data-citation",
                    id: `citation-${citation.citation_id}`,
                    data: citation,
                  });
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
                  // D4 adds `route`. Ignoring unknown names means an older
                  // frontend survives a newer gateway.
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

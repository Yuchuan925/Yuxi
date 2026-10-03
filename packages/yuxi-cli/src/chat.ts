import { randomUUID } from "node:crypto";
import type { Client } from "./client.js";
import { isTurnTerminal } from "./chat-output.js";
import type { Json, SseEvent } from "./types.js";
import { YuxiError } from "./types.js";

export type ChatTurnStatus = "completed" | "failed" | "cancelled" | "waiting";

export interface ChatTurnResult {
  turnId: string;
  status: ChatTurnStatus;
}

export type ChatDeltaHandler = (text: string) => void;

type ChatClient = Pick<Client, "send" | "events">;

export class ChatSessionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ChatSessionError";
  }
}

/** 管理单个 Public v1 Thread 的顺序输入、cursor 与 Turn 事件。 */
export class ChatSession {
  private cursor: string | undefined;
  private readonly seenEventIds = new Set<string>();

  constructor(private readonly client: ChatClient, private readonly threadId: string) {}

  async send(message: string, onDelta: ChatDeltaHandler): Promise<ChatTurnResult> {
    const accepted = await this.client.send(this.threadId, message, randomUUID());
    const turnId = String(accepted.turn_id ?? "");
    if (!turnId) throw new ChatSessionError("服务端输入回执缺少 turn_id");

    let terminalStatus: ChatTurnStatus | undefined;
    let deltaCount = 0;
    let reconnects = 0;
    while (!terminalStatus) {
      try {
        for await (const event of this.client.events(this.threadId, this.cursor)) {
          this.advanceCursor(event);
          const data = parseEventData(event.data);
          if (!isCurrentTurn(data, turnId)) continue;
          if (this.isDuplicate(data)) continue;

          const type = eventType(event, data);
          if (type === "agent.session.turn.output_text.delta") {
            const delta = textDelta(data);
            if (delta) { deltaCount += 1; onDelta(delta); }
          }
          if (isTurnTerminal(type)) {
            terminalStatus = terminalStatusFrom(type);
            break;
          }
        }
      } catch (error) {
        if (!isRetryableStreamError(error) || reconnects >= 3) throw error;
        await sleep(250 * 2 ** reconnects);
        reconnects += 1;
        continue;
      }
      if (!terminalStatus) {
        if (reconnects >= 3) throw new ChatSessionError(`Thread 事件流在 Turn ${turnId} 终态前结束`);
        await sleep(250 * 2 ** reconnects);
        reconnects += 1;
      }
    }
    if (terminalStatus === "completed" && deltaCount === 0) {
      throw new ChatSessionError(`Turn ${turnId} 已完成，但事件流没有 output_text.delta`);
    }
    return { turnId, status: terminalStatus };
  }

  private advanceCursor(event: SseEvent): void {
    if (event.id) this.cursor = event.id;
  }

  private isDuplicate(data: unknown): boolean {
    if (!data || typeof data !== "object" || Array.isArray(data)) return false;
    const eventId = (data as Json).event_id;
    if (typeof eventId !== "string" || !eventId) return false;
    if (this.seenEventIds.has(eventId)) return true;
    this.seenEventIds.add(eventId);
    if (this.seenEventIds.size > 4096) this.seenEventIds.delete(this.seenEventIds.values().next().value as string);
    return false;
  }
}

function parseEventData(value?: string): unknown {
  if (!value) return undefined;
  try { return JSON.parse(value); } catch { throw new ChatSessionError("Thread SSE data 不是 JSON"); }
}

function isCurrentTurn(value: unknown, turnId: string): boolean {
  return Boolean(value && typeof value === "object" && !Array.isArray(value) && (value as Json).turn_id === turnId);
}

function eventType(event: SseEvent, data: unknown): string {
  if (event.event) return event.event;
  if (data && typeof data === "object" && !Array.isArray(data)) return String((data as Json).type ?? "");
  return "";
}

function textDelta(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const delta = (value as Json).delta;
  return typeof delta === "string" ? delta : "";
}

function terminalStatusFrom(type: string): ChatTurnStatus {
  const status = type.slice("agent.session.turn.".length);
  if (status === "completed" || status === "failed" || status === "cancelled" || status === "waiting") return status;
  throw new ChatSessionError(`未知的 Turn 终态事件: ${type}`);
}

function isRetryableStreamError(error: unknown): boolean {
  if (!(error instanceof YuxiError)) return false;
  return error.status === undefined || error.status === 429 || error.status >= 500;
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

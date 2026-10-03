export function isTurnTerminal(type: string): boolean {
  return [
    "agent.session.turn.completed",
    "agent.session.turn.failed",
    "agent.session.turn.cancelled",
    "agent.session.turn.waiting",
  ].includes(type);
}


export const DEFAULT_ALL_AGENT_RESOURCE_KINDS = Object.freeze([
  'tools',
  'knowledges',
  'mcps',
  'skills',
  'subagents'
])

export const isDefaultAllAgentResourceKind = (kind) =>
  DEFAULT_ALL_AGENT_RESOURCE_KINDS.includes(kind)

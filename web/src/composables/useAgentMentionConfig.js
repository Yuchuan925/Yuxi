import { computed } from 'vue'
import { isDefaultAllAgentResourceKind } from '@/utils/agentConfigUtils'

export function useAgentMentionConfig({
  currentAgentState,
  currentThreadFiles,
  currentThreadAttachments,
  workspaceMentionFiles,
  configurableItems,
  agentConfig,
  availableKnowledgeBases,
  availableMcps,
  availableSkills
}) {
  const mentionConfig = computed(() => {
    const rawFiles = currentAgentState.value?.files || {}
    const files = []
    const seenPaths = new Set()
    const workspaceFiles = Array.isArray(currentThreadFiles?.value) ? currentThreadFiles.value : []
    const userWorkspaceFiles = Array.isArray(workspaceMentionFiles?.value)
      ? workspaceMentionFiles.value
      : []

    const pushFile = (entry) => {
      const path = entry?.path || ''
      if (!path || seenPaths.has(path)) return
      seenPaths.add(path)
      files.push(entry)
    }

    if (typeof rawFiles === 'object' && !Array.isArray(rawFiles) && rawFiles !== null) {
      Object.entries(rawFiles).forEach(([filePath, fileData]) => {
        pushFile({
          path: filePath,
          ...fileData
        })
      })
    }

    const attachments = Array.isArray(currentThreadAttachments?.value)
      ? currentThreadAttachments.value
      : []
    attachments.forEach((attachment) => {
      const path = attachment?.path || ''
      if (!path) return
      pushFile({
        path,
        size: attachment.file_size,
        modified_at: attachment.uploaded_at,
        artifact_url: attachment.artifact_url,
        file_name: attachment.file_name,
        status: attachment.status
      })
    })

    userWorkspaceFiles.forEach((entry) => {
      const path = entry?.virtual_path || ''
      if (!path || entry?.is_dir) return
      pushFile({
        path,
        size: entry.size,
        modified_at: entry.modified_at,
        file_name: entry.name
      })
    })

    workspaceFiles.forEach((entry) => {
      const path = entry?.path || ''
      if (!path.startsWith('/home/gem/user-data/workspace/') || entry?.is_dir) return
      pushFile({
        path,
        size: entry.size,
        modified_at: entry.modified_at,
        artifact_url: entry.artifact_url,
        file_name: entry.name
      })
    })

    const configItems = configurableItems.value || {}
    const currentConfig = agentConfig.value || {}
    let includeAllKnowledgeBases = false
    let includeAllMcps = false
    let includeAllSkills = false
    let includeAllSubagents = false
    const allowedKbNames = new Set()
    const allowedMcpNames = new Set()
    const allowedSkillNames = new Set()
    const allowedSubagentNames = new Set()
    const subagentOptionMap = new Map()

    Object.entries(configItems).forEach(([key, item]) => {
      const kind = item?.kind
      const val = currentConfig[key]

      if (val === null && isDefaultAllAgentResourceKind(kind)) {
        includeAllKnowledgeBases ||= kind === 'knowledges'
        includeAllMcps ||= kind === 'mcps'
        includeAllSkills ||= kind === 'skills'
        includeAllSubagents ||= kind === 'subagents'
      } else if (Array.isArray(val)) {
        if (kind === 'knowledges') {
          val.forEach((v) => allowedKbNames.add(v))
        } else if (kind === 'mcps') {
          val.forEach((v) => allowedMcpNames.add(v))
        } else if (kind === 'skills' || key === 'skills') {
          val.forEach((v) => allowedSkillNames.add(v))
        } else if (kind === 'subagents' || key === 'subagents') {
          val.forEach((v) => allowedSubagentNames.add(v))
        }
      }

      if (kind === 'subagents' || key === 'subagents') {
        const options = Array.isArray(item?.options) ? item.options : []
        options.forEach((option) => {
          if (option == null) return

          const value =
            typeof option === 'object'
              ? option.id || option.value || option.name || option.label
              : option
          if (!value) return

          subagentOptionMap.set(value, {
            id: value,
            name: typeof option === 'object' ? option.name || option.label || value : value,
            description: typeof option === 'object' ? option.description || '' : ''
          })
        })
      }
    })

    if (includeAllSubagents) {
      subagentOptionMap.forEach((_, name) => allowedSubagentNames.add(name))
    }

    const knowledgeBases = includeAllKnowledgeBases
      ? availableKnowledgeBases.value
      : availableKnowledgeBases.value.filter((kb) => allowedKbNames.has(kb.name))
    const mcps = includeAllMcps
      ? availableMcps.value
      : availableMcps.value.filter((mcp) => allowedMcpNames.has(mcp.name))
    const skills = includeAllSkills
      ? availableSkills.value
      : availableSkills.value.filter((skill) => {
          const skillName = skill.name || ''
          const skillSlug = skill.slug || ''
          return allowedSkillNames.has(skillName) || allowedSkillNames.has(skillSlug)
        })
    const subagents = Array.from(allowedSubagentNames)
      .filter((name) => !!name)
      .map(
        (name) =>
          subagentOptionMap.get(name) || {
            id: name,
            name,
            description: ''
          }
      )

    if (
      !files.length &&
      !knowledgeBases.length &&
      !mcps.length &&
      !skills.length &&
      !subagents.length
    )
      return null

    return {
      files,
      knowledgeBases,
      mcps,
      skills,
      subagents
    }
  })

  return {
    mentionConfig
  }
}

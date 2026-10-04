const sessionTimestamp = (session) => {
  const timestamp = Date.parse(session.created_at || '')
  return Number.isNaN(timestamp) ? 0 : timestamp
}

const sortSidebarSessions = (sessions) =>
  [...sessions].sort((left, right) => {
    if (left.is_pinned !== right.is_pinned) return left.is_pinned ? -1 : 1
    return sessionTimestamp(right) - sessionTimestamp(left)
  })

export const deriveProjectThreadStatus = (sessions) => {
  if (sessions.some((session) => session.thread_status === 'loading')) {
    return 'loading'
  }
  if (sessions.some((session) => session.thread_status === 'ready')) {
    return 'ready'
  }
  return 'done'
}

export const buildProjectSessionGroups = (projects, sessions) => {
  const sortedSessions = sortSidebarSessions(sessions)
  const activeProjects = projects.filter(
    (project) => project.status !== 'deleted' && project.selection_status === 'selectable'
  )
  const sessionsByProject = new Map(activeProjects.map((project) => [project.id, []]))
  const otherSessions = []

  sortedSessions.forEach((session) => {
    const projectSessions = sessionsByProject.get(session.project_id)
    if (projectSessions) {
      projectSessions.push(session)
    } else {
      otherSessions.push(session)
    }
  })

  return {
    groups: activeProjects.map((project) => {
      const projectSessions = sessionsByProject.get(project.id)
      return {
        project,
        sessions: projectSessions,
        threadStatus: deriveProjectThreadStatus(projectSessions)
      }
    }),
    otherSessions
  }
}

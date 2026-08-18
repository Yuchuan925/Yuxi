import assert from 'node:assert/strict'
import test from 'node:test'

import {
  PROJECT_WORKDIR_PREFIX,
  shouldAutoOpenAgentPanel
} from '../../src/utils/agentPanelAutoOpen.js'

test('Project Workdir 出现普通文件时自动打开 AgentPanel', () => {
  assert.equal(
    shouldAutoOpenAgentPanel([
      { path: `${PROJECT_WORKDIR_PREFIX}abc/uploads`, is_dir: true },
      { path: `${PROJECT_WORKDIR_PREFIX}abc/report.md`, is_dir: false }
    ]),
    true
  )
})

test('目录、User Data 和旧 outputs 路径不会触发 Project Viewer', () => {
  assert.equal(
    shouldAutoOpenAgentPanel([
      { path: `${PROJECT_WORKDIR_PREFIX}abc/outputs`, is_dir: true },
      { path: '/home/gem/user-data/workspace/private.md', is_dir: false },
      { path: '/home/gem/user-data/outputs/legacy.md', is_dir: false }
    ]),
    false
  )
})

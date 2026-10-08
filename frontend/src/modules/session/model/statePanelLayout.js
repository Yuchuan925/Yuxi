/** 右侧留白足够时嵌入状态面板，否则在按钮下方悬浮。 */
export const getStatePanelPlacement = (workspace, trigger, input, embeddedArea) => {
  const embedded = embeddedArea && embeddedArea.right - embeddedArea.left >= 340
  const top = trigger.bottom - workspace.top + 8
  const bottom = Math.min(workspace.bottom, input?.top ?? workspace.bottom)
  const right = embedded
    ? workspace.right - embeddedArea.left - 340
    : Math.max(8, workspace.right - trigger.right)
  return {
    mode: embedded ? 'embedded' : 'floating',
    style: {
      top: `${top}px`,
      right: `${right}px`,
      width: `${embedded ? 340 : Math.min(340, Math.max(0, trigger.right - workspace.left - 8))}px`,
      maxHeight: `${Math.max(0, bottom - workspace.top - top - 8)}px`
    }
  }
}

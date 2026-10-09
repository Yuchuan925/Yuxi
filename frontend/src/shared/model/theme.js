import { ref } from 'vue'
import { defineStore } from 'pinia'
import { theme } from 'ant-design-vue'

export const useThemeStore = defineStore('theme', () => {
  // 从 localStorage 读取保存的主题，默认为浅色
  const isDark = ref(localStorage.getItem('theme') === 'dark')

  /** 从当前 CSS 色板装配控件主题，提供算法需要的实际色值。 */
  function createTheme() {
    const styles = getComputedStyle(document.documentElement)
    const css = (name) => styles.getPropertyValue(name).trim()
    return {
      algorithm: isDark.value ? theme.darkAlgorithm : theme.defaultAlgorithm,
      token: {
        fontFamily: css('--font-family'),
        fontSize: 14,
        fontWeightStrong: 600,
        colorPrimary: css('--main-color'),
        colorLink: css('--main-color'),
        colorLinkHover: css('--main-600'),
        colorLinkActive: css('--main-800'),
        colorSuccess: css('--color-success-500'),
        colorWarning: css('--color-warning-500'),
        colorError: css('--color-error-500'),
        colorInfo: css('--color-info-500'),
        colorText: css('--gray-900'),
        colorTextSecondary: css('--gray-600'),
        colorTextTertiary: css('--gray-500'),
        colorTextQuaternary: css('--gray-400'),
        colorTextLightSolid: css('--color-text-inverse'),
        colorBgContainer: css('--gray-0'),
        colorBgElevated: css('--color-bg-elevated'),
        colorBgLayout: css('--gray-50'),
        colorBorder: css('--gray-200'),
        colorBorderSecondary: css('--gray-150'),
        colorFillAlter: css('--gray-25'),
        controlOutline: css('--main-100'),
        borderRadius: 8,
        controlHeight: 32,
        controlHeightSM: 24,
        controlHeightLG: 40,
        boxShadow: css('--shadow-popover'),
        boxShadowSecondary: css('--shadow-popover'),
        wireframe: false
      },
      components: {
        Button: {
          borderRadius: parseFloat(css('--radius-control')),
          colorTextLightSolid: css('--color-on-primary'),
          colorPrimaryHover: css(isDark.value ? '--main-600' : '--main-800'),
          colorPrimaryActive: css(isDark.value ? '--main-800' : '--main-900'),
          colorError: css('--color-error-700'),
          colorErrorHover: css('--color-error-700'),
          colorErrorActive: css('--color-error-900')
        },
        Modal: { borderRadiusLG: parseFloat(css('--radius-dialog')), fontSizeHeading5: 18 },
        Tooltip: {
          colorBgDefault: css('--gray-900'),
          colorTextLightSolid: css('--gray-0')
        },
        Tag: { borderRadiusSM: 4 },
        Form: { colorTextHeading: css('--gray-800') },
        Switch: {
          colorTextLightSolid: css('--color-on-primary'),
          colorTextQuaternary: css('--gray-600'),
          colorTextTertiary: css('--gray-700')
        },
        Message: { borderRadiusLG: 8 },
        Notification: { borderRadiusLG: 8 },
        Alert: {
          borderRadiusLG: 8,
          colorSuccessBg: css('--gray-25'),
          colorSuccessBorder: css('--gray-150'),
          colorSuccess: css('--color-success-700'),
          colorInfoBg: css('--gray-25'),
          colorInfoBorder: css('--gray-150'),
          colorInfo: css('--main-color'),
          colorWarningBg: css('--gray-25'),
          colorWarningBorder: css('--gray-150'),
          colorWarning: css('--color-warning-900'),
          colorErrorBg: css('--gray-25'),
          colorErrorBorder: css('--gray-150'),
          colorError: css('--color-error-700')
        }
      }
    }
  }

  updateDocumentTheme()
  const currentTheme = ref(createTheme())

  // 切换主题
  function toggleTheme() {
    setTheme(!isDark.value)
  }

  // 设置主题
  function setTheme(dark) {
    isDark.value = dark
    localStorage.setItem('theme', dark ? 'dark' : 'light')
    updateDocumentTheme()
    currentTheme.value = createTheme()
  }

  // 更新 document 的主题类
  function updateDocumentTheme() {
    if (isDark.value) {
      document.documentElement.classList.add('dark')
    } else {
      document.documentElement.classList.remove('dark')
    }
  }

  return {
    isDark,
    currentTheme,
    toggleTheme,
    setTheme
  }
})
